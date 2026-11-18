"""Reports the model calls of one attempt: each logical call, and every transport attempt made for it.

A logical call is one request the pipeline asked for (a table mapping round, a window, a page). The client may
answer it from a cache or send it several times; those attempts are linked to the call here, in the thread that
made it. The request itself, its hash and the ledger are never changed.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from kb_ingest.llm import ModelCallError, ModelClient
from kb_ingest.llm.identity import Json
from kb_ingest.llm.logical_request import LogicalRequestBinding

from . import journal
from .hooks import current_parent, current_visual_asset, error_code
from .schema import ERRORS, safe_model
from .visual_steps import current as current_visual_context

# Request states that do not prove how a request ended.
_OPEN_STATES = (None, "pending", "sent", "unknown")
_STAGE_PREFIXES = (
    ("table-", "table_mapping"),
    ("visual-", "visual_reading"),
    ("entities-", "entity_extraction"),
    ("attributes-", "entity_extraction"),
)


@dataclass
class _Call:
    """One logical call in flight; the transport records of the current thread are attached to it."""

    root: Path
    fields: dict[str, object]
    forbidden: tuple[object, ...]
    source: str = "unknown"
    request_state: str | None = None
    request_hash: str | None = None
    possible_duplicate_charge: bool = False
    dispatched: bool = False
    began: float = field(default_factory=time.monotonic)


_current: ContextVar[_Call | None] = ContextVar("observation_model_call", default=None)


def stage_of(name: str) -> str:
    """The pipeline stage a call belongs to, from its phase name."""
    if name == "handwriting-check":
        return "visual_reading"
    for prefix, stage in _STAGE_PREFIXES:
        if name.startswith(prefix):
            return stage
    if name in ("synonyms", "identity"):
        return name
    return "claims" if "claim" in name else "resolve"


class ModelCalls:
    """The model calls of one attempt. With no attempt directory every method does nothing."""

    def __init__(self, root: Path | None, client: object, assets: Sequence[str] = ()) -> None:
        self._root = root
        self._forbidden = (getattr(client, "api_key", None),)
        self._model = safe_model(getattr(client, "model", None), self._forbidden)
        self._protocol = getattr(client, "protocol", None)
        self._client = client
        # Pages are shown per document only when the task has several originals.
        self._documents = {asset: index for index, asset in enumerate(assets, 1)} if len(assets) > 1 else {}

    def _fields(self, name: str, payload: object) -> dict[str, object]:
        kind, parent = current_parent() or (None, None)
        return {
            "kind": "model_call",
            "stage": stage_of(name),
            "item_id": name,
            "logical_id": uuid.uuid4().hex,
            "parent_kind": kind,
            "parent_id": parent,
            "requested_model": self._model,
            "protocol": self._protocol,
            "visual_targets": self._visual_targets(name, payload),
        }

    def _visual_targets(self, name: str, payload: object) -> list[dict[str, object]] | None:
        context = current_visual_context()
        if name == "handwriting-check":
            return [context[1]] if context and context[1] else None
        if not isinstance(payload, Mapping) or not name.startswith("visual-"):
            return None
        reasons = context[0] if context else {}
        if name == "visual-page":
            index = self._documents.get(current_visual_asset() or "")
            return [
                {
                    "page": payload.get("page"),
                    "kind": "unread_page",
                    "document_index": index,
                    "reason": reasons.get(payload.get("block_id")),
                }
            ]
        pages = {(page.get("asset_ref"), page.get("page")) for page in _rows(payload.get("pages"))}
        return [
            {
                "page": region.get("page"),
                "kind": region.get("kind"),
                "bbox": region.get("bbox"),
                "document_index": self._documents.get(str(region.get("asset_ref"))),
                "reason": reasons.get(region.get("block_id")),
            }
            for region in _rows(payload.get("regions"))
            if (region.get("asset_ref"), region.get("page")) in pages
        ]

    def wrap(self, call: _JournalledCall) -> _JournalledCall:
        """The same call, reported on the timeline; unchanged when there is no attempt directory."""
        return call if self._root is None else _ObservedCall(call, self)

    @contextmanager
    def observing(self, name: str, payload: object) -> Iterator[None]:
        """Report the call made inside, once `dispatching()` says it is really asked for.

        It ends completed, failed, or unknown when the client's records do not prove how the request ended.
        """
        if self._root is None:
            yield
            return
        call = _Call(self._root, self._fields(name, payload), self._forbidden)
        token = _current.set(call)
        try:
            yield
        except BaseException as error:
            if call.dispatched:
                code = error_code(error)
                state = self._request_state(call)
                status = "unknown" if state in _OPEN_STATES or code == "model_outcome_unknown" else "failed"
                _end(call, status, state, error_code=code)
            raise
        else:
            if call.dispatched:
                _end(call, "completed", call.request_state)
        finally:
            _current.reset(token)

    def _request_state(self, call: _Call) -> str | None:
        """The request's state in the client's own records, when the client can tell; what was seen otherwise."""
        lookup = getattr(self._client, "request_state", None)
        if not callable(lookup) or call.request_hash is None:
            return call.request_state
        state = lookup(call.request_hash)
        return state if isinstance(state, str) else None


# The pipeline's journalled model call: (name, instructions, payload, schema, *, images) -> answer. It carries a
# `sent` attribute counting the requests it really made.
type _JournalledCall = Callable[..., Json]


class _ObservedCall:
    def __init__(self, call: _JournalledCall, calls: ModelCalls) -> None:
        self._call, self._calls = call, calls

    @property
    def sent(self) -> int:
        return int(getattr(self._call, "sent", 0))

    def __call__(
        self,
        name: str,
        instructions: str,
        payload: object,
        schema: Json,
        *,
        images: list[object] | None = None,
        logical_binding: LogicalRequestBinding | None = None,
    ) -> Json:
        with self._calls.observing(name, payload):
            return self._call(
                name,
                instructions,
                payload,
                schema,
                images=images,
                **({"logical_binding": logical_binding} if logical_binding is not None else {}),
            )


def cache_hit() -> None:
    """The pipeline's own journal answered the current call: no request is made."""
    call = _current.get()
    if call is not None:
        journal.record(call.root, **call.fields, status="cache_hit", source="journal_cache")


