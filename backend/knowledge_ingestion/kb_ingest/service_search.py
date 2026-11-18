"""Select published task versions and annotate matching facts for service queries."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .document_workflow import document_query_metadata
from .query import query_package, query_package_detailed
from .service_publication import published_task_id

if TYPE_CHECKING:
    from kb_ingest.ingest.service import IngestionService


@dataclass(frozen=True, slots=True)
class SearchScope:
    source_id: str | None
    include_history: bool
    query_scope: str

    def __post_init__(self) -> None:
        if self.query_scope not in ("source_reviewed", "saved_extractions"):
            message = "unsupported query_scope"
            raise ValueError(message)

    def tasks(self, service: IngestionService) -> Iterator[dict]:
        for task in service.store.list_tasks():
            if task["state"] != "saved" or (self.source_id and task["source_id"] != self.source_id):
                continue
            source = service.store.source_state(task["source_id"])
            if not self.include_history and published_task_id(source) != task["task_id"]:
                continue
            if (
                task["source_type"] in ("paper", "patent")
                and service.records(task["task_id"])["header"].get("document_extraction_mode") == "program-only-v1"
                and service.records(task["task_id"])["header"].get("review_policy") != "human_reviewed_candidate_v1"
                and self.query_scope != "saved_extractions"
            ):
                continue
            yield task


@dataclass(frozen=True, slots=True)
class _DocumentFacts:
    status: dict
    eligible: str
    checked: set[str]

    @classmethod
    def from_package(cls, package: dict, *, detailed: bool = False) -> _DocumentFacts | None:
        status = document_query_metadata(package)
        if not status:
            return None
        eligible = (
            status["queryable_use_status"] if detailed else status.get("queryable_use_status", "document_verified")
        )
        checked = {a["assertion_id"] for a in package["assertions"] if a.get("use_status") == eligible}
        return cls(status, eligible, checked)

    def annotate(self, row: dict) -> None:
        row["document_quality"] = self.status
        if (
            self.status["document_pipeline_version"] == "2"
            and row.get("assertion_refs")
            and set(row["assertion_refs"]) <= self.checked
        ):
            row["use_status"] = self.eligible


def search(service: IngestionService, scope: SearchScope, filters: list[dict] | None) -> list[dict]:
    hits: list[dict] = []
    for task in scope.tasks(service):
        task_id = task["task_id"]
        if filters:
            package = service._document_query_package(task_id)
            facts = _DocumentFacts.from_package(package)
            summary = service.task_status(task_id).get("automation")
            for match in query_package(package, filters):
                row = {**match, "task_id": task_id}
                if facts:
                    facts.annotate(row)
                if summary:
                    row["automation"] = summary
                hits.append(row)
        else:
            facts = None
            if task["source_type"] in ("paper", "patent"):
                facts = _DocumentFacts.from_package(service._document_query_package(task_id))
            summary = service.task_status(task_id).get("automation")
            row = {k: task[k] for k in ("task_id", "source_id", "generation", "source_type", "state", "updated_at")}
            if facts:
                row["document_quality"] = facts.status
            if summary:
                row["automation"] = summary
            hits.append(row)
    return hits


def _detailed_rows(package: dict, matches: list[dict], task_id: str) -> Iterator[dict]:
    facts = _DocumentFacts.from_package(package, detailed=True)
    tabular = package["header"].get("entry_profile") == "row_measurements_v1" or bool(
        package["header"].get("standard_table_review")
    )
    assertions = {a["assertion_id"]: a for a in package["assertions"]} if tabular else None
    for match in matches:
        row = {**match, "task_id": task_id}
        if assertions is not None:
            from .ingest.tables.table_results import matched_values_projection

            row.update(matched_values_projection(assertions, match))
        if facts:
            facts.annotate(row)
        yield row


def search_detailed(service: IngestionService, scope: SearchScope, filters: list[dict] | None) -> dict:
    if not filters:
        return {
            "results": service.search(scope.source_id, filters, scope.include_history, scope.query_scope),
            "exclusions": [],
        }
    results: list[dict] = []
    exclusions: list[dict] = []
    for task in scope.tasks(service):
        task_id = task["task_id"]
        package = service._document_query_package(task_id)
        detail = query_package_detailed(package, filters)
        results.extend(_detailed_rows(package, detail["results"], task_id))
        exclusions.extend({**row, "task_id": task_id} for row in detail["exclusions"])
    return {"results": results, "exclusions": exclusions}
