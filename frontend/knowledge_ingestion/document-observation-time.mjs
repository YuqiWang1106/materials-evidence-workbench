/** The timeline API sends instants as UTC text ("2026-11-13T07:00:00.123456Z"); the view computes with epoch seconds. */

const INSTANT = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z$/;

/** Epoch seconds of a UTC instant. A number is already epoch seconds; anything else is returned unchanged. */
export function epochSeconds(value) {
  if (typeof value !== 'string') return value;
  const match = INSTANT.exec(value);
  if (!match) return value;
  const whole = Date.parse(match[1] + 'Z');
  return Number.isFinite(whole) ? whole / 1000 + Number(match[2] || 0) : value;
}

const withSeconds = (row, keys) => {
  if (!row || typeof row !== 'object') return row;
  const copy = {...row};
  for (const key of keys) if (key in copy) copy[key] = epochSeconds(copy[key]);
  return copy;
};
const rows = (value, keys) => Array.isArray(value) ? value.map(row => withSeconds(row, keys)) : value;

/** A snapshot whose instants are epoch seconds. The task's own created_at is already a number. */
export function normalizeSnapshot(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const stage = ['started_at', 'ended_at'];
  return {...withSeconds(value, ['observed_at']), progress: withSeconds(value.progress, ['updated_at']),
    events: rows(value.events, ['at']), stages: rows(value.stages, stage), stage_history: rows(value.stage_history, stage),
    model_attempts: rows(value.model_attempts, ['started_at'])};
}
