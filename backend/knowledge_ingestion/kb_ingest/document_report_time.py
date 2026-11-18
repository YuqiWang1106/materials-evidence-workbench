"""Convert report-owned timestamps without touching content hashes or nested evidence."""

from collections.abc import Callable

from .core.serialization import canonical
from .storage.file_time import utc_text
from .storage.time_source import sourced_epoch


def report_times(record: dict[str, object], convert: Callable[[object], object] = sourced_epoch) -> dict[str, object]:
    result = dict(record)
    for key in ("created_at", "updated_at", "completed_at", "finished_at", "generated_at"):
        value = record.get(key)
        if value is not None:
            result[key] = convert(value)
    for key in ("legacy_job", "report"):
        child = record.get(key)
        if isinstance(child, dict):
            result[key] = report_times(child, convert)
    attempts = record.get("attempts")
    if isinstance(attempts, list):
        result["attempts"] = [report_times(attempt, convert) for attempt in attempts]
    return result


def report_generated_at(received_at: object, persisted: dict[str, object] | None) -> object:
    expected = utc_text(received_at)
    # Old reports copied the response time verbatim. Replaying publication may
    # reuse that exact JSON representation, including precision beyond microseconds.
    if persisted is not None and canonical(persisted.get("generated_at")) == canonical(received_at):
        return received_at
    return expected
