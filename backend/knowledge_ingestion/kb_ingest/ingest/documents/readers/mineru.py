"""Isolated MinerU 4 MiddleJson adapter and stateless CLI runner.

The CLI is optional and is never imported into the ingestion service process.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import uuid4


@contextmanager
def _local_parse_slot(cancelled=None, timeout_seconds=None):
    """One local parser across worker processes; shared mounts span containers."""
    started = time.monotonic()
    configured = os.environ.get("MATERIALS_MINERU_LOCK_PATH")
    path = Path(configured) if configured else Path(tempfile.gettempdir()) / f"materials-mineru-{os.getuid()}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        while not acquired:
            if cancelled and cancelled():
                yield False
                return
            elapsed = time.monotonic() - started
            if timeout_seconds is not None and elapsed >= timeout_seconds:
                raise TimeoutError("mineru_queue_timeout")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                delay = 0.1 if timeout_seconds is None else min(0.1, timeout_seconds - elapsed)
                time.sleep(max(0, delay))
        yield True
    finally:
        if acquired:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _id(*parts: object) -> str:
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class _TableParser(HTMLParser):
    def __init__(self, region_id: str):
        super().__init__(convert_charrefs=True)
        self.region_id = region_id
        self.cells: list[dict] = []
        self.row = -1
        self.col = 0
        self.occupied: set[tuple[int, int]] = set()
        self.current: dict | None = None
        self.in_table = False
        self.depth = 0
        self.diagnostics: list[dict] = []

    def _finish_cell(self, *, unclosed=False) -> None:
        if self.current is not None:
            if unclosed:
                self.diagnostics.append({"kind": "table_markup_unclosed", "row": self.row})
            self.current["text"] = " ".join(self.current["text"].split())
            self.cells.append(self.current)
            self.current = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.in_table = True
            else:
                self.diagnostics.append({"kind": "nested_table_unsupported"})
            return
        if not self.in_table or self.depth != 1:
            return
        if tag == "tr":
            self._finish_cell(unclosed=True)
            self.row += 1
            self.col = 0
        elif tag in {"td", "th"}:
            self._finish_cell(unclosed=True)
            attributes = dict(attrs)

            def span(name: str) -> int:
                if name not in attributes:
                    return 1
                raw = attributes[name]
                try:
                    value = int(raw)
                    if not isinstance(raw, str) or not raw.isdecimal() or not 1 <= value <= 1000:
                        raise ValueError("invalid span")
                    return value
                except (TypeError, ValueError):
                    self.diagnostics.append(
                        {
                            "kind": "invalid_table_span",
                            "attribute": name,
                            "raw": raw,
                            "row": self.row,
                            "column": self.col,
                        }
                    )
                    return 1

            if self.row < 0:
                self.diagnostics.append({"kind": "table_cell_without_row", "row": self.row})
            while (self.row, self.col) in self.occupied:
                self.col += 1
            rowspan, colspan = span("rowspan"), span("colspan")
            overlap = []
            for r in range(self.row, self.row + rowspan):
                for c in range(self.col, self.col + colspan):
                    if (r, c) in self.occupied:
                        overlap.append([r, c])
                    self.occupied.add((r, c))
            if overlap:
                self.diagnostics.append(
                    {"kind": "table_cell_overlap", "row": self.row, "column": self.col, "overlap": overlap}
                )
            self.current = {
                "id": f"cell:{_id(self.region_id, self.row, self.col)}",
                "row": self.row,
                "column": self.col,
                "rowspan": rowspan,
                "colspan": colspan,
                "role": "header" if tag == "th" else "data",
                "text": "",
                "span_attributes": {k: v for k, v in attributes.items() if k in {"rowspan", "colspan"}},
                "anchor": {"bbox": None, "precision": "table_region_only"},
            }
            self.col += colspan
        elif tag == "br" and self.current is not None:
            self.current["text"] += " "

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.depth == 1:
            self.current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.depth == 1:
            self._finish_cell()
        elif tag == "tr" and self.depth == 1:
            self._finish_cell(unclosed=True)
        elif tag == "table" and self.depth:
            if self.depth == 1:
                self._finish_cell(unclosed=True)
            self.depth -= 1
            self.in_table = self.depth > 0


def parse_html_table(html: str, region_id: str) -> list[dict]:
    cells, _ = _parse_html_table(html, region_id)
    return cells


def _parse_html_table(html: str, region_id: str) -> tuple[list[dict], list[dict]]:
    parser = _TableParser(region_id)
    parser.feed(html)
    parser.close()
    if parser.depth or parser.current is not None:
        parser.diagnostics.append({"kind": "table_markup_unclosed"})
        parser._finish_cell()
    for cell in parser.cells:
        cell["structure_reliable"] = not parser.diagnostics
    return parser.cells, parser.diagnostics


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(filter(None, (_text(part) for part in value))).strip()
    if isinstance(value, dict):
        return _text(value.get("content") or value.get("text") or "")
    return ""


def _html(value: Any) -> str | None:
    if isinstance(value, str):
        return value if "<table" in value.lower() else None
    if isinstance(value, dict):
        if isinstance(value.get("html"), str):
            return value["html"]
        return _html(value.get("content"))
    if isinstance(value, list):
        for part in value:
            found = _html(part)
            if found:
                return found
    return None


def _bbox(raw: Any, page: int | None, dimensions: dict[int, tuple[float, float]]) -> list[float] | None:
    if page is None or not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        box = [float(x) for x in raw]
        width, height = dimensions[page]
        if not (all(math.isfinite(x) for x in box) and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
            return None
        return [box[0] * width, box[1] * height, box[2] * width, box[3] * height]
    except (ValueError, TypeError, KeyError):
        return None


def _image_ref(ref: str) -> dict:
    path = Path(ref)
    safe = not path.is_absolute() and ".." not in path.parts and "\\" not in ref
    return {"path": ref if safe else None, "status": "unverified_parser_asset" if safe else "rejected_unsafe_path"}


def _visual_parts(block: dict, page: int | None, dimensions: dict) -> tuple[list, list, list, list, list]:
    captions: list[str] = []
    footnotes: list[str] = []
    image_refs: list[dict] = []
    source_parts: list[dict] = []
    diagnostics: list[dict] = []
    parent_type = block.get("type")
    allowed_prefixes = {parent_type}
    if parent_type in {"image", "figure", "chart"}:
        allowed_prefixes = {"image", "figure", "chart"}

    def visit(value, path):
        if isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, [*path, i])
        elif isinstance(value, dict):
            box = _bbox(value.get("bbox"), page, dimensions)
            source_parts.append(
                {
                    "type": value.get("type", "unknown"),
                    "text": _text(value),
                    "content_path": path,
                    "parser_index": value.get("index"),
                    "anchor": {
                        "page": page,
                        "bbox": box,
                        "unit": "pt" if box else None,
                        "precision": "region_bbox_from_normalized" if box else "unknown",
                        "parser_bbox_normalized": value.get("bbox"),
                    },
                }
            )
            visit(value.get("content"), [*path, "content"])

    visit(block.get("content"), ["content"])
    for child in block.get("content", []) if isinstance(block.get("content"), list) else []:
        if not isinstance(child, dict):
            continue
        child_type = child.get("type")
        if isinstance(child_type, str) and child_type.endswith("_caption"):
            if child_type.removesuffix("_caption") in allowed_prefixes:
                captions.append(_text(child.get("content")))
            else:
                diagnostics.append(
                    {
                        "kind": "caption_parent_mismatch",
                        "parent_type": parent_type,
                        "child_type": child_type,
                        "text": _text(child.get("content")),
                    }
                )
        elif isinstance(child_type, str) and child_type.endswith("_footnote"):
            if child_type.removesuffix("_footnote") in allowed_prefixes:
                footnotes.append(_text(child.get("content")))
            else:
                diagnostics.append(
                    {
                        "kind": "footnote_parent_mismatch",
                        "parent_type": parent_type,
                        "child_type": child_type,
                        "text": _text(child.get("content")),
                    }
                )
        ref = child.get("image_path")
        if isinstance(ref, str):
            image_refs.append(_image_ref(ref))
    return captions, footnotes, image_refs, source_parts, diagnostics


def _pages(pages: list[int] | None, *, page_count: int | None = None) -> list[int] | None:
    if pages is None:
        return None
    if (
        not isinstance(pages, list)
        or not pages
        or any(type(p) is not int or p <= 0 or (page_count is not None and p > page_count) for p in pages)
        or len(set(pages)) != len(pages)
    ):
        raise ValueError("requested_pages must be a nonempty list of unique physical pages")
    return sorted(pages)


def _parser_mode(middle: dict) -> dict:
    extensions = middle.get("extensions", {})
    raw = extensions.get("mineru", {}) if isinstance(extensions, dict) else {}
    mode = dict(raw) if isinstance(raw, dict) else {}
    if mode.get("parse_mode") in {"txt", "ocr"}:
        mode["ocr_mode"] = mode["parse_mode"]
    return mode


def adapt_middle_json(
    middle: dict,
    *,
    asset_ref: str,
    source_sha256: str,
    page_count: int,
    page_dimensions: dict[int, tuple[float, float]],
    requested_pages: list[int] | None = None,
) -> dict:
    """Preserve parser alternatives without fabricating page or cell geometry."""
    if (
        not isinstance(middle, dict)
        or middle.get("schema") != "docvortex.middle"
        or middle.get("schema_version") != "2.0"
    ):
        raise ValueError("MinerU MiddleJson protocol 2.0 required")
    metadata = middle.get("metadata")
    producer = metadata.get("producer") if isinstance(metadata, dict) else None
    if not isinstance(producer, dict) or producer.get("name") != "mineru" or producer.get("version") != "4.0.9":
        raise ValueError("MinerU producer version 4.0.9 required")
    if not isinstance(middle.get("pages"), list):
        raise ValueError("MinerU MiddleJson pages required")
    requested = _pages(requested_pages, page_count=page_count)
    expected = set(requested if requested is not None else range(1, page_count + 1))
    regions: list[dict] = []
    statuses = dict.fromkeys(sorted(expected), "unread")
    seen: set[int] = set()
    gaps: list[dict] = []
    if requested is None and middle.get("is_full_document") is not True:
        gaps.append({"kind": "parser_not_full_document"})
    for output_pos, parser_page in enumerate(middle["pages"]):
        if isinstance(parser_page, dict) and not isinstance(parser_page.get("blocks", []), list):
            raise ValueError("MinerU page blocks must be a list")
        raw_index = parser_page.get("page_idx") if isinstance(parser_page, dict) else None
        page = (
            raw_index + 1
            if isinstance(raw_index, int) and not isinstance(raw_index, bool) and 0 <= raw_index < page_count
            else None
        )
        if page is None:
            gaps.append({"kind": "unknown_page", "output_index": output_pos})
        elif page not in expected:
            gaps.append({"kind": "unrequested_page", "page": page, "output_index": output_pos})
            page = None
        elif page in seen:
            gaps.append({"kind": "duplicate_page", "page": page})
            statuses[page] = "partial"
            page = None
        else:
            seen.add(page)
            statuses[page] = "parsed"
        for ordinal, block in enumerate(parser_page.get("blocks", []) if isinstance(parser_page, dict) else []):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "unknown")
            bbox = _bbox(block.get("bbox"), page, page_dimensions)
            page_key = raw_index if page is not None else f"unknown-output-{output_pos}"
            region_id = (
                "region:"
                f"{_id(source_sha256, 'mineru', page_key, block.get('index'), ordinal, kind, block.get('content'))}"
            )
            raw_content = block.get("content")
            table_html = _html(raw_content) if kind == "table" else None
            captions, footnotes, image_refs, source_parts, diagnostics = _visual_parts(block, page, page_dimensions)
            region = {
                "id": region_id,
                "asset_ref": asset_ref,
                "source": "mineru",
                "kind": kind,
                "page": page,
                "text": _text(raw_content) if not table_html else "",
                "anchor": {
                    "page": page,
                    "bbox": bbox,
                    "unit": "pt" if bbox else None,
                    "precision": "region_bbox_from_normalized" if bbox else "unknown",
                    "parser_bbox_normalized": block.get("bbox"),
                },
                "parser_index": block.get("index"),
                "caption": " ".join(captions) or _text(block.get("caption")),
                "footnote": " ".join(footnotes) or _text(block.get("footnote")),
                "captions": captions,
                "footnotes": footnotes,
                "image_refs": image_refs,
                "source_parts": source_parts,
                "diagnostics": diagnostics,
                "reading_risks": [],
                "cells": [],
            }
            part_types = [str(part["type"]) for part in source_parts] + [kind]
            if any("formula" in t or "equation" in t for t in part_types):
                region["reading_risks"].append("formula_requires_source_review")
            if any(d["kind"].endswith("parent_mismatch") for d in diagnostics):
                region["reading_risks"].append("caption_association_requires_source_review")
            if table_html:
                region["html"] = table_html
                region["cells"], table_diagnostics = _parse_html_table(table_html, region_id)
                region["diagnostics"].extend(table_diagnostics)
                if table_diagnostics:
                    region["reading_risks"].append("table_structure_unreliable")
                region["text"] = " ".join(c["text"] for c in region["cells"])
            for ref in (block.get("image"), block.get("image_path")):
                if isinstance(ref, str):
                    region["image_refs"].append(_image_ref(ref))
            regions.append(region)
    for p in statuses:
        if statuses[p] == "unread":
            gaps.append({"kind": "parser_page_missing", "page": p})
    complete = not gaps and seen == expected
    if complete:
        statuses = dict.fromkeys(statuses, "parsed")
    return {
        "regions": regions,
        "page_status": statuses,
        "status": "complete" if complete else "partial",
        "gaps": gaps,
        "producer": producer,
        "parser_mode": _parser_mode(middle),
    }


def resolve_mineru_command(command: list[str] | tuple[str, ...] | None) -> list[str] | None:
    if command == []:
        return None
    if command is None:
        configured = os.environ.get("MATERIALS_MINERU_COMMAND", "").strip()
        command = [configured] if configured else None
    if not command:
        return None
    executable = Path(command[0]).expanduser()
    located = str(executable) if executable.is_file() else shutil.which(str(executable))
    if not located or not os.access(located, os.X_OK) or Path(located).name not in {"mineru-kit", "mineru-kit.exe"}:
        return None
    return [located, *command[1:]]


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_mineru(
    source: Path,
    work_dir: Path,
    command: list[str],
    cancelled=None,
    *,
    tier: str = "basic",
    pages: list[int] | None = None,
    ocr_mode: str = "auto",
    execution_timeout_seconds: float | None = None,
    queue_timeout_seconds: float | None = None,
    include_metrics: bool = False,
) -> dict:
    """Run a bounded local parse; retain its raw output separately from the cache.

    The caller owns attempt identity and cumulative limits. Timing covers actual
    queue wait and subprocess execution, including shutdown on cancellation.
    """
    queue_started = None
    execution_started = None
    execution_ended = None
    queued = 0.0
    requested = {"tier": tier, "pages": pages, "ocr_mode": ocr_mode}

    def finish(payload: dict) -> dict:
        if include_metrics:
            now = time.monotonic()
            payload.update(
                {
                    "requested_parameters": requested,
                    "queue_seconds": queued
                    if execution_started is not None
                    else (max(0, now - queue_started) if queue_started is not None else 0.0),
                    "execution_seconds": max(0, (execution_ended or now) - execution_started)
                    if execution_started is not None
                    else 0.0,
                }
            )
        return payload

    try:
        selected = _pages(pages)
        if tier not in {"basic", "standard"} or ocr_mode not in {"auto", "txt", "ocr"}:
            raise ValueError("unsupported parser parameters")
        requested["pages"] = selected
    except (ValueError, TypeError):
        return finish({"status": "not_ready", "error": "invalid_mineru_parameters"})
    try:
        raw_timeout = (
            execution_timeout_seconds
            if execution_timeout_seconds is not None
            else os.environ.get("MATERIALS_MINERU_TIMEOUT_SECONDS", "1800")
        )
        timeout = float(raw_timeout)
        queue_timeout = float(queue_timeout_seconds) if queue_timeout_seconds is not None else None
        if (
            isinstance(raw_timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
            or (
                queue_timeout is not None
                and (isinstance(queue_timeout_seconds, bool) or not math.isfinite(queue_timeout) or queue_timeout <= 0)
            )
        ):
            raise ValueError("positive finite timeout required")
    except (TypeError, ValueError):
        return finish({"status": "not_ready", "error": "invalid_mineru_timeout"})
    work_dir.mkdir(parents=True, exist_ok=True)
    output = work_dir / f"middle_json.{uuid4().hex}.json"
    canonical_output = work_dir / "middle_json.json"
    log_path = work_dir / f"mineru.{uuid4().hex}.log"
    arguments = [
        *command,
        "parse",
        str(source),
        "-o",
        str(output),
        "--format",
        "middle_json",
        "--tier",
        tier,
        "--pages",
        ",".join(map(str, selected)) if selected is not None else "all",
        "--ocr-mode",
        ocr_mode,
    ]
    queue_started = time.monotonic()
    try:
        with _local_parse_slot(cancelled, queue_timeout) as acquired:
            if not acquired or (cancelled and cancelled()):
                return finish({"status": "cancelled", "error": "cancelled_before_start"})
            queued = time.monotonic() - queue_started
            environment = os.environ.copy()
            environment.pop("MINERU_API_URL", None)
            environment.pop("MINERU_API_KEY", None)
            environment["MINERU_HOME"] = str(
                Path(os.environ.get("MATERIALS_MINERU_HOME") or (work_dir / "mineru_home")).expanduser()
            )
            environment["MINERU_MODEL_SOURCE"] = "local"
            with log_path.open("w", encoding="utf-8") as log:
                execution_started = time.monotonic()
                try:
                    process = subprocess.Popen(
                        arguments,
                        cwd=work_dir,
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        text=True,
                    )
                except OSError as exc:
                    execution_ended = time.monotonic()
                    return finish({"status": "not_ready", "error": "mineru_start_failed", "errno": exc.errno})
                try:
                    while process.poll() is None:
                        if cancelled and cancelled():
                            _stop_process(process)
                            execution_ended = time.monotonic()
                            return finish({"status": "cancelled", "error": "cancelled_during_parse"})
                        elapsed = time.monotonic() - execution_started
                        if elapsed >= timeout:
                            _stop_process(process)
                            execution_ended = time.monotonic()
                            return finish({"status": "failed", "error": "mineru_timeout", "timeout_seconds": timeout})
                        time.sleep(min(0.1, max(0, timeout - elapsed)))
                except BaseException:
                    _stop_process(process)
                    raise
                execution_ended = time.monotonic()
    except TimeoutError:
        return finish({"status": "failed", "error": "mineru_queue_timeout", "timeout_seconds": queue_timeout})
    if process.returncode != 0:
        # Detailed CLI output stays in the protected attempt directory. Do not
        # return arbitrary logs, which may contain document text or credentials.
        return finish({"status": "failed", "error": "mineru_exit_nonzero", "returncode": process.returncode})
    if not output.is_file():
        return finish({"status": "failed", "error": "middle_json_missing"})
    try:
        raw = output.read_bytes()
        payload = json.loads(raw)
        digest = hashlib.sha256(raw).hexdigest()
        cache_temp = work_dir / f".middle_json.{uuid4().hex}.tmp"
        try:
            with cache_temp.open("xb") as cache_file:
                cache_file.write(raw)
                cache_file.flush()
                os.fsync(cache_file.fileno())
            cache_temp.replace(canonical_output)
        finally:
            cache_temp.unlink(missing_ok=True)
        result = {"status": "ok", "middle": payload, "output_sha256": digest}
        if include_metrics:
            result["raw_output_path"] = str(output)
            result["actual_parser_mode"] = _parser_mode(payload) if isinstance(payload, dict) else {}
        return finish(result)
    except (ValueError, OSError):
        return finish({"status": "failed", "error": "middle_json_invalid"})
