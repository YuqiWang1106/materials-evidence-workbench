"""Prepare bounded, auditable source packets for one program-only extraction pass."""

from __future__ import annotations

import base64
import json
from collections import defaultdict
from hashlib import sha256
from pathlib import Path

import pymupdf

from .document_pipeline_blocks import ir_blocks
from .document_program_input import _pdf_subset

ESTIMATE_METHOD = (
    "Approximate input tokens: ceil((supplied JSON UTF-8 bytes + native PDF "
    "text UTF-8 bytes)/2) + 4096 visual tokens per original page + "
    "caller instruction/schema allowance; excludes PDF base64 transport bytes"
)


def _projection(block: dict) -> dict:
    row = {key: block[key] for key in ("block_id", "asset_ref", "page", "kind", "source", "text") if key in block}
    if block.get("requires_image_verification"):
        row["requires_image_verification"] = True
    if block.get("reading_risks"):
        row["reading_risks"] = block["reading_risks"]
    return row


def _table_unit(table: dict, cells: list[dict], notes: list[dict]) -> dict:
    row = {key: table[key] for key in ("block_id", "asset_ref", "page", "kind", "source") if key in table}
    for key in ("caption", "captions", "footnote", "footnotes"):
        if table.get(key):
            row[key] = table[key]
    if cells:
        row["grid"] = [
            {
                **{
                    key: cell["anchor"]["table_cell"][key]
                    for key in ("row", "column", "row_span", "col_span")
                    if key in cell["anchor"]["table_cell"]
                },
                "text": cell["text"],
                "block_id": cell["block_id"],
            }
            for cell in cells
        ]
    else:
        row["text"] = table.get("text", "")
    if notes:
        row["linked_notes"] = [_projection(note) for note in notes]
    return row


