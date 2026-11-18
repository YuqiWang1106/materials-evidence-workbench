"""Prepare standard (non-program) bounded source packets for document extraction."""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

import pymupdf

from .document_program_input import (
    _RESPONSES_FILE_LIMIT,
    _file_data,
    _load_page_images,
    _middle_packet,
    _pdf_subset,
    _reread_packets,
    _safe_middle_pages,
)


def prepare_standard_inputs(
    original: Path,
    document: dict,
    middle_json_path: Path | None,
    output: Path,
    *,
    reader_dir: Path | None = None,
    reread_state_dir: Path | None = None,
    max_pages: int = 30,
) -> dict:
    """Build page-complete Sol packets from raw MinerU Standard artifacts and PDF.

    This path does not use adapted IR blocks, table reconstruction, token
    ceilings, or semantic parse status as send gates. The original PDF and its
    complete physical-page inventory are the source boundary.
    """
    if type(max_pages) is not int or max_pages < 1 or max_pages > 30:
        raise ValueError("max_pages must be within 1..30")
    if not isinstance(document.get("asset_ref"), str) or not document["asset_ref"].strip():
        raise ValueError("source identity unavailable")
    expected_digest = document.get("source_sha256")
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        raise ValueError("source identity unavailable")
    original = Path(original).expanduser().resolve(strict=True)
    original_bytes = original.read_bytes()
    digest = sha256(original_bytes).hexdigest()
    if digest != expected_digest:
        raise ValueError("source hash mismatch")
    try:
        with pymupdf.open(stream=original_bytes, filetype="pdf") as pdf:
            physical = list(range(1, pdf.page_count + 1))
            page_count = pdf.page_count
            full_pdf_bytes = original_bytes
    except Exception as exc:
        raise ValueError("original PDF is damaged or unreadable") from exc
    page_rows = document.get("pages")
    if (
        not isinstance(page_rows, list)
        or len(page_rows) != page_count
        or any(type(row.get("page")) is not int for row in page_rows if isinstance(row, dict))
        or sorted(row.get("page") for row in page_rows if isinstance(row, dict)) != physical
    ):
        raise ValueError("physical page inventory incomplete or untrusted")
    page_by_number = {row["page"]: row for row in page_rows}

    middle = None
    parser_status = (document.get("parser") or {}).get("status", "unavailable")
    parser_hash = (document.get("parser") or {}).get("middle_sha256")
    parser_issues = []
    unmapped_page_objects = []
    if middle_json_path is not None and Path(middle_json_path).is_file():
        try:
            raw_middle = Path(middle_json_path).read_bytes()
            actual_parser_hash = sha256(raw_middle).hexdigest()
            if not isinstance(parser_hash, str) or actual_parser_hash != parser_hash:
                raise ValueError("parser artifact hash mismatch")
            parsed_middle = json.loads(raw_middle)
            middle, parser_pages, unmapped_pages = _safe_middle_pages(parsed_middle, physical)
            if isinstance(parsed_middle, dict) and isinstance(parsed_middle.get("pages"), list):
                unmapped_page_objects = [
                    parsed_middle["pages"][issue["output_index"]]
                    for issue in unmapped_pages
                    if type(issue.get("output_index")) is int
                    and 0 <= issue["output_index"] < len(parsed_middle["pages"])
                ]
            if middle is not None:
                parser_hash = actual_parser_hash
            if unmapped_pages:
                parser_issues.extend(unmapped_pages)
        except (OSError, ValueError, json.JSONDecodeError):
            parser_pages = {}
            parser_issues.append({"reason": "middle_json_unavailable_or_unverified"})
            middle = None
    else:
        parser_pages = {}
        parser_issues.append({"reason": "middle_json_not_available"})
    if middle is None:
        parser_pages = {}

    rereads, reread_issues = _reread_packets(
        Path(reread_state_dir) if reread_state_dir else None, document, physical, digest
    )
    page_images, image_issues = _load_page_images(
        document,
        physical,
        Path(reader_dir) if reader_dir else None,
        [parser_pages[p] for p in physical if p in parser_pages],
        unmapped_page_objects=unmapped_page_objects,
    )
    images_by_page: dict[int | None, list[dict]] = defaultdict(list)
    for image in page_images:
        images_by_page[image["page"]].append(image)
    # Unmapped MinerU references stay with the first packet that carries the
    # separately labelled, page-unbound parser records.
    global_images = images_by_page.pop(None, [])
    rereads_by_page: dict[int, list[dict]] = defaultdict(list)
    for reread in rereads:
        for page in reread["pages"]:
            rereads_by_page[page].append(reread)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    packets = []
    unprocessed = []
    # First obey the 30-page source boundary; split a window further only to
    # satisfy the Responses 50 MB total attachment limit.
    windows = [physical[start : start + max_pages] for start in range(0, page_count, max_pages)]

    def build_packet(pages: list[int], *, force_subset: bool = False) -> dict:
        page_objects = [parser_pages[p] for p in pages if p in parser_pages]
        whole = pages == physical and page_count <= max_pages and not force_subset
        if whole:
            pdf_bytes = full_pdf_bytes
            pdf_name = "source-original.pdf"
        else:
            with pymupdf.open(stream=original_bytes, filetype="pdf") as pdf:
                pdf_bytes = _pdf_subset(pdf, pages)
            pdf_name = f"source-pages-{pages[0]:04d}-{pages[-1]:04d}.pdf"
        files = [_file_data(pdf_name, "application/pdf", pdf_bytes, detail="high")]
        used_hashes = set()
        if middle is not None and page_objects:
            middle_bytes = _middle_packet(
                middle, parser_pages, pages, whole=whole and set(parser_pages) == set(physical)
            )
            files.append(
                _file_data(f"mineru-middle-pages-{pages[0]:04d}-{pages[-1]:04d}.json", "application/json", middle_bytes)
            )
        if pages[0] == physical[0] and unmapped_page_objects:
            unbound_bytes = json.dumps(
                {
                    "schema": middle.get("schema") if middle else None,
                    "schema_version": middle.get("schema_version") if middle else None,
                    "source_artifact_sha256": parser_hash,
                    "unmapped_page_records": unmapped_page_objects,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            files.append(_file_data("mineru-unmapped-page-records.json", "application/json", unbound_bytes))
        selected_rereads = {item["attempt_id"]: item for page in pages for item in rereads_by_page.get(page, [])}
        reread_records = []
        reread_images = []
        reread_image_issues = []
        for item in selected_rereads.values():
            selected_pages = [page for page in item["pages"] if page in pages]
            if not selected_pages:
                continue
            # Reread JSON is prepared for an OCR request's exact page scope;
            # repeat only the pages intersecting this packet and preserve them.
            raw_path = Path(reread_state_dir) / "attempts" / item["attempt_id"] / "middle_json.json"
            reread_middle = json.loads(raw_path.read_text(encoding="utf-8"))
            source_by_page = {
                row.get("page_idx") + 1: row
                for row in reread_middle.get("pages", [])
                if isinstance(row, dict) and type(row.get("page_idx")) is int
            }
            reread_packet = dict(reread_middle)
            reread_packet["pages"] = [source_by_page[p] for p in selected_pages if p in source_by_page]
            reread_packet["is_full_document"] = False
            reread_bytes = json.dumps(reread_packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            files.append(
                _file_data(
                    item["filename"].replace(".json", f"-pages-{selected_pages[0]:04d}-{selected_pages[-1]:04d}.json"),
                    "application/json",
                    reread_bytes,
                )
            )
            reread_page_objects = [
                row
                for row in item["page_objects"]
                if type(row.get("page_idx")) is int and row["page_idx"] + 1 in selected_pages
            ]
            reread_page_images, reread_page_issues = _load_page_images(
                {}, selected_pages, item["reader_dir"], reread_page_objects
            )
            reread_images.extend(reread_page_images)
            reread_image_issues.extend(reread_page_issues)
            reread_records.append(
                {
                    "attempt_id": item["attempt_id"],
                    "pages": selected_pages,
                    "source_output_sha256": item["sha256"],
                    "filename": files[-1]["filename"],
                    "image_resources": [image["filename"] for image in reread_page_images],
                }
            )
        images = []
        unknown_page_images = global_images if pages[0] == physical[0] else []
        for image in [
            *unknown_page_images,
            *(item for page in pages for item in images_by_page.get(page, [])),
            *reread_images,
        ]:
            if image["sha256"] in used_hashes:
                continue
            used_hashes.add(image["sha256"])
            images.append(image)
        total = sum(len(base64.b64decode(file["file_data"].split(",", 1)[1])) for file in files)
        total += sum(image["size"] for image in images)
        return {
            "files": files,
            "image_records": images,
            "total_attachment_bytes": total,
            "page_objects": page_objects,
            "whole_pdf": whole,
            "attachment_gaps": [],
            "reread_sources": reread_records,
            "reread_image_issues": reread_image_issues,
        }

    def append_sized(pages: list[int]) -> None:
        candidate = build_packet(pages)
        if candidate["total_attachment_bytes"] <= _RESPONSES_FILE_LIMIT:
            packets.append((pages, candidate))
            return
        if len(pages) > 1:
            middle_point = len(pages) // 2
            append_sized(pages[:middle_point])
            append_sized(pages[middle_point:])
            return
        # Optional imagery can be omitted only with an explicit page-level
        # notice. The PDF and raw parse remain the first priority.
        without_images = {
            **candidate,
            "image_records": [],
            "total_attachment_bytes": sum(
                len(base64.b64decode(f["file_data"].split(",", 1)[1])) for f in candidate["files"]
            ),
        }
        if without_images["total_attachment_bytes"] <= _RESPONSES_FILE_LIMIT:
            candidate = without_images
            candidate["attachment_gaps"] = [
                {"pages": pages, "reason": "optional_image_resources_exceeded_combined_attachment_limit"}
            ]
            packets.append((pages, candidate))
            return
        if len(candidate["files"]) > 1:
            # A single oversized parse attachment cannot be transferred by
            # this API. Keep the PDF usable and report the exact omission.
            pdf_only = {
                **candidate,
                "files": candidate["files"][:1],
                "image_records": [],
                "total_attachment_bytes": len(base64.b64decode(candidate["files"][0]["file_data"].split(",", 1)[1])),
            }
            if pdf_only["total_attachment_bytes"] <= _RESPONSES_FILE_LIMIT:
                packets.append((pages, pdf_only))
                unprocessed.append({"pages": pages, "reason": "mineru_artifact_exceeds_api_attachment_limit"})
                return
        unprocessed.append({"pages": pages, "reason": "source_page_exceeds_api_attachment_limit"})

    for window in windows:
        append_sized(window)
    source_gaps = document.get("gaps", [])
    page_coverage = []
    for page in physical:
        containing = [(pages, row) for pages, row in packets if page in pages]
        page_coverage.append(
            {
                "page": page,
                "read_status": page_by_number[page].get("read_status"),
                "parser_status": page_by_number[page].get("parser_status"),
                "status": "sent" if containing else "not_sent",
                "pdf_supplied": bool(containing),
                "mineru_page_supplied": any(
                    page in parser_pages
                    and any(file["filename"].startswith("mineru-middle-pages-") for file in packet["files"])
                    for _, packet in containing
                ),
                "gaps": [gap for gap in source_gaps if gap.get("page") in {None, page}],
            }
        )
    for index, (pages, payload) in enumerate(packets, 1):
        page_map = [
            {"packet_pdf_page": position + 1, "original_physical_page": page} for position, page in enumerate(pages)
        ]
        attachment_rows = [
            {
                "filename": f["filename"],
                "media_type": f["file_data"].split(";", 1)[0][5:],
                "bytes": len(base64.b64decode(f["file_data"].split(",", 1)[1])),
            }
            for f in payload["files"]
        ]
        image_rows = [
            {
                "filename": item["filename"],
                "page": item["page"],
                "asset_id": item["asset_id"],
                "origin": item["origin"],
                "sha256": item["sha256"],
                "bytes": item["size"],
            }
            for item in payload["image_records"]
        ]
        page_rows_in_group = [
            {
                "page": page,
                "read_status": page_by_number[page].get("read_status"),
                "parser_status": page_by_number[page].get("parser_status"),
            }
            for page in pages
        ]
        issues = [gap for gap in source_gaps if gap.get("page") in {None, *pages}]
        manifest = {
            "source": {"asset_ref": document["asset_ref"], "sha256": digest, "original_name": original.name},
            "group": {
                "id": f"g{index}",
                "original_pages": pages,
                "pdf_page_map": page_map,
                "middle_page_map": [
                    {"middle_page_idx": page - 1, "original_physical_page": page}
                    for page in pages
                    if page in parser_pages
                ],
                "pdf_is_original": payload["whole_pdf"],
            },
            "mineru": {
                "status": parser_status,
                "version": (document.get("parser") or {}).get("producer", {}).get("version"),
                "tier": (document.get("parser") or {}).get("mode", {}).get("tier", "standard"),
                "source_artifact_sha256": parser_hash,
                "attached_pages": [p for p in pages if p in parser_pages],
                "missing_or_unmapped_page_records": parser_issues,
                "unmapped_records_attached_separately": bool(
                    unmapped_page_objects
                    and any(row["filename"] == "mineru-unmapped-page-records.json" for row in attachment_rows)
                ),
            },
            "page_status": page_rows_in_group,
            "reading_gaps": issues,
            "reread_files": [row for row in attachment_rows if "reread" in row["filename"]],
            "reread_sources": payload.get("reread_sources", []),
            "image_resources": image_rows,
            "image_resource_gaps": [
                issue
                for issue in [*image_issues, *payload.get("reread_image_issues", [])]
                if issue.get("page") in {None, *pages}
            ],
            "attachment_gaps": [
                *payload.get("attachment_gaps", []),
                *[issue for issue in unprocessed if set(issue.get("pages", [])) & set(pages)],
            ],
            "reread_gaps": [issue for issue in reread_issues if set(issue.get("pages", pages)) & set(pages)],
            "attachments": attachment_rows,
            "input_policy": "no_local_token_limit; actual usage is recorded by the model ledger",
        }
        packets[index - 1] = (pages, payload | {"manifest": manifest})
    packet_rows = []
    for index, (pages, payload) in enumerate(packets, 1):
        manifest_json = json.dumps(payload["manifest"], ensure_ascii=False, separators=(",", ":"))
        text = (
            "Source manifest (metadata and page mapping only; scientific content is in the attached files).\n"
            + manifest_json
            + "\nRead all supplied MinerU and original-PDF material. If they disagree or parsing is incomplete, "
            "retain the uncertainty and cite the original physical page; do not silently omit readable content."
            + (
                " Records in mineru-unmapped-page-records.json have no reliable physical-page mapping; do not "
                "invent a page or use them to force sample ownership."
                if any(file["filename"] == "mineru-unmapped-page-records.json" for file in payload["files"])
                else ""
            )
        )
        packet_rows.append(
            {
                "group_id": f"g{index}",
                "text": text,
                "files": payload["files"],
                "images": [item["data"] for item in payload["image_records"]],
                "pages": pages,
                "target_block_ids": [],
                "context_block_ids": [],
                "estimated_input_tokens": None,
                "actual_input_tokens": None,
                "manifest": payload["manifest"],
            }
        )
    manifest = {
        "version": "standard-files-v1",
        "source_sha256": digest,
        "source_pages": physical,
        "max_pages": max_pages,
        "input_token_policy": "record_actual_usage",
        "parser_status": parser_status,
        "parser_artifact_sha256": parser_hash,
        "page_coverage": page_coverage,
        "unprocessed": unprocessed,
        "reading_gaps": source_gaps,
        "parser_artifact_issues": parser_issues,
        "image_resource_issues": image_issues,
        "reread_issues": reread_issues,
        "input_strategy": "raw_middle_json_plus_source_pdf_plus_minimal_manifest",
    }
    manifest["input_sha256"] = sha256(
        json.dumps(
            {
                "source": digest,
                "packets": [
                    {
                        "pages": p["pages"],
                        "manifest": p["manifest"],
                        "files": [sha256(f["file_data"].encode()).hexdigest() for f in p["files"]],
                        "images": [sha256(i.encode()).hexdigest() for i in p["images"]],
                    }
                    for p in packet_rows
                ],
                "unprocessed": unprocessed,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {"packets": packet_rows, "registry": {}, "manifest": manifest}
