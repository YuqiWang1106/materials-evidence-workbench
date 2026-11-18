"""Bind exact execution-ledger times to the website's verified receipt copies."""

from collections.abc import Callable, Iterable

from sqlalchemy import Engine

from ..llm.project_model_dispatch import ProjectModelDispatcher
from ..storage.receipt_times import Snapshot, ambiguous_epoch, restore_receipt_times
from ..store import Store
from .website_time import TimeFields, nullable, object_fields, sequence

type LedgerBinding = Callable[[], tuple[Store, Iterable[object]]]
type ReceiptShape = Callable[[TimeFields], TimeFields]


def report_receipts(leaf: TimeFields) -> TimeFields:
    receipt = nullable(leaf)
    return object_fields(budget_receipt=receipt, report=nullable(object_fields(budget_receipt=receipt)))


def study_receipts(leaf: TimeFields) -> TimeFields:
    return object_fields(provider_receipt=nullable(leaf))


def proposal_receipts(leaf: TimeFields) -> TimeFields:
    return object_fields(proposal=nullable(study_receipts(leaf)))


def history_receipts(leaf: TimeFields) -> TimeFields:
    return object_fields(revisions=sequence(report_receipts(leaf)))


def unit_receipts(leaf: TimeFields) -> TimeFields:
    return object_fields(wiki_jobs=sequence(study_receipts(leaf)))


def units_receipts(leaf: TimeFields) -> TimeFields:
    return object_fields(units=sequence(unit_receipts(leaf)))


def _restore(binding: LedgerBinding, pending: list[Snapshot], keys: tuple[str, ...]) -> None:
    store, dispatchers = binding()
    engines = {
        (d.context.project_id, d.context.policy.model): d.context.service.engine
        for d in dispatchers
        if isinstance(d, ProjectModelDispatcher) and d.context.service.workspace_id == store.workspace_id
    }
    groups: dict[Engine, list[Snapshot]] = {}
    for receipt in pending:
        project, model = receipt.get("project_id"), receipt.get("model")
        if isinstance(project, str) and isinstance(model, str):
            groups.setdefault(engines.get((project, model), store.engine), []).append(receipt)
    for engine, receipts in groups.items():
        restore_receipt_times(engine, store.workspace_id, receipts, keys)


def with_receipts(
    fields: TimeFields, binding: LedgerBinding, shape: ReceiptShape, keys: tuple[str, ...] = ("sent_at",)
) -> TimeFields:
    def project(value: object) -> object:
        pending = []

        def capture(snapshot: Snapshot) -> Snapshot:
            if isinstance(snapshot.get("call_id"), str) and any(ambiguous_epoch(snapshot.get(key)) for key in keys):
                snapshot = snapshot.copy()
                pending.append(snapshot)
            return snapshot

        copy = shape(TimeFields({}, capture)).project(value)
        if pending:
            _restore(binding, pending, keys)
        return fields.project(copy)

    return TimeFields(fields.schema, project, threaded=True)
