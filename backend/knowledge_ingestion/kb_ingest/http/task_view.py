"""Project ingestion task records for website responses."""

from collections.abc import Iterable, Mapping
from typing import Literal, Protocol, overload


class _TaskRecord(Protocol):
    def items(self) -> Iterable[tuple[str, object]]: ...

    @overload
    def get(self, key: Literal["source_type"], /) -> object: ...

    @overload
    def get(self, key: Literal["options"], default: Mapping[str, object], /) -> Mapping[str, object]: ...


def public_task(task):
    # type: (_TaskRecord) -> dict[str, object]
    result = {k: v for k, v in task.items() if k not in ("attempt_token", "idempotency_key", "options", "receipt")}
    options = task.get("options", {})
    if task.get("source_type") in ("paper", "patent"):
        if options.get("_document_extraction_mode"):
            result["document_extraction_mode"] = options["_document_extraction_mode"]
        if options.get("_candidate_review_protocol"):
            result["candidate_review_protocol"] = options["_candidate_review_protocol"]
    return result
