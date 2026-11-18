import {normalizeSnapshot} from './document-observation-time.mjs';

/** Subscribe to read-only document diagnostics. No token or document content is put in the URL. */
export function subscribeObservation({taskId, api, onSnapshot, onConnection = (_connection) => {}, onClock = (_clock) => {},
  EventSourceClass = globalThis.EventSource, documentRef = globalThis.document,
  setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout, pollInterval = 4000,
  verifyInterval = 15000, staleAfter = 45000, requestTimeout = 10000,
  AbortControllerClass = globalThis.AbortController, now = () => Date.now()}) {
  if (typeof taskId !== 'string' || !taskId || typeof api !== 'function' || typeof onSnapshot !== 'function')
    throw new TypeError('taskId, api and onSnapshot are required');

  const endpoint = `/api/tasks/${encodeURIComponent(taskId)}/document-observation`;
  let disposed = false, paused = documentRef?.visibilityState === 'hidden';
  let source = null, timer = null, epoch = 0, requestId = 0, live = false, degraded = false;
  let accepted = null, lastReceivedAt = null, acceptedVersion = 0, recovery = null, inFlight = null, pendingWake = null;
  let lastStreamAt = null, watchStartedAt = now();

  function connection(state, reason) {
    if (!disposed) onConnection({state, lastReceivedAt, ...(reason ? {reason} : {})});
  }

  function parsed(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || value.task_id !== taskId ||
        !Number.isSafeInteger(value.generation) || value.generation < 0 ||
        !Number.isSafeInteger(value.attempt) || value.attempt < 0 ||
        typeof value.cursor !== 'string' || value.cursor.length > 100 ||
        typeof value.observed_at !== 'number' || !Number.isFinite(value.observed_at) || value.observed_at < 0) return null;
    const match = /^(\d+):(\d+):[a-f0-9]{12}$/.exec(value.cursor);
    if (!match || Number(match[1]) !== value.attempt || !Number.isSafeInteger(Number(match[2]))) return null;
    const elapsed = typeof value.elapsed_seconds === 'number' && Number.isFinite(value.elapsed_seconds) && value.elapsed_seconds >= 0
      ? value.elapsed_seconds : null;
    return {generation:value.generation, attempt:value.attempt, sequence:Number(match[2]),
      observed:value.observed_at, elapsed, cursor:value.cursor};
  }

  function accept(value) {
    const next = parsed(value);
    if (!next) return false;
    if (accepted) {
      if (next.generation === accepted.generation && next.attempt === accepted.attempt &&
          next.sequence < accepted.sequence && next.observed > accepted.observed) {
        // A newer server snapshot lost history; don't silently replace a newer
        // view or advertise it as live until synchronization is confirmed.
        live = false; degraded = true; connection('stale', 'history_regression');
        if (value.history === 'partial' || value.history === 'legacy_incomplete') void recoverHistory(next);
        else schedulePoll();
        return false;
      }
      if (recovery && next.generation === recovery.candidate.generation && next.attempt === recovery.candidate.attempt &&
          next.observed < recovery.candidate.observed) return false;
      if (next.generation < accepted.generation ||
          (next.generation === accepted.generation && next.attempt < accepted.attempt) ||
          (next.generation === accepted.generation && next.attempt === accepted.attempt && next.sequence < accepted.sequence) ||
          (next.generation === accepted.generation && next.attempt === accepted.attempt && next.sequence === accepted.sequence &&
           (next.observed < accepted.observed || (next.observed === accepted.observed && next.cursor !== accepted.cursor)))) return false;
      if (next.generation === accepted.generation && next.attempt === accepted.attempt && next.cursor === accepted.cursor) {
        // A repeated full snapshot refreshes the connection, but does not rerender the page.
        const newer = next.observed > accepted.observed;
        const clockValid = next.elapsed === null || accepted.elapsed === null || next.elapsed >= accepted.elapsed;
        if (!newer || !clockValid) return false;
        accepted.observed = Math.max(accepted.observed, next.observed);
        acceptedVersion++;
        lastReceivedAt = now();
        if (next.elapsed !== null) accepted.elapsed = next.elapsed;
        onClock({cursor:next.cursor, observed_at:next.observed, elapsed_seconds:next.elapsed});
        return true;
      }
    }
    accepted = next;
    acceptedVersion++;
    lastReceivedAt = now();
    onSnapshot(value);
    return true;
  }

  function clearPoll() {
    if (timer !== null) { clearTimer(timer); timer = null; }
  }
  function clearDeadline(request) {
    if (request?.deadline != null) { clearTimer(request.deadline); request.deadline = null; }
  }
  function deadline(request, active) {
    request.deadline = setTimer(() => {
      request.deadline = null;
      if (disposed || paused || !active()) return;
      // Fence a late reply and abort the real fetch. If an injected client
      // ignores abort, keep its slot occupied rather than spawn unbounded GETs.
      requestId++;
      request.controller?.abort();
      live = false; degraded = true;
      connection('stale', 'verification_timeout');
      clearPoll(); schedulePoll();
    }, requestTimeout);
  }
  function schedulePoll() {
    if (disposed || paused || timer !== null) return;
    timer = setTimer(() => {
      timer = null;
      const current = now();
      const reference = live ? lastStreamAt : (lastReceivedAt ?? watchStartedAt);
      if (reference !== null && current - reference >= staleAfter) {
        const wasLive = live;
        live = false; degraded = true;
        connection('stale', wasLive ? 'stream_unverified' : 'backend_unverified');
      }
      // Schedule independently of the GET: a hung request must not prevent
      // freshness checks. refresh() coalesces concurrent checks.
      schedulePoll();
      void refresh();
    }, live ? verifyInterval : pollInterval);
  }

  async function recoverHistory(candidate) {
    if (disposed || paused || recovery) return;
    clearPoll();
    const previous = inFlight;
    if (previous) { clearDeadline(previous); previous.controller?.abort(); }
    const controller = typeof AbortControllerClass === 'function' ? new AbortControllerClass() : null;
    /** @type {(value?: unknown) => void} */
    let finishRecovery = () => {};
    const pending = {candidate, epoch, version:acceptedVersion, request:++requestId, controller,
                     finished:new Promise(resolve=>{finishRecovery=resolve;})};
    recovery = pending;
    deadline(pending, () => recovery === pending);
    try {
      // Do not create a second outstanding GET if an injected or broken API
      // ignores abort. The old response is fenced by the new request id.
      if (previous) {
        await previous.promise;
        if (disposed || paused || recovery !== pending || epoch !== pending.epoch ||
            requestId !== pending.request || acceptedVersion !== pending.version) return;
      }
      // This GET is started after detecting the gap. A delayed older GET or
      // EventSource frame alone cannot authorize a sequence rollback.
      const value = normalizeSnapshot(await api(endpoint, {signal:controller?.signal, timeout:requestTimeout}));
      if (disposed || paused || recovery !== pending || epoch !== pending.epoch ||
          requestId !== pending.request || acceptedVersion !== pending.version) return;
      const next = parsed(value);
      if (!next || !accepted || next.generation !== candidate.generation || next.attempt !== candidate.attempt ||
          next.observed < candidate.observed || next.sequence < candidate.sequence ||
          !['partial', 'legacy_incomplete'].includes(value.history)) return;
      // The fresh authoritative snapshot still confirms a gap. Keep the gap
      // visible; reset only the client's cursor fence, never the task itself.
      accepted = next; acceptedVersion++; lastReceivedAt = now();
      onSnapshot(value);
      source?.close(); source = null; live = false; degraded = true;
      connection('polling', 'history_resynchronized');
      openStream();
    } catch {
      // The old snapshot remains visibly stale; the regular fallback retries.
    } finally {
      clearDeadline(pending);
      if (recovery === pending) recovery = null;
      finishRecovery();
      schedulePoll();
    }
  }

  function refresh() {
    if (disposed || paused) return Promise.resolve(null);
    if (inFlight) return inFlight.promise;
    if (recovery) return recovery.finished;
    const capturedEpoch = epoch, thisRequest = ++requestId;
    const controller = typeof AbortControllerClass === 'function' ? new AbortControllerClass() : null;
    const current = {controller, promise:null};
    inFlight = current;
    deadline(current, () => inFlight === current);
    current.promise = (async () => {
      try {
        const value = normalizeSnapshot(await api(endpoint, {signal:controller?.signal, timeout:requestTimeout}));
        if (disposed || paused || capturedEpoch !== epoch || thisRequest !== requestId) return null;
        const valid = accept(value);
        if (!live && valid) connection(degraded || !source ? 'polling' : 'connecting');
        return valid ? value : null;
      } catch {
        if (!disposed && !paused && capturedEpoch === epoch && thisRequest === requestId && !live) {
          degraded = true; connection('stale'); schedulePoll();
        }
        return null;
      } finally {
        clearDeadline(current);
        if (inFlight === current) inFlight = null;
      }
    })();
    return current.promise;
  }

  function openStream() {
    if (disposed || paused) return;
    if (typeof EventSourceClass !== 'function') {
      degraded = true; connection('stale'); schedulePoll(); return;
    }
    try {
      const current = new EventSourceClass(`${endpoint}/stream`);
      source = current;
      current.onopen = () => {
        if (disposed || paused || source !== current) return;
        live = false;
        connection('connecting');
      };
      current.onerror = () => {
        if (disposed || paused || source !== current) return;
        live = false; degraded = true; connection('stale'); clearPoll(); schedulePoll();
      };
      current.addEventListener('unavailable', () => {
        if (disposed || paused || source !== current) return;
        live = false; degraded = true; connection('stale', 'observation_unavailable'); clearPoll(); schedulePoll();
      });
      current.addEventListener('snapshot', event => {
        if (disposed || paused || source !== current) return;
        let value;
        try { value = normalizeSnapshot(JSON.parse(event.data)); } catch { return; }
        if (!accept(value)) return;
        if (recovery) {
          clearDeadline(recovery); recovery.controller?.abort(); requestId++;
        }
        lastStreamAt = now(); live = true; degraded = false; clearPoll(); connection('live'); schedulePoll();
      });
    } catch {
      source = null; degraded = true; connection('stale'); schedulePoll();
    }
  }

  function visibilityChanged() {
    if (disposed) return;
    const hidden = documentRef?.visibilityState === 'hidden';
    if (hidden === paused) return;
    paused = hidden; epoch++; requestId++; live = false;
    clearDeadline(inFlight); inFlight?.controller?.abort();
    clearDeadline(recovery); recovery?.controller?.abort();
    if (hidden) {
      clearPoll(); source?.close(); source = null; connection('paused');
    } else {
      lastStreamAt = null; watchStartedAt = now();
      connection('connecting'); openStream();
      const pending = [inFlight?.promise, recovery?.finished].filter(Boolean);
      if (pending.length) {
        // Repeated visibility toggles share one waiter even when an injected
        // API ignores abort and the old promise remains unresolved.
        if (!pendingWake) pendingWake = Promise.allSettled(pending).then(() => {
          pendingWake = null;
          if (!disposed && !paused) void refresh();
        });
      } else void refresh();
      schedulePoll();
    }
  }

  documentRef?.addEventListener?.('visibilitychange', visibilityChanged);
  if (paused) connection('paused');
  else { connection('connecting'); openStream(); void refresh(); schedulePoll(); }

  return {
    refresh,
    dispose() {
      if (disposed) return;
      disposed = true; epoch++; requestId++; clearPoll();
      clearDeadline(inFlight); inFlight?.controller?.abort(); inFlight = null;
      clearDeadline(recovery); recovery?.controller?.abort(); recovery = null;
      source?.close(); source = null;
      documentRef?.removeEventListener?.('visibilitychange', visibilityChanged);
    },
  };
}
