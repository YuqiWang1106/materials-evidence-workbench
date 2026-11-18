"""IR block flattening and extraction job packing for document v2."""

from __future__ import annotations

from copy import deepcopy


def _anchor(region, page):
    a = region.get("anchor") or {}
    result = {"kind": "pdf", "page_numbers": [page["page"]]}
    if a.get("bbox"):
        result["coordinate_region"] = {**a, "width": page["width_pt"], "height": page["height_pt"]}
    return result


def ir_blocks(documents):
    result = []
    for d in documents:
        disputed = {
            rid for dispute in d.get("reading_disagreements", []) for rid in dispute.get("affected_region_ids", [])
        }
        for p in d["pages"]:
            regions = [r for r in d["regions"] if r.get("page") == p["page"]]
            # Keep both readers as evidence alternatives, use parser ordering for extraction.
            for r in regions:
                region_key = d["asset_ref"] + ":" + r["id"]
                base = {
                    "block_id": region_key,
                    "asset_ref": d["asset_ref"],
                    "page": p["page"],
                    "kind": r["kind"],
                    "source": r["source"],
                    "text": r.get("text", ""),
                    "anchor": _anchor(r, p),
                    "ir_region": r["id"],
                    "requires_image_verification": r["id"] in disputed or bool(r.get("requires_image_verification")),
                }
                for key in ("reading_sha256", "verification_state", "diagnostics", "reading_risks", "reading"):
                    if r.get(key):
                        base[key] = deepcopy(r[key])
                result.append(
                    {
                        **base,
                        **{
                            key: deepcopy(r[key])
                            for key in ("caption", "captions", "footnote", "footnotes")
                            if r["kind"] == "table" and key in r
                        },
                    }
                )
                for c in r.get("cells", []):
                    if c.get("structure_reliable") is False:
                        continue
                    row = c.get("row", c.get("row_start"))
                    col = c.get("column", c.get("col", c.get("col_start")))
                    if row is None or col is None:
                        continue
                    anchor = deepcopy(base["anchor"])
                    # A reader that measured the cell (handwriting) gives its own box; parser HTML has none.
                    measured = bool(c.get("bbox")) and bool(anchor.get("coordinate_region"))
                    if measured:
                        anchor["coordinate_region"] = {
                            **anchor["coordinate_region"],
                            "bbox": list(c["bbox"]),
                            "precision": "cell_bbox",
                        }
                    anchor["table_cell"] = {
                        "row": row,
                        "column": col,
                        "row_span": c.get("row_span", c.get("rowspan", 1)),
                        "col_span": c.get("col_span", c.get("colspan", 1)),
                        "coordinate_basis": "cell_bbox" if measured else "table_structure_not_cell_bbox",
                    }
                    result.append(
                        {
                            **base,
                            "block_id": region_key + f":cell:{row}:{col}",
                            "kind": "table_cell",
                            "text": c.get("text", ""),
                            "anchor": anchor,
                            "table_region": region_key,
                            **({"reading": deepcopy(c["reading"])} if c.get("reading") else {}),
                        }
                    )
            if not regions and not p.get("blank"):
                result.append(
                    {
                        "block_id": d["asset_ref"] + f":page:{p['page']}:unread",
                        "asset_ref": d["asset_ref"],
                        "page": p["page"],
                        "kind": "unread_page",
                        "source": "visual",
                        "text": "",
                        "anchor": {
                            "kind": "pdf",
                            "page_numbers": [p["page"]],
                            "coordinate_region": {
                                "bbox": [0, 0, p["width_pt"], p["height_pt"]],
                                "width": p["width_pt"],
                                "height": p["height_pt"],
                                "unit": "pt",
                                "origin": "top_left",
                                "precision": "page_bbox",
                            },
                        },
                    }
                )
    # Compact bundle-local IDs; immutable original locators stay in the registry.
    aliases = {b["block_id"]: f"b{i + 1}" for i, b in enumerate(result)}
    for b in result:
        b["block_id"] = aliases[b["block_id"]]
        if b.get("table_region"):
            b["table_region"] = aliases[b["table_region"]]
    return result


def _public_blocks(blocks):
    return [
        {
            k: b.get(k)
            for k in (
                "block_id",
                "asset_ref",
                "page",
                "kind",
                "source",
                "text",
                "table_region",
                "requires_image_verification",
            )
        }
        for b in blocks
    ]


def _pack(blocks, limit):
    """Bounded packets never silently split a source region or table context."""
    groups = {}
    order = []
    for b in blocks:
        key = b.get("table_region", b["block_id"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(b)
    packets = []
    packet = []
    size = 0
    for key in order:
        group = groups[key]
        n = sum(len(b.get("text", "")) + len(b["block_id"]) + 100 for b in group)
        if packet and size + n > limit:
            packets.append(packet)
            packet = []
            size = 0
        packet.extend(group)
        size += n
    if packet:
        packets.append(packet)
    return packets


def extraction_jobs(blocks, labels, limit):
    """Start from actual source groups; narrow subjects only on capacity failure.

    Labels come from document discovery, not an answer set. A source without
    labelled subjects still receives an unscoped job. All groups are visited.
    """
    labels = list(dict.fromkeys(x for x in labels if x))
    scopes = [labels]
    return [
        (i, part, scope)
        for i, (part, scope) in enumerate((part, scope) for part in _pack(blocks, limit) for scope in scopes)
    ]


def split_extraction_job(blocks, labels):
    """Narrow output scope without dropping context or breaking a table family."""
    if len(labels) > 1:
        mid = len(labels) // 2
        return [(blocks, labels[:mid]), (blocks, labels[mid:])]
    groups = _pack(blocks, 1)
    if len(groups) < 2:
        return []
    mid = len(groups) // 2
    return [(sum(groups[:mid], []), labels), (sum(groups[mid:], []), labels)]