def dispatching() -> None:
    """The current call is now asked of the client. Not proof that a request was sent."""
    call = _current.get()
    if call is not None:
        call.dispatched, call.began = True, time.monotonic()
        journal.record(call.root, **call.fields, status="started")


def _rows(value: object) -> list[Mapping[str, object]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _end(call: _Call, status: str, state: str | None, **fields: object) -> None:
    journal.record(
        call.root,
        **call.fields,
        status=status,
        elapsed_seconds=time.monotonic() - call.began,
        source=call.source,
        request_state=state or "unknown",
        possible_duplicate_charge=call.possible_duplicate_charge,
        **fields,
    )


def transport_record(row: Mapping[str, object]) -> None:
    """Attach one row of the client's attempt ledger to the logical call of the current thread."""
    call = _current.get()
    if call is None:
        return
    raw = row.get("status")
    request_hash = row.get("request_hash")
    if isinstance(request_hash, str):
        call.request_hash = request_hash  # kept in memory only, to ask the client for the request's state
    call.possible_duplicate_charge = call.possible_duplicate_charge or row.get("possible_duplicate_charge") is True
    if raw in ("completed", "cache_hit"):
        call.request_state = "completed"
    if raw == "cache_hit":
        call.source = "client_cache"
    elif raw == "started":
        call.source = "transport_attempt"
    status = raw if raw in ("started", "completed", "cache_hit") else "failed"
    if raw in ("transport_unknown", "transport_write_failed"):
        status = "unknown"
    requested = safe_model(row.get("requested_model"), call.forbidden)
    fields = {key: call.fields[key] for key in ("stage", "item_id", "logical_id", "parent_kind", "parent_id")}
    journal.record(
        call.root,
        kind="model_attempt",
        **fields,
        status=status,
        attempt_id=row.get("attempt_id"),
        attempt=row.get("attempt"),
        transport_status=raw,
        will_retry=row.get("will_retry") is True,
        possible_duplicate_charge=row.get("possible_duplicate_charge") is True,
        elapsed_seconds=row.get("wall_seconds"),
        requested_model=requested,
        returned_model=requested if requested and row.get("returned_model") == requested else None,
        error_code=raw if raw in ERRORS else None,
        visual_targets=call.fields.get("visual_targets"),
    )


class ObservedModelClient(ModelClient):
    """A model client whose attempt ledger rows are also shown on the attempt's timeline."""

    def _record(self, row: Json) -> None:
        super()._record(row)
        transport_record(row)

    def request_state(self, request_hash: str) -> str | None:
        """The request's state in the budget's execution record; None when it cannot be read."""
        execution = None
        with suppress(OSError, ValueError, KeyError, ModelCallError):
            execution = self.budget.execution(request_hash)
        state = execution.get("state") if execution else None
        return state if isinstance(state, str) else None
