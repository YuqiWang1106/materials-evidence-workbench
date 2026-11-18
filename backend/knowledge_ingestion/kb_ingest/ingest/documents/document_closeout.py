"""Public document closeout: assess, validate, and atomically persist quality and performance.

Quality and performance reports are each written through the same atomic JSON
entry. A successful quality write is not rolled back if the later performance
write fails; the two files are not jointly atomic.
"""

from __future__ import annotations

import json
import resource
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kb_ingest import contract
from kb_ingest.contract import add_issue
from kb_ingest.core import atomic_json
from kb_ingest.ingest.documents.document_quality_assess import assess_package

JsonWriter = Callable[[Path, object], None]
Clock = Callable[[], float]
RssReader = Callable[[], int]


def process_peak_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)


@dataclass(frozen=True)
class CloseIssues:
    gaps: Sequence[object]
    unresolved: Sequence[object]


@dataclass(frozen=True)
class DocumentCloseout:
    package: dict[str, Any]
    work_dir: Path
    issues: CloseIssues
    started: float
    model: object | None = None


@dataclass(frozen=True)
class DocumentCloseoutContext:
    """Optional I/O replacements. Unset fields are resolved when the service runs."""

    write_json: JsonWriter | None = None
    monotonic: Clock | None = None
    peak_rss_bytes: RssReader | None = None

    def json_writer(self) -> JsonWriter:
        return atomic_json if self.write_json is None else self.write_json

    def clock(self) -> Clock:
        return time.monotonic if self.monotonic is None else self.monotonic

    def rss_reader(self) -> RssReader:
        return process_peak_rss_bytes if self.peak_rss_bytes is None else self.peak_rss_bytes


class DocumentCloseoutService:
    def __init__(self, context: DocumentCloseoutContext | None = None) -> None:
        self._context = context or DocumentCloseoutContext()

    def complete(self, request: DocumentCloseout) -> dict[str, Any]:
        package = request.package
        issues = request.issues
        for item in issues.unresolved:
            add_issue(package, json.dumps(item, ensure_ascii=False), severity="warning")
        for item in issues.gaps:
            add_issue(package, json.dumps(item, ensure_ascii=False), severity="warning")
        report = assess_package(package, read_gaps=issues.gaps, unresolved=issues.unresolved)
        contract.validate_package(package)
        work = request.work_dir
        writer = self._context.json_writer()
        writer(work / "quality-report.json", report)
        peak_rss = self._context.rss_reader()()
        timing_path = work / "timing.json"
        timing = json.loads(timing_path.read_text()) if timing_path.is_file() else {}
        clock = self._context.clock()
        writer(
            work / "performance.json",
            {
                "elapsed_seconds": clock() - request.started,
                "reading_seconds": timing.get("reading_seconds"),
                "extraction_review_seconds": max(0, clock() - request.started - timing.get("reading_seconds", 0)),
                "process_peak_rss_bytes": peak_rss,
                "memory_scope": "process high-water mark; not exclusive per-document memory",
                "assertions": len(package["assertions"]),
                "verified_assertions": report["verified_assertions"],
                "model": request.model,
                "status": report["outcome"],
                "scientific_acceptance": False,
            },
        )
        return package


def closeout(
    package: dict[str, Any],
    work: Path,
    issues: CloseIssues,
    started: float,
    model: object | None = None,
) -> dict[str, Any]:
    """Shared closeout entry for the document pipelines and extraction."""
    return DocumentCloseoutService().complete(DocumentCloseout(package, work, issues, started, model))
