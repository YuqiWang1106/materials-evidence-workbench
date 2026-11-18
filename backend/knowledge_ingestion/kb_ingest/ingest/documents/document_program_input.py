"""Helpers for building bounded document program and standard input packets."""

from __future__ import annotations

import base64
import json
from hashlib import sha256
from pathlib import Path

import pymupdf


def _pdf_subset(pdf: pymupdf.Document, pages: list[int]) -> bytes:
    with pymupdf.open() as subset:
        for number in pages:
            subset.insert_pdf(pdf, from_page=number - 1, to_page=number - 1)
        return subset.tobytes(garbage=4, deflate=True, no_new_id=True)


_RESPONSES_FILE_LIMIT = 50_000_000
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _file_data(filename: str, media_type: str, content: bytes, *, detail: str | None = None) -> dict:
    import base64

    result = {
        "filename": filename,
        "file_data": f"data:{media_type};base64," + base64.b64encode(content).decode("ascii"),
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _safe_middle_pages(
    middle: dict | None, physical_pages: list[int]
) -> tuple[dict | None, dict[int, dict], list[dict]]:
    """Index raw pages by the protocol's zero-based source page index.

    Page dictionaries are not adapted or copied field-by-field: opaque MinerU
    extensions and future block fields remain part of each page object.
    """
    if not isinstance(middle, dict) or not isinstance(middle.get("pages"), list):
        return None, {}, []
    by_page: dict[int, dict] = {}
    unmapped = []
    for position, page in enumerate(middle["pages"]):
        index = page.get("page_idx") if isinstance(page, dict) else None
        page_number = index + 1 if type(index) is int and index >= 0 else None
        if page_number not in physical_pages:
            unmapped.append({"output_index": position, "reason": "page_index_missing_or_out_of_range"})
        elif page_number in by_page:
            unmapped.append({"output_index": position, "reason": "duplicate_page_index", "page": page_number})
        else:
            by_page[page_number] = page
    return middle, by_page, unmapped


def _middle_packet(middle: dict, pages_by_number: dict[int, dict], pages: list[int], *, whole: bool) -> bytes:
    packet = dict(middle)
    packet["pages"] = [pages_by_number[page] for page in pages if page in pages_by_number]
    # The source page objects remain untouched. Only the document-level flag is
    # adjusted so a subset cannot masquerade as a full-document parse.
    if "is_full_document" in packet and not whole:
        packet["is_full_document"] = False
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _referenced_parser_images(
    page_objects: list[dict], *, force_page_unknown: bool = False
) -> list[tuple[str, int | None]]:
    refs: list[tuple[str, int | None]] = []

    def visit(value, page_number):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"image_path", "image"} and isinstance(child, str):
                    suffix = Path(child.split("?", 1)[0]).suffix.lower()
                    if suffix in _IMAGE_TYPES:
                        refs.append((child, page_number))
                else:
                    visit(child, page_number)
        elif isinstance(value, list):
            for child in value:
                visit(child, page_number)

    for page in page_objects:
        index = page.get("page_idx") if isinstance(page, dict) else None
        page_number = index + 1 if type(index) is int and index >= 0 and not force_page_unknown else None
        visit(page, page_number)
    return list(dict.fromkeys(refs))


