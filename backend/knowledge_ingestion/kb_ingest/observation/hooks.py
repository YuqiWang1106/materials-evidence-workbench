"""What the pipeline calls to report its work: stages, and the tables, windows and rounds inside them.

Every hook takes the attempt's artifact directory; with `None` it does nothing, so a caller without a timeline
(a test, another pipeline) needs no branch. A hook never raises for a journal problem and never returns anything
the pipeline acts on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path

from . import journal
from .schema import ERRORS, PARENT_KINDS, safe_id

_STAGE_OF = {
    "table": "table_mapping",
    "table_round": "table_mapping",
    "window": "entity_extraction",
    "embedding_batch": "resolve",
    "embedding_summary": "resolve",
    "reader_step": "reading",
}
# Fixed codes found in an exception's text, most specific first. The text itself is never recorded.
_CODES_IN_TEXT = (
    "model_outcome_unknown",
    "time_budget_exhausted",
    "transport_not_sent",
    "transport_unknown",
    "transport_write_failed",
    *(f"http_{status}" for status in (400, 401, 403, 404, 408, 429, 500, 502, 503, 504)),
)
_PHRASES = (
    ("quote occurs more than once", "quote_not_unique"),
    ("quote is not an exact literal", "quote_not_found"),
    ("source grid", "invalid_source_grid"),
    ("source cell", "invalid_source_grid"),
    ("budget", "request_allowance"),
    ("allowance", "request_allowance"),
    ("cancelled", "cancelled"),
    ("mapping", "mapping_invalid"),
)
_parent: ContextVar[tuple[str, str] | None] = ContextVar("observation_parent", default=None)
_visual_asset: ContextVar[str | None] = ContextVar("observation_visual_asset", default=None)
_trackers: dict[str, StageTracker] = {}
_trackers_guard = threading.Lock()


def error_code(error: BaseException) -> str:
    """The fixed code of a pipeline exception; never its text or any source value."""
    if isinstance(error, OSError):
        return "local_io_error"
    if type(error).__name__ == "EvidenceValidationError":
        return "review_evidence_invalid"
    text = str(error).lower()
    for code in _CODES_IN_TEXT:
        if code in text:
            return code
    for phrase, code in _PHRASES:
        if phrase in text:
            return code
    return "model_error" if type(error).__name__ == "ModelCallError" else "response_invalid"


@contextmanager
def scope(kind: str, identifier: object) -> Iterator[None]:
    """Model calls made inside belong to this table or window (in the current thread)."""
    parent = safe_id(identifier)
    token = _parent.set((kind, parent) if kind in PARENT_KINDS and parent else None)
    try:
        yield
    finally:
        _parent.reset(token)


def current_parent() -> tuple[str, str] | None:
    return _parent.get()


@contextmanager
def visual_asset(asset_id: str) -> Iterator[None]:
    """The original whose page is being sent to the vision model."""
    token = _visual_asset.set(asset_id)
    try:
        yield
    finally:
        _visual_asset.reset(token)


def current_visual_asset() -> str | None:
    return _visual_asset.get()


class Step:
    """One table, window or mapping round: `start` recorded it as started, one of the methods ends it.

    While a table or window is open, the model calls of its thread are reported as belonging to it.
    """

    def __init__(self, root: Path | None, fields: dict[str, object]) -> None:
        self._root, self._fields, self._started = root, fields, time.monotonic()
        self._parent: Token[tuple[str, str] | None] | None = None
        kind, parent = str(fields["kind"]), safe_id(fields["item_id"])
        if root is not None and kind in PARENT_KINDS and parent:
            self._parent = _parent.set((kind, parent))

    def ended(self, status: str = "completed", **fields: object) -> None:
        if self._parent is not None:
            _parent.reset(self._parent)
            self._parent = None
        if self._root is not None:
            elapsed = time.monotonic() - self._started
            journal.record(self._root, **self._fields, status=status, elapsed_seconds=elapsed, **fields)

    def failed(self, error: BaseException, *, not_processed: bool = False) -> None:
        """End with the error's code: failed, or not processed when it was never sent for lack of allowance."""
        self.ended("not_processed" if not_processed else "failed", error_code=error_code(error))