def prepare_program_inputs(
    original: Path,
    ir: dict,
    output: Path,
    *,
    max_pages: int = 30,
    max_input_tokens: int | None = None,
    request_overhead_tokens: int = 8000,
) -> dict:
    """Return in-memory Responses PDF packets, source registry and coverage manifest.

    Input token estimates are recorded only by default. An explicit finite
    max_input_tokens remains available to bounded offline callers.
    ``output`` is reserved for the caller's run directory. This function writes
    no original, audit, or runtime configuration files.
    """
    if (
        type(max_pages) is not int
        or max_pages < 1
        or (max_input_tokens is not None and (type(max_input_tokens) is not int or max_input_tokens < 1))
        or type(request_overhead_tokens) is not int
        or request_overhead_tokens < 0
    ):
        raise ValueError("positive page and input-token bounds required")
    if len(ir.get("documents", [])) != 1:
        raise ValueError("one original document required")
    document = ir["documents"][0]
    original_bytes = Path(original).read_bytes()
    digest = sha256(original_bytes).hexdigest()
    if digest != document.get("source_sha256"):
        raise ValueError("original / IR source hash mismatch")
    with pymupdf.open(stream=original_bytes, filetype="pdf") as pdf:
        physical = list(range(1, pdf.page_count + 1))
        page_rows = document.get("pages", [])
        if len(page_rows) != len(physical) or sorted(p.get("page") for p in page_rows) != physical:
            raise ValueError("incomplete or duplicated page inventory")
        if any(r.get("page") not in physical for r in document.get("regions", [])):
            raise ValueError("source region outside page inventory")
        blocks = ir_blocks([document])
        registry = {b["block_id"]: b for b in blocks}
        if len(registry) != len(blocks):
            raise ValueError("duplicate block identity")
        # Parser text is the primary source representation. Native text remains
        # in the registry and is used on pages lacking a parser reading.
        parser_pages = {b["page"] for b in blocks if b.get("source") == "mineru" and b.get("kind") != "table_cell"}
        selected = [b for b in blocks if not (b.get("source") == "pymupdf" and b["page"] in parser_pages)]
        region_by_id = {r["id"]: r for r in document.get("regions", [])}
        block_by_region = {b["ir_region"]: b for b in selected if "ir_region" in b and b["kind"] != "table_cell"}
        notes_by_table: dict[str, list[dict]] = defaultdict(list)
        linked_note_ids = set()
        for block in selected:
            region = region_by_id.get(block.get("ir_region"), {})
            table_id = region.get("associated_table_region_id")
            if table_id and table_id in block_by_region and block_by_region[table_id]["kind"] == "table":
                notes_by_table[block_by_region[table_id]["block_id"]].append(block)
                linked_note_ids.add(block["block_id"])
        missing_context_refs = []

        def linked_context(block: dict) -> list[str]:
            region = region_by_id.get(block.get("ir_region"), {})
            refs = [*region.get("shared_method_region_ids", []), *region.get("continuation_region_ids", [])]
            missing_context_refs.extend(
                {
                    "block_id": block["block_id"],
                    "region_id": ref,
                    "reason": "explicit_linked_context_region_unavailable",
                }
                for ref in refs
                if ref not in block_by_region
            )
            return [block_by_region[ref]["block_id"] for ref in refs if ref in block_by_region]

        cells_by_table: dict[str, list[dict]] = defaultdict(list)
        for block in selected:
            if block["kind"] == "table_cell":
                cells_by_table[block["table_region"]].append(block)
        units = []
        for block in selected:
            if block["kind"] == "table_cell" or block["block_id"] in linked_note_ids:
                continue
            if block["kind"] == "table":
                cells = cells_by_table.get(block["block_id"], [])
                notes = notes_by_table.get(block["block_id"], [])
                raw_cells = region_by_id.get(block.get("ir_region"), {}).get("cells", [])
                units.append(
                    {
                        "page": block["page"],
                        "rows": [_table_unit(block, cells, notes)],
                        "pages": sorted({block["page"], *(note["page"] for note in notes)}),
                        "ids": [block["block_id"], *(c["block_id"] for c in cells), *(n["block_id"] for n in notes)],
                        "linked_context_ids": linked_context(block),
                        "kind": "table",
                        "unsafe_reason": "table_grid_incomplete"
                        if raw_cells and len(raw_cells) != len(cells)
                        else None,
                    }
                )
            else:
                units.append(
                    {
                        "page": block["page"],
                        "rows": [_projection(block)],
                        "ids": [block["block_id"]],
                        "linked_context_ids": linked_context(block),
                        "kind": "section",
                    }
                )
        # Every physical page is represented, including scanned and blank pages.
        for page in physical:
            if not any(u["page"] == page for u in units):
                units.append({"page": page, "rows": [], "ids": [], "kind": "visual_page"})
        units.sort(key=lambda u: u["page"])
        # A heading owns the following prose until the next heading. Without a
        # heading, the original physical page is the only justified boundary.
        grouped: list[dict] = []
        current: dict | None = None
        active_heading: str | None = None

        def push_section() -> None:
            nonlocal current
            if current is not None:
                grouped.append(current)
                current = None

        for unit in units:
            row = unit["rows"][0] if unit["rows"] else {}
            title = row.get("kind") in {"title", "doc_title", "paragraph_title", "section_title", "heading"}
            if unit["kind"] != "section":
                push_section()
                if unit["kind"] == "table" and active_heading:
                    unit["linked_context_ids"] = [*unit.get("linked_context_ids", []), active_heading]
                grouped.append(unit)
                continue
            if title:
                push_section()
                active_heading = unit["ids"][0]
            elif current is not None and current.get("heading_id") is None and current["page"] != unit["page"]:
                push_section()
            if current is None:
                current = {**unit, "pages": [unit["page"]], "heading_id": active_heading}
            else:
                current["rows"].extend(unit["rows"])
                current["ids"].extend(unit["ids"])
                current["linked_context_ids"].extend(unit.get("linked_context_ids", []))
                if unit["page"] not in current["pages"]:
                    current["pages"].append(unit["page"])
        push_section()
        units = grouped
        pdf_cache: dict[tuple[int, ...], bytes] = {}
        native_text_bytes = {page: len(pdf[page - 1].get_text().encode("utf-8")) for page in physical}

        def payload(
            rows: list[dict], pages: list[int], target_ids: list[str], context_ids: list[str]
        ) -> tuple[str, bytes, int]:
            text = json.dumps(
                {
                    "source_sha256": digest,
                    "asset_ref": document["asset_ref"],
                    "page_number_rule": "1-based original physical PDF page numbers",
                    "pdf_page_map": {str(i + 1): page for i, page in enumerate(pages)},
                    "target_block_ids": target_ids,
                    "context_block_ids": context_ids,
                    "extraction_scope_rule": (
                        "Extract only target block IDs; context blocks explain targets and are not "
                        "independent extraction targets."
                    ),
                    "source_blocks": rows,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            key = tuple(pages)
            if key not in pdf_cache:
                pdf_cache[key] = _pdf_subset(pdf, pages)
            binary = pdf_cache[key]
            text_bytes = len(text.encode("utf-8")) + sum(native_text_bytes[p] for p in pages)
            estimate = (text_bytes + 1) // 2 + 4096 * len(pages) + request_overhead_tokens
            return text, binary, estimate

        def trial(group: list[dict]) -> tuple[list[int], list[dict], str, bytes, int]:
            pages = sorted({page for unit in group for page in unit.get("pages", [unit["page"]])})
            rows = [row for u in group for row in u["rows"]]
            ids = [block_id for unit in group for block_id in unit["ids"]]
            context_ids = [block_id for unit in group for block_id in unit.get("context_ids", [])]
            text, binary, estimate = payload(rows, pages, ids, context_ids)
            return pages, rows, text, binary, estimate

        def within_input_budget(group: list[dict]) -> bool:
            return max_input_tokens is None or trial(group)[-1] <= max_input_tokens

        unprocessed = []
        omitted_context = []
        packets = []
        pending: list[dict] = []

        def flush() -> None:
            if not pending:
                return
            ids = [block_id for unit in pending for block_id in unit["ids"]]
            context_ids = [block_id for unit in pending for block_id in unit.get("context_ids", [])]
            requested = dict.fromkeys(
                block_id for unit in pending for block_id in unit.get("linked_context_ids", []) if block_id not in ids
            )
            shared = [
                {
                    "page": registry[block_id]["page"],
                    "rows": [_projection(registry[block_id])],
                    "ids": [],
                    "context_ids": [block_id],
                }
                for block_id in requested
            ]
            group = pending[:]
            if shared:
                proposed = [*group, *shared]
                if len(
                    {page for unit in proposed for page in unit.get("pages", [unit["page"]])}
                ) <= max_pages and within_input_budget(proposed):
                    group = proposed
                    context_ids.extend(requested)
                else:
                    omitted_context.append(
                        {
                            "group_id": f"g{len(packets) + 1}",
                            "block_ids": list(requested),
                            "reason": "explicit_linked_context_exceeds_page_or_input_budget",
                        }
                    )
            pages = sorted({page for unit in group for page in unit.get("pages", [unit["page"]])})
            rows = [row for unit in group for row in unit["rows"]]
            text, binary, estimate = payload(rows, pages, ids, context_ids)
            packets.append(
                {
                    "group_id": f"g{len(packets) + 1}",
                    "text": text,
                    "files": [
                        {
                            "filename": "source-pages.pdf",
                            "file_data": "data:application/pdf;base64," + base64.b64encode(binary).decode(),
                            "detail": "high",
                        }
                    ],
                    "images": [],
                    "target_block_ids": ids,
                    "context_block_ids": context_ids,
                    "pages": pages,
                    "estimated_input_tokens": estimate,
                }
            )
            pending.clear()

        for unit in units:
            if unit.get("unsafe_reason"):
                flush()
                unprocessed.append(
                    {
                        "page": unit["page"],
                        "pages": unit.get("pages", [unit["page"]]),
                        "block_ids": unit["ids"],
                        "reason": unit["unsafe_reason"],
                    }
                )
                continue
            candidate = [*pending, unit]
            if len(
                {page for item in candidate for page in item.get("pages", [item["page"]])}
            ) <= max_pages and within_input_budget(candidate):
                pending.append(unit)
                continue
            flush()
            if len(unit.get("pages", [unit["page"]])) <= max_pages and within_input_budget([unit]):
                pending.append(unit)
                continue
            # Split only when source metadata explicitly establishes that each
            # complete row is a sample and identifies its header row.
            if (
                unit["kind"] == "table"
                and unit["rows"][0].get("grid")
                and len(unit.get("pages", [unit["page"]])) <= max_pages
            ):
                table = unit["rows"][0]
                grid = table["grid"]
                region = region_by_id.get(registry[unit["ids"][0]]["ir_region"], {})
                if region.get("sample_axis") != "row" or region.get("header_rows") != [0]:
                    unprocessed.append(
                        {
                            "page": unit["page"],
                            "pages": unit.get("pages", [unit["page"]]),
                            "block_ids": unit["ids"],
                            "reason": "table_orientation_or_header_unverified",
                        }
                    )
                    continue
                rows_by_index: dict[int, list[dict]] = defaultdict(list)
                for cell in grid:
                    if type(cell.get("row")) is not int or type(cell.get("column")) is not int:
                        raise ValueError("table grid requires integer coordinates")
                    rows_by_index[cell["row"]].append(cell)
                ordered = sorted(rows_by_index)
                leading = rows_by_index[ordered[0]]
                note_ids = [note["block_id"] for note in table.get("linked_notes", [])]
                chunk: list[dict] = []

                def split_unit(data: list[dict]) -> dict:
                    entry = {**table, "grid": [*leading, *data] if data and data is not leading else leading}
                    return {
                        "page": unit["page"],
                        "rows": [entry],
                        "pages": unit.get("pages", [unit["page"]]),
                        "ids": [unit["ids"][0], *(c["block_id"] for c in entry["grid"]), *note_ids],
                        "linked_context_ids": unit.get("linked_context_ids", []),
                        "kind": "table",
                    }

                spans_rows = any(cell.get("row_span", 1) != 1 for cell in grid)
                if len(ordered) > 1 and not spans_rows and within_input_budget([split_unit(leading)]):
                    safe = True
                    chunks: list[list[dict]] = []
                    for index in ordered[1:]:
                        row = rows_by_index[index]
                        candidate = split_unit([*chunk, *row])
                        if within_input_budget([candidate]):
                            chunk += row
                        elif chunk:
                            chunks.append(chunk)
                            chunk = row
                            if not within_input_budget([split_unit(chunk)]):
                                safe = False
                                break
                        else:
                            safe = False
                            break
                    if safe:
                        if chunk:
                            chunks.append(chunk)
                        for position, data in enumerate(chunks):
                            part = split_unit(data)
                            if position:
                                shared_ids = [unit["ids"][0], *(c["block_id"] for c in leading), *note_ids]
                                part["ids"] = [c["block_id"] for c in data]
                                part["context_ids"] = shared_ids
                            pending.append(part)
                            flush()
                        continue
            unprocessed.append(
                {
                    "page": unit["page"],
                    "pages": unit.get("pages", [unit["page"]]),
                    "block_ids": unit["ids"],
                    "reason": "table_indivisible_or_exceeds_input_budget"
                    if unit["kind"] == "table"
                    else "complete_section_exceeds_page_or_input_budget"
                    if unit.get("heading_id")
                    else "source_page_exceeds_input_budget",
                }
            )
        flush()
    covered = {page for packet in packets for page in packet["pages"]}
    skipped = {page for row in unprocessed for page in row.get("pages", [row["page"]])}
    manifest = {
        "version": "program-input-v2",
        "source_sha256": digest,
        "ir_sha256": sha256(json.dumps(ir, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "source_pages": physical,
        "estimate_method": ESTIMATE_METHOD,
        "max_pages": max_pages,
        "max_input_tokens": max_input_tokens,
        "input_token_policy": "record_only" if max_input_tokens is None else "bounded",
        "request_overhead_tokens": request_overhead_tokens,
        "page_coverage": [
            {
                "page": page["page"],
                "read_status": page.get("read_status"),
                "parser_status": page.get("parser_status"),
                "pdf_supplied": page["page"] in covered,
                "unprocessed_scope": page["page"] in skipped,
            }
            for page in page_rows
        ],
        "unprocessed": unprocessed,
        "context_not_included": [*omitted_context, *missing_context_refs],
    }
    manifest["input_sha256"] = sha256(
        json.dumps(
            {
                "source": digest,
                "ir": manifest["ir_sha256"],
                "max_pages": max_pages,
                "max_input_tokens": max_input_tokens,
                "request_overhead_tokens": request_overhead_tokens,
                "groups": [
                    (
                        p["pages"],
                        p["target_block_ids"],
                        p["context_block_ids"],
                        sha256(p["text"].encode()).hexdigest(),
                        sha256(base64.b64decode(p["files"][0]["file_data"].split(",", 1)[1])).hexdigest(),
                        p["estimated_input_tokens"],
                    )
                    for p in packets
                ],
                "unprocessed": unprocessed,
                "context_not_included": manifest["context_not_included"],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {"packets": packets, "registry": registry, "manifest": manifest}