def _load_page_images(
    document: dict,
    pages: list[int],
    reader_dir: Path | None,
    page_objects: list[dict],
    *,
    unmapped_page_objects: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    attachments: list[dict] = []
    issues: list[dict] = []
    if reader_dir is None:
        reader_root = None
    else:
        reader_root = reader_dir.expanduser().resolve()
    refs = []
    region_by_id = {region.get("id"): region for region in document.get("regions", [])}
    for asset in document.get("image_assets", []):
        region = region_by_id.get(asset.get("region_id"), {})
        if region.get("page") in pages:
            refs.append(
                {
                    "path": asset.get("path"),
                    "asset_id": asset.get("asset_id"),
                    "page": region.get("page"),
                    "origin": asset.get("origin", "parsed_page_image"),
                }
            )
    for reference, page in _referenced_parser_images(page_objects):
        refs.append({"path": reference, "page": page, "origin": "mineru_referenced_image"})
    for reference, _ in _referenced_parser_images(unmapped_page_objects or [], force_page_unknown=True):
        refs.append({"path": reference, "page": None, "origin": "mineru_unmapped_page_image"})
    seen = set()
    for position, item in enumerate(refs, 1):
        rel = item.get("path")
        if not isinstance(rel, str) or reader_root is None:
            issues.append(
                {
                    "page": item.get("page"),
                    "asset_id": item.get("asset_id"),
                    "reason": "image_resource_path_unavailable",
                }
            )
            continue
        candidate = (reader_root / rel).resolve()
        if not candidate.is_relative_to(reader_root) or not candidate.is_file():
            issues.append(
                {
                    "page": item.get("page"),
                    "asset_id": item.get("asset_id"),
                    "reason": "image_resource_not_found_or_outside_reading_cache",
                }
            )
            continue
        raw = candidate.read_bytes()
        digest = sha256(raw).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        mime = _IMAGE_TYPES.get(candidate.suffix.lower())
        if mime is None:
            issues.append(
                {
                    "page": item.get("page"),
                    "asset_id": item.get("asset_id"),
                    "reason": "unsupported_image_resource_type",
                }
            )
            continue
        attachments.append(
            {
                "data": "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii"),
                "filename": f"mineru-image-{position}{candidate.suffix.lower()}",
                "sha256": digest,
                "page": item.get("page"),
                "asset_id": item.get("asset_id"),
                "origin": item["origin"],
                "size": len(raw),
            }
        )
    return attachments, issues


def _reread_packets(
    state_dir: Path | None, document: dict, pages: list[int], source_sha: str
) -> tuple[list[dict], list[dict]]:
    if state_dir is None:
        return [], []
    attempts = (document.get("reread") or {}).get("attempts", [])
    result, issues = [], []
    for attempt in attempts:
        request = attempt.get("request") or {}
        attempt_pages = request.get("pages", [])
        if (
            attempt.get("status") != "ok"
            or request.get("tier") != "standard"
            or request.get("ocr_mode") != "ocr"
            or not set(attempt_pages) & set(pages)
        ):
            continue
        attempt_id = attempt.get("attempt_id")
        if not isinstance(attempt_id, str):
            issues.append({"pages": attempt_pages, "reason": "reread_attempt_identity_missing"})
            continue
        directory = (state_dir / "attempts" / attempt_id).resolve()
        root = state_dir.resolve()
        raw_path = directory / "middle_json.json"
        try:
            if not directory.is_relative_to(root):
                raise ValueError("attempt path outside reread state")
            receipt = json.loads((directory / "request.json").read_text(encoding="utf-8"))
            raw = raw_path.read_bytes()
            identity = receipt.get("identity", {})
            identity_parameters = identity.get("parameters", {})
            if (
                identity.get("source_sha256") != source_sha
                or identity_parameters.get("tier") != "standard"
                or identity_parameters.get("ocr_mode") != "ocr"
                or identity_parameters.get("pages") != attempt_pages
                or sha256(raw).hexdigest() != attempt.get("output_sha256")
            ):
                raise ValueError("reread source or output hash mismatch")
            middle = json.loads(raw)
            output_pages = {
                page.get("page_idx") + 1: page
                for page in middle.get("pages", [])
                if isinstance(page, dict)
                and type(page.get("page_idx")) is int
                and page["page_idx"] + 1 in attempt_pages
                and page["page_idx"] + 1 in pages
            }
            if not output_pages:
                continue
            packet = dict(middle)
            packet["pages"] = [output_pages[number] for number in sorted(output_pages)]
            packet["is_full_document"] = False
            result.append(
                {
                    "attempt_id": attempt_id,
                    "pages": sorted(output_pages),
                    "sha256": sha256(raw).hexdigest(),
                    "bytes": json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    "page_objects": [output_pages[number] for number in sorted(output_pages)],
                    "reader_dir": directory,
                    "filename": f"mineru-standard-ocr-reread-{attempt_id[:12]}.json",
                }
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            issues.append(
                {
                    "attempt_id": attempt_id,
                    "pages": attempt_pages,
                    "reason": "reread_artifact_unavailable_or_unverified",
                    "detail": type(exc).__name__,
                }
            )
    return result, issues