def start(root: Path | None, kind: str, item_id: object, **fields: object) -> Step:
    """Record that a table, window or mapping round started and return the handle that ends it."""
    step = Step(root, {"kind": kind, "stage": _STAGE_OF[kind], "item_id": item_id, **fields})
    if root is not None:
        journal.record(root, **step._fields, status="started")
    return step


def note(root: Path | None, kind: str, status: str, **fields: object) -> None:
    """Record one event that has no start, such as a summary."""
    if root is not None:
        journal.record(root, kind=kind, stage=_STAGE_OF[kind], status=status, **fields)


def skipped(root: Path | None, kind: str, item_ids: Iterable[object], code: str) -> None:
    """Record items that were never started, with the reason."""
    if root is None:
        return
    for item_id in item_ids:
        journal.record(root, kind=kind, stage=_STAGE_OF[kind], status="not_processed", item_id=item_id, error_code=code)


class StageTracker:
    """The stage an attempt is in. Entering another stage ends the current one; a failure ends it as failed."""

    def __init__(self, root: Path | None) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._name: str | None = None
        self._started = 0.0
        self._partial: str | None = None

    def enter(self, stage: str, **counts: object) -> None:
        """Move to `stage` (ending the previous one) and record its current counts, if any are given."""
        if self._root is None:
            return
        with self._lock:
            if self._name != stage:
                _parent.set(None)  # a table or window left open by an error does not own the next stage's calls
                self._end()
                self._name, self._started = stage, time.monotonic()
                journal.record(self._root, kind="stage", stage=stage, status="started")
            shown = {key: counts[key] for key in ("completed", "total", "failed") if key in counts}
            if shown:
                journal.record(self._root, kind="stage", stage=stage, status="started", **shown)

    def partial(self, code: str, when: object = True) -> None:
        """The current stage will end as partial with this code (if `when` is truthy)."""
        if when:
            with self._lock:
                self._partial = code

    def not_processed(self, stages: tuple[str, ...], code: str) -> None:
        """Stages that will not run at all in this attempt."""
        if self._root is None:
            return
        for stage in stages:
            journal.record(self._root, kind="stage", stage=stage, status="not_processed", error_code=code)

    def finish(self) -> None:
        """End the current stage: completed, or partial when something in it was left out."""
        with self._lock:
            self._end()

    def fail(self, error: BaseException | str) -> None:
        """End the current stage as failed. Does nothing when no stage is open."""
        code = error if isinstance(error, str) else error_code(error)
        with self._lock:
            self._end("failed", code if code in ERRORS else "task_processing_error")

    def _end(self, status: str | None = None, code: str | None = None) -> None:
        if self._root is None or self._name is None:
            return
        outcome = status or ("partial" if self._partial else "completed")
        journal.record(
            self._root,
            kind="stage",
            stage=self._name,
            status=outcome,
            error_code=code or self._partial,
            elapsed_seconds=time.monotonic() - self._started,
        )
        self._name, self._partial = None, None


def tracker(root: Path | None) -> StageTracker:
    """The stage tracker of an attempt; the task runner and the pipeline share one per directory."""
    if root is None:
        return StageTracker(None)
    with _trackers_guard:
        return _trackers.setdefault(str(root), StageTracker(root))


@contextmanager
def stage(root: Path | None, name: str, on_error: str) -> Iterator[None]:
    """Run one stage: completed when the block ends, failed when it raises.

    An I/O or review-evidence error keeps its own code; any other failure is reported as `on_error`.
    """
    stages = tracker(root)
    stages.enter(name)
    try:
        yield
    except BaseException as error:
        code = error_code(error)
        stages.fail(code if code in ("local_io_error", "review_evidence_invalid") else on_error)
        raise
    stages.finish()


def release(root: Path | None) -> None:
    """Forget the attempt's tracker once the attempt has ended."""
    with _trackers_guard:
        _trackers.pop(str(root), None)
