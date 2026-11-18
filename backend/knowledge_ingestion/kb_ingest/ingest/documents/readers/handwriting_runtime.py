"""Subprocess runners for the local handwriting readers."""

from __future__ import annotations

import json
import math
import os
import subprocess
import time

import pymupdf

from ..document_ir import _verified_path
from .handwriting import (
    QUEUE_TIMEOUT_SECONDS,
    READER_SECONDS_PER_PAGE,
    READER_TIMEOUT_SECONDS,
    READER_VERSION,
    RENDER_LONG_SIDE,
    SECOND_READER_BASE_SECONDS,
    SECOND_READER_SECONDS_PER_ITEM,
    SECOND_READER_VERSION,
)
from .mineru import _local_parse_slot, _stop_process


class ReaderError(RuntimeError):
    """The reader did not produce a usable result; `code` is safe to show."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ReaderCancelled(ReaderError):
    def __init__(self):
        super().__init__("reading_cancelled")


def _run(argv, output, log_path, work, *, code, version, cancelled, timeout):
    """Same process discipline as run_mineru: one local parser at a time, own process group, log to file."""
    environment = {**os.environ, "MINERU_MODEL_SOURCE": "local", "HF_HUB_OFFLINE": "1"}
    try:
        # Only the queue raises TimeoutError here; the reader's own limits raise ReaderError.
        with _local_parse_slot(cancelled, QUEUE_TIMEOUT_SECONDS) as acquired:
            if not acquired or (cancelled and cancelled()):
                raise ReaderCancelled()
            with log_path.open("w", encoding="utf-8") as log:
                try:
                    process = subprocess.Popen(
                        argv, cwd=work, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
                    )
                except OSError:
                    raise ReaderError(code + "_start_failed") from None
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if cancelled and cancelled():
                            raise ReaderCancelled()
                        if time.monotonic() >= deadline:
                            raise ReaderError(code + "_timeout")
                        time.sleep(0.2)
                finally:
                    _stop_process(process)
    except TimeoutError:
        raise ReaderError(code + "_queue_timeout") from None
    if process.returncode != 0 or not output.is_file():
        raise ReaderError(code + "_exit_" + str(process.returncode))
    try:
        result = json.loads(output.read_text(encoding="utf-8"))
    except ValueError:
        raise ReaderError(code + "_output_invalid") from None
    if not isinstance(result, dict) or result.get("version") != version:
        raise ReaderError(code + "_output_invalid")
    return result


def render_pages(asset, work, run, numbers=None, long_side=RENDER_LONG_SIDE):
    """One PNG per page (all pages, or the page numbers given), long side about `long_side` pixels; returns
    [{page, image, zoom}]."""
    pages = []
    with pymupdf.open(_verified_path(asset)) as document:
        for index, page in enumerate(document):
            if numbers is not None and index + 1 not in numbers:
                continue
            zoom = min(4.0, long_side / max(page.rect.width, page.rect.height))
            image = work / f"page-{run}-{index + 1}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(str(image))
            pages.append({"page": index + 1, "image": str(image), "zoom": zoom})
    return pages


def run_reader(command, pages, model_root, work, run, *, cancelled=None, timeout=None):
    pages_path, output, log_path = work / f"pages-{run}.json", work / f"reader-{run}.json", work / f"reader-{run}.log"
    if timeout is None:
        # A scanned page with tables takes up to about a minute on a CPU; long documents get time per page.
        timeout = max(READER_TIMEOUT_SECONDS, READER_SECONDS_PER_PAGE * len(pages))
    pages_path.write_text(
        json.dumps(
            [
                {"page": p["page"], "image": p["image"], **({"regions": p["regions"]} if "regions" in p else {})}
                for p in pages
            ]
        ),
        encoding="utf-8",
    )
    result = _run(
        [*command, "--pages", str(pages_path), "--model-root", str(model_root), "--output", str(output)],
        output,
        log_path,
        work,
        code="handwriting_reader",
        version=READER_VERSION,
        cancelled=cancelled,
        timeout=timeout,
    )
    if not isinstance(result.get("pages"), list):
        raise ReaderError("handwriting_reader_output_invalid")
    return result


class SecondReaderTimeout(ReaderError):
    """The second reader ran out of time; `readings` holds what it had saved so far."""

    def __init__(self, readings):
        super().__init__("handwriting_second_reader_timeout")
        self.readings = readings


def _second_readings(result):
    readings = result.get("readings") if isinstance(result, dict) else None
    if not isinstance(readings, dict) or not all(isinstance(v, str) for v in readings.values()):
        raise ReaderError("handwriting_second_reader_output_invalid")
    return readings


def run_second_reader(
    command,
    items,
    model_dir,
    work,
    run,
    *,
    cancelled=None,
    timeout=None,
    seconds_per_item=SECOND_READER_SECONDS_PER_ITEM,
):
    """{item id: text} from the second reader; items it could not read are simply absent. The reader saves its
    output every few items, so a timeout raises SecondReaderTimeout with the readings saved until then."""
    items_path, output, log_path = work / f"items-{run}.json", work / f"second-{run}.json", work / f"second-{run}.log"
    items_path.write_text(json.dumps(items), encoding="utf-8")
    if timeout is None:
        timeout = max(READER_TIMEOUT_SECONDS, SECOND_READER_BASE_SECONDS + seconds_per_item * len(items))
    try:
        result = _run(
            [*command, "--items", str(items_path), "--model-dir", str(model_dir), "--output", str(output)],
            output,
            log_path,
            work,
            code="handwriting_second_reader",
            version=SECOND_READER_VERSION,
            cancelled=cancelled,
            timeout=timeout,
        )
    except ReaderError as exc:
        if exc.code != "handwriting_second_reader_timeout":
            raise
        try:
            saved = json.loads(output.read_text(encoding="utf-8"))
            readings = _second_readings(saved) if saved.get("version") == SECOND_READER_VERSION else {}
        except (OSError, ValueError, AttributeError, ReaderError):
            readings = {}
        raise SecondReaderTimeout(readings) from None
    return _second_readings(result)


def seconds_per_item(value):
    """(allowance, problem): a configured per-item allowance must be a finite number above zero."""
    if value in (None, ""):
        return SECOND_READER_SECONDS_PER_ITEM, None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number) or number <= 0:
        return SECOND_READER_SECONDS_PER_ITEM, {"kind": "handwriting_second_seconds_per_item_invalid"}
    return number, None


def second_pass(
    items, command, model, work, run, *, cancelled=None, allowance=SECOND_READER_SECONDS_PER_ITEM, prefix="handwriting"
):
    """(readings or None, gap or None) of the second reader over `items`; ReaderCancelled propagates."""
    if not command or not model:
        return None, {"kind": prefix + "_second_reader_not_configured"}
    if not items:
        return None, None
    try:
        readings = run_second_reader(command, items, model, work, run, cancelled=cancelled, seconds_per_item=allowance)
    except ReaderCancelled:
        raise
    except SecondReaderTimeout as exc:
        return exc.readings or None, {
            "kind": prefix + "_second_reader_timeout",
            "items": sum(1 for item in items if item["id"] not in exc.readings),
        }
    except ReaderError as exc:
        return None, {"kind": prefix + "_second_reader_failed", "reason": exc.code}
    except (OSError, ValueError, TypeError) as exc:
        return None, {"kind": prefix + "_second_reader_failed", "reason": type(exc).__name__}
    missing = sum(1 for item in items if item["id"] not in readings)
    return readings, ({"kind": prefix + "_second_reader_incomplete", "items": missing} if missing else None)
