"""Display locators use real isolated PDFs; no OCR, provider or runtime config."""

import hashlib
from pathlib import Path

import pymupdf
import pytest


def _locator(*args, **kwargs):
    from kb_ingest import document_evidence_location

    return document_evidence_location.EvidenceLocator(*args, **kwargs)


def _asset(path, asset_id="a"):
    return {"asset_id": asset_id, "local_path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _pdf(tmp_path, lines=("Unique tensile strength 12.5 MPa",), *, rotate=0, crop=False):
    path = tmp_path / "input.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        for i, line in enumerate(lines):
            page.insert_text((60, 75 + i * 25), line)
        if crop:
            page.set_cropbox(pymupdf.Rect(40, 40, 360, 270))
        page.set_rotation(rotate)
        doc.new_page(width=400, height=300).insert_text((60, 75), "Other source page")
        doc.save(path)
    return _asset(path)


def _block(
    text="Unique tensile strength 12.5 MPa",
    *,
    block_id="b1",
    asset="a",
    page=1,
    bbox=(30, 40, 300, 180),
    kind="text",
    source="mineru",
    **extra,
):
    return {
        "block_id": block_id,
        "asset_ref": asset,
        "page": page,
        "kind": kind,
        "source": source,
        "text": text,
        "ir_region": block_id,
        "anchor": {
            "kind": "pdf",
            "page_numbers": [page],
            "coordinate_region": {
                "bbox": list(bbox),
                "unit": "pt",
                "origin": "top_left",
                "coordinate_space": "rendered_page",
                "width": 400,
                "height": 300,
                "precision": "table_bbox" if kind.startswith("table") else "block_bbox",
            },
        },
        **extra,
    }


def _locate(asset, evidence, blocks=()):
    with _locator({"a": asset}, {b["block_id"]: b for b in blocks}, {"a": {1, 2}}) as locator:
        return locator.locate(evidence)


def _page(result, reason=None):
    assert result["status"] == "page_only"
    assert result["precision"] == "page"
    assert result["highlight_regions"] == []
    if reason:
        assert reason in result["location_reason"]


def test_native_multiline_unique_quote_uses_separate_character_line_boxes(tmp_path):
    asset = _pdf(tmp_path, ("Unique tensile strength", "equals 12.5 MPa today"))
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "tensile strength equals 12.5 MPa"})
    assert result["status"] == "located"
    assert result["precision"] == "text"
    assert result["location_method"] == "native_text_match"
    assert len(result["highlight_regions"]) == 2
    assert result["highlight_regions"][0]["bbox"][0] > 60
    assert result["anchor"]["kind"] == "pdf"
    for region in result["highlight_regions"]:
        assert region["page"] == 1 and region["unit"] == "pt"
        assert region["origin"] == "top_left" and region["coordinate_space"] == "rendered_page"


@pytest.mark.parametrize(
    "quote,lines,reason",
    [
        ("Repeat tensile strength", ("Repeat tensile strength", "Repeat tensile strength"), "ambiguous"),
        ("12.5", ("Measured 12.5 MPa",), "short_quote"),
        ("", ("Unique tensile strength 12.5 MPa",), "empty_quote"),
        ("Unique tensile strength 13.5 MPa", ("Unique tensile strength 12.5 MPa",), "quote_not_found"),
        ("12.5 MPa", ("Measured 112.5 MPa",), "quote_not_found"),
        ("123 MPa", ("Measured 1 23 MPa",), "quote_not_found"),
    ],
)
def test_unsafe_or_unmatched_native_quote_stays_page_only(tmp_path, quote, lines, reason):
    _page(_locate(_pdf(tmp_path, lines), {"asset_ref": "a", "page": 1, "quote": quote}), reason)


def test_does_not_search_different_page(tmp_path):
    _page(
        _locate(_pdf(tmp_path), {"asset_ref": "a", "page": 2, "quote": "Unique tensile strength 12.5 MPa"}),
        "quote_not_found",
    )


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotated_cropped_native_boxes_match_rendered_page_bounds(tmp_path, rotate):
    asset = _pdf(tmp_path, rotate=rotate, crop=True)
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Unique tensile strength"})
    assert result["precision"] == "text"
    with pymupdf.open(asset["local_path"]) as doc:
        page = doc[0]
        expected = page.search_for("Unique tensile strength")[0] * page.rotation_matrix
        region = result["highlight_regions"][0]
        assert region["bbox"] == pytest.approx(list(expected), abs=0.01)
        assert region["width"] == page.rect.width
        assert region["height"] == page.rect.height


def test_scanned_page_html_entity_quote_has_table_region_without_fabricated_cell(tmp_path):
    asset = _pdf(tmp_path, ())
    table = _block("<table><tr><td>Sample &amp; A</td><td>12.5 MPa</td></tr></table>", kind="table")
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Sample & A 12.5 MPa"}, [table])
    assert result["status"] == "located" and result["precision"] == "region"
    assert result["location_method"] == "mineru_region_match"
    assert result["location_reason"] == "table_region"
    assert result["highlight_regions"][0]["bbox"] == [30, 40, 300, 180]


def test_table_parent_cell_same_physical_region_not_false_ambiguity(tmp_path):
    asset = _pdf(tmp_path, ())
    table = _block("Measured tensile strength 12.5 MPa", kind="table")
    cell = _block(
        "Measured tensile strength 12.5 MPa", block_id="b2", kind="table_cell", table_region="b1", ir_region="b1"
    )
    cell["anchor"]["table_cell"] = {"row": 1, "column": 2, "coordinate_basis": "table_structure_not_cell_bbox"}
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": table["text"]}, [table, cell])
    assert result["precision"] == "region" and len(result["highlight_regions"]) == 1
    assert result["location_reason"] == "table_region"


@pytest.mark.parametrize(
    "bbox", [[float("nan"), 0, 20, 30], [100, 20, 50, 90], [0, 0, 0, 30], [-20, 20, 100, 100], [10, 20, 500, 100]]
)
def test_invalid_ir_coordinates_never_produce_highlights(tmp_path, bbox):
    asset = _pdf(tmp_path, ())
    block = _block(bbox=bbox)
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": block["text"]}, [block]), "invalid_region_coordinates")


@pytest.mark.parametrize("field,value", [("asset_ref", "other"), ("page", 2)])
def test_explicit_block_identity_conflict_does_not_use_block_box(tmp_path, field, value):
    asset = _pdf(tmp_path)
    item = {"asset_ref": "a", "page": 1, "block_id": "b1", "quote": "Unique tensile strength"}
    item[field] = value
    result = _locate(asset, item, [_block()])
    assert result["status"] in {"page_only", "not_found"}
    assert result["highlight_regions"] == [] and "conflict" in result["location_reason"]


def test_hash_mismatch_and_corrupt_pdf_never_use_ir_highlights(tmp_path):
    asset = _pdf(tmp_path)
    asset["sha256"] = "0" * 64
    _page(_locate(asset, {"block_id": "b1", "quote": "Unique tensile strength"}, [_block()]), "source_hash_mismatch")
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a PDF")
    _page(
        _locate(_asset(broken), {"block_id": "b1", "quote": "Unique tensile strength"}, [_block()]),
        "source_pdf_unreadable",
    )


def test_legacy_block_with_empty_quote_uses_region_without_claiming_text_match(tmp_path):
    asset = _pdf(tmp_path, ())
    result = _locate(asset, {"block_id": "b1", "quote": ""}, [_block()])
    assert result["precision"] == "region"
    assert result["location_method"] == "legacy_block_anchor"
    assert result["location_reason"] == "empty_quote_block_anchor"


def test_existing_block_does_not_falsely_match_changed_quote(tmp_path):
    asset = _pdf(tmp_path, ())
    _page(_locate(asset, {"block_id": "b1", "quote": "Different tensile strength"}, [_block()]), "quote_not_found")


def test_multiple_mineru_regions_or_duplicate_region_text_is_ambiguous(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block()
    b = _block(block_id="b2", bbox=(30, 185, 300, 230))
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": a["text"]}, [a, b]), "ambiguous")
    a["text"] = a["text"] + " " + a["text"]
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": b["text"]}, [a]), "ambiguous")


def test_unknown_page_and_unknown_asset_are_not_found(tmp_path):
    asset = _pdf(tmp_path)
    for item in (
        {"asset_ref": "missing", "page": 1},
        {"asset_ref": "a", "page": 7},
        {"asset_ref": "a", "page": True},
        {"block_id": "missing"},
    ):
        result = _locate(asset, item)
        assert result["status"] == "not_found" and result["highlight_regions"] == []


def test_session_hashes_opens_pdf_and_indexes_page_once(tmp_path, monkeypatch):
    asset = _pdf(tmp_path)
    from kb_ingest import document_evidence_location as module

    reads, opens, indexes = [], [], []
    read_bytes, pdf_open, get_text = Path.read_bytes, module.pymupdf.open, pymupdf.Page.get_text

    def read(path):
        reads.append(path)
        return read_bytes(path)

    def open_pdf(*args, **kwargs):
        opens.append(args)
        return pdf_open(*args, **kwargs)

    def text(page, *args, **kwargs):
        indexes.append(page.number)
        return get_text(page, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(module.pymupdf, "open", open_pdf)
    monkeypatch.setattr(pymupdf.Page, "get_text", text)
    with _locator({"a": asset}, {}, {"a": {1, 2}}) as locator:
        for _ in range(4):
            assert (
                locator.locate({"asset_ref": "a", "page": 1, "quote": "Unique tensile strength"})["precision"] == "text"
            )
    assert len(reads) == len(opens) == len(indexes) == 1


def test_real_pdf_inventory_fallback_when_derived_ir_is_missing(tmp_path):
    asset = _pdf(tmp_path)
    with _locator({"a": asset}, {}, {}) as locator:
        result = locator.locate({"asset_ref": "a", "page": 1, "quote": "Different unrecognized text"})
        _page(result, "quote_not_found")
        result = locator.locate({"asset_ref": "a", "page": 1, "quote": "Unique tensile strength"})
        assert result["precision"] == "text"


@pytest.mark.parametrize(
    "mutation",
    [
        {"page": 2},
        {"origin": "bottom_left"},
        {"coordinate_space": "unrotated_pdf"},
        {"width": 500},
        {"precision": "unknown"},
    ],
)
def test_region_contract_conflicts_do_not_generate_coords(tmp_path, mutation):
    asset = _pdf(tmp_path, ())
    block = _block()
    block["anchor"]["coordinate_region"].update(mutation)
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": block["text"]}, [block]), "invalid_region_coordinates")


def test_malformed_legacy_anchor_does_not_crash(tmp_path):
    asset = _pdf(tmp_path, ())
    block = _block()
    block["anchor"]["page_numbers"] = None
    _page(_locate(asset, {"block_id": "b1"}, [block]), "empty_quote")


def test_unicode_ligatures_and_layout_whitespace_match_without_changing_numbers(tmp_path):
    asset = _pdf(tmp_path, ())
    block = _block("The e\ufb03cient\u00a0sample\nmeasured \u221212.5 MPa")
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "The efficient sample measured -12.5 MPa"}, [block])
    assert result["precision"] == "region"
    _page(
        _locate(asset, {"asset_ref": "a", "page": 1, "quote": "sample measured 12.5 MPa"}, [block]), "quote_not_found"
    )


def test_attached_mineru_caption_cannot_borrow_figure_region_geometry(tmp_path):
    asset = _pdf(tmp_path, ())
    block = _block("", kind="image", caption="<p>Figure 1. Sample &amp; reference behavior</p>")
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Sample & reference behavior"}, [block])
    _page(result, "quote_not_found")


def test_registered_asset_binding_alias_can_locate_verified_original(tmp_path):
    asset = _pdf(tmp_path)
    asset["asset_id"] = "other"
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Unique tensile strength"})
    assert result["asset_ref"] == "a" and result["precision"] == "text"


def test_mineru_html_inline_tags_and_entities_keep_numeric_value(tmp_path):
    asset = _pdf(tmp_path, ())
    block = _block("Mg<sub>2</sub> sample &amp; reference: 12.5 MPa")
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Mg2 sample & reference: 12.5 MPa"}, [block])
    assert result["precision"] == "region"


def test_exact_long_fragments_of_spliced_quote_get_explicit_partial_location(tmp_path):
    asset = _pdf(
        tmp_path,
        (
            "The sample was heated at 450 C",
            "Intervening original unrelated paragraph",
            "The tensile strength reached 12.5 MPa",
        ),
    )
    quote = "Claim 1: The sample was heated at 450 C; The tensile strength reached 12.5 MPa"
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": quote})
    assert result["status"] == "located" and result["precision"] == "text"
    assert result["location_method"] == "native_text_segments"
    assert result["partial_match"] is True
    assert len(result["highlight_regions"]) == 2
    assert [item["quote"] for item in result["matched_fragments"]] == [
        "The sample was heated at 450 C",
        "The tensile strength reached 12.5 MPa",
    ]
    assert result["unmatched_fragments"] == [{"quote": "Claim 1", "reason": "short_fragment"}]
    assert [item["highlight_region_indexes"] for item in result["matched_fragments"]] == [[0], [1]]


def test_partial_segments_never_fuzzy_match_changed_numbers(tmp_path):
    asset = _pdf(tmp_path, ("The sample was heated at 450 C", "The strength reached 12.5 MPa"))
    result = _locate(
        asset, {"asset_ref": "a", "page": 1, "quote": "The sample was heated at 450 C；The strength reached 13.5 MPa。"}
    )
    assert result["location_method"] == "native_text_segments"
    assert len(result["highlight_regions"]) == 1
    assert result["unmatched_fragments"] == [{"quote": "The strength reached 13.5 MPa", "reason": "quote_not_found"}]


def test_ambiguous_fragment_is_recorded_and_never_highlighted(tmp_path):
    asset = _pdf(
        tmp_path,
        ("Repeated long original sentence", "Repeated long original sentence", "A unique long original sentence"),
    )
    result = _locate(
        asset,
        {"asset_ref": "a", "page": 1, "quote": "Repeated long original sentence; A unique long original sentence"},
    )
    assert result["location_method"] == "native_text_segments"
    assert len(result["highlight_regions"]) == 1
    assert result["unmatched_fragments"] == [
        {"quote": "Repeated long original sentence", "reason": "ambiguous_fragment"}
    ]


def test_fully_ambiguous_quote_never_attempts_partial_segments(tmp_path):
    asset = _pdf(
        tmp_path,
        ("Long original sentence; Another original sentence", "Long original sentence; Another original sentence"),
    )
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Long original sentence; Another original sentence"})
    _page(result, "ambiguous_native_text_match")
    assert result.get("partial_match", False) is False


def test_duplicate_quote_fragments_are_not_selected_twice(tmp_path):
    asset = _pdf(tmp_path, ("The unique original sentence appears once",))
    quote = "The unique original sentence appears once; The unique original sentence appears once"
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": quote}), "quote_not_found")


def test_short_fragments_and_pure_numbers_cannot_create_partial_location(tmp_path):
    asset = _pdf(tmp_path, ("Small value 12.5 MPa", "1234567890123456789"))
    _page(
        _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Claim: 12.5 MPa; 1234567890123456789; Small value"}),
        "quote_not_found",
    )


def test_partial_locator_stays_on_declared_page(tmp_path):
    asset = _pdf(tmp_path, ("The sample was heated at 450 C",))
    _page(
        _locate(asset, {"asset_ref": "a", "page": 2, "quote": "Claim 1: The sample was heated at 450 C"}),
        "quote_not_found",
    )


def test_scanned_mineru_prefix_returns_explicit_region_partial_match(tmp_path):
    asset = _pdf(tmp_path, ())
    block = _block("The sample was heated at 450 C")
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Claim 1: The sample was heated at 450 C"}, [block])
    assert result["location_method"] == "mineru_region_segments"
    assert result["precision"] == "region" and result["partial_match"] is True


def test_exact_full_quote_keeps_full_matching_method(tmp_path):
    asset = _pdf(tmp_path, ("The sample was heated at 450 C; The strength reached 12.5 MPa",))
    result = _locate(
        asset, {"asset_ref": "a", "page": 1, "quote": "The sample was heated at 450 C; The strength reached 12.5 MPa"}
    )
    assert result["location_method"] == "native_text_match"
    assert result.get("partial_match", False) is False


@pytest.mark.parametrize("field,value", [("page", []), ("asset_ref", [])])
def test_malformed_ir_block_identity_cannot_crash_session(tmp_path, field, value):
    asset = _pdf(tmp_path)
    block = _block()
    block[field] = value
    with _locator({"a": asset}, {"b1": block}, {"a": {1, 2}}) as locator:
        result = locator.locate({"asset_ref": "a", "page": 1, "quote": "Unique tensile strength"})
        assert result["precision"] == "text"


@pytest.mark.parametrize(
    "item",
    [{"asset_ref": [], "page": 1}, {"asset_ref": "a", "page": []}, {"asset_ref": {}, "page": {}}, {"block_id": []}],
)
def test_malformed_evidence_identity_safely_returns_not_found(tmp_path, item):
    result = _locate(_pdf(tmp_path), item)
    assert result["status"] == "not_found" and result["highlight_regions"] == []


def test_chinese_long_sentence_segments_and_claim_prefix_are_explicit_partial(tmp_path):
    path = tmp_path / "chinese.pdf"
    sentence_a = "所述复合材料的制备方法包括高温烧结步骤"
    sentence_b = "所述试样在室温条件下进行了拉伸性能测试"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        page.insert_text((30, 60), sentence_a, fontname="china-s")
        page.insert_text((30, 100), "中间原文说明", fontname="china-s")
        page.insert_text((30, 140), sentence_b, fontname="china-s")
        doc.save(path)
    evidence = {"asset_ref": "a", "page": 1, "quote": "权利要求1：" + sentence_a + "；" + sentence_b + "。"}
    original_evidence = dict(evidence)
    result = _locate(_asset(path), evidence)
    assert result["location_method"] == "native_text_segments"
    assert result["partial_match"] is True
    assert len(result["highlight_regions"]) == 2
    assert [part["quote"] for part in result["matched_fragments"]] == [sentence_a, sentence_b]
    assert result["unmatched_fragments"] == [{"quote": "权利要求1", "reason": "short_fragment"}]
    assert evidence == original_evidence


def test_scanned_spliced_quote_locates_unique_long_mineru_regions(tmp_path):
    asset = _pdf(tmp_path, ())
    first = _block("The sample was heated at 450 C", bbox=(20, 30, 350, 90))
    second = _block("The tensile strength reached 12.5 MPa", block_id="b2", bbox=(20, 140, 350, 200))
    result = _locate(
        asset,
        {"asset_ref": "a", "page": 1, "quote": "Claim 1: " + first["text"] + "; " + second["text"]},
        [first, second],
    )
    assert result["status"] == "located" and result["precision"] == "region"
    assert result["location_method"] == "mineru_region_segments"
    assert result["partial_match"] is True
    assert [box["bbox"] for box in result["highlight_regions"]] == [
        first["anchor"]["coordinate_region"]["bbox"],
        second["anchor"]["coordinate_region"]["bbox"],
    ]
    assert [part["highlight_region_indexes"] for part in result["matched_fragments"]] == [[0], [1]]
    assert result["unmatched_fragments"] == [{"quote": "Claim 1", "reason": "short_fragment"}]


def test_scanned_partial_does_not_match_changed_numbers(tmp_path):
    asset = _pdf(tmp_path, ())
    first = _block("The sample was heated at 450 C")
    second = _block("The tensile strength reached 12.5 MPa", block_id="b2", bbox=(20, 190, 350, 240))
    result = _locate(
        asset,
        {"asset_ref": "a", "page": 1, "quote": first["text"] + "; The tensile strength reached 13.5 MPa"},
        [first, second],
    )
    assert result["location_method"] == "mineru_region_segments"
    assert len(result["highlight_regions"]) == 1
    assert result["unmatched_fragments"] == [
        {"quote": "The tensile strength reached 13.5 MPa", "reason": "quote_not_found"}
    ]


def test_scanned_repeated_fragment_in_different_regions_stays_unmatched(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block("The sample was heated at 450 C")
    duplicate = _block(a["text"], block_id="b2", bbox=(20, 185, 350, 220))
    unique = _block("The tensile strength reached 12.5 MPa", block_id="b3", bbox=(20, 230, 350, 270))
    result = _locate(
        asset, {"asset_ref": "a", "page": 1, "quote": a["text"] + "; " + unique["text"]}, [a, duplicate, unique]
    )
    assert result["location_method"] == "mineru_region_segments"
    assert len(result["highlight_regions"]) == 1
    assert result["highlight_regions"][0]["bbox"] == unique["anchor"]["coordinate_region"]["bbox"]
    assert result["unmatched_fragments"] == [{"quote": a["text"], "reason": "ambiguous_fragment"}]


def test_scanned_fragments_in_one_table_share_table_box_without_fake_cells(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block("The sample was heated at 450 C. The tensile strength reached 12.5 MPa", kind="table")
    cell = _block("The sample was heated at 450 C", block_id="b2", kind="table_cell", table_region="b1", ir_region="b1")
    cell["anchor"]["table_cell"] = {"row": 1, "column": 1, "coordinate_basis": "table_structure_not_cell_bbox"}
    result = _locate(
        asset,
        {
            "asset_ref": "a",
            "page": 1,
            "quote": "Claim 1: The sample was heated at 450 C; The tensile strength reached 12.5 MPa",
        },
        [a, cell],
    )
    assert result["location_method"] == "mineru_region_segments"
    assert result["precision"] == "region" and len(result["highlight_regions"]) == 1
    assert [part["highlight_region_indexes"] for part in result["matched_fragments"]] == [[0], [0]]
    assert "table_cell" not in result["anchor"]


def test_scanned_partial_never_joins_text_from_different_blocks(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block("The sample was heated")
    b = _block("at 450 C before testing", block_id="b2", bbox=(20, 185, 350, 220))
    _page(
        _locate(
            asset,
            {"asset_ref": "a", "page": 1, "quote": "Claim 1: The sample was heated at 450 C before testing"},
            [a, b],
        ),
        "quote_not_found",
    )


def test_scanned_full_quote_ambiguity_prevents_partial_region_search(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block("The sample was heated at 450 C; The strength reached 12.5 MPa")
    b = _block(a["text"], block_id="b2", bbox=(20, 185, 350, 220))
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": a["text"]}, [a, b])
    _page(result, "ambiguous_mineru_region_match")
    assert result["partial_match"] is False


def test_scanned_invalid_matched_bbox_never_becomes_partial_highlight(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block("The sample was heated at 450 C", bbox=(-20, 20, 350, 90))
    b = _block("The tensile strength reached 12.5 MPa", block_id="b2", bbox=(20, 185, 350, 220))
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": a["text"] + "; " + b["text"]}, [a, b])
    assert result["location_method"] == "mineru_region_segments"
    assert len(result["highlight_regions"]) == 1
    assert result["unmatched_fragments"] == [{"quote": a["text"], "reason": "invalid_region_coordinates"}]


def test_scanned_partial_does_not_search_other_pages_or_assets(tmp_path):
    asset = _pdf(tmp_path, ())
    a = _block("The sample was heated at 450 C", page=2)
    b = _block("The tensile strength reached 12.5 MPa", block_id="b2", asset="other")
    _page(
        _locate(asset, {"asset_ref": "a", "page": 1, "quote": "Claim 1: " + a["text"] + "; " + b["text"]}, [a, b]),
        "quote_not_found",
    )


@pytest.mark.parametrize(
    "missing_fields",
    [
        {"asset_ref": None, "page": None},
        {"asset_ref": None},
        {"page": None},
    ],
)
def test_legacy_explicit_null_identity_can_fall_back_to_registered_block(tmp_path, missing_fields):
    asset = _pdf(tmp_path)
    evidence = {"block_id": "b1", "quote": "Unique tensile strength", **missing_fields}
    result = _locate(asset, evidence, [_block()])
    assert result["asset_ref"] == "a" and result["page"] == 1
    assert result["precision"] == "text" and result["location_method"] == "native_text_match"
    assert evidence == {"block_id": "b1", "quote": "Unique tensile strength", **missing_fields}


@pytest.mark.parametrize(
    "wrong_fields",
    [
        {"asset_ref": None, "page": 0},
        {"asset_ref": None, "page": []},
        {"asset_ref": [], "page": None},
        {"asset_ref": "", "page": None},
        {"asset_ref": "other", "page": None},
        {"asset_ref": None, "page": 2},
    ],
)
def test_legacy_null_fallback_does_not_accept_explicit_wrong_identity(tmp_path, wrong_fields):
    result = _locate(_pdf(tmp_path), {"block_id": "b1", "quote": "Unique tensile strength", **wrong_fields}, [_block()])
    assert result["status"] in {"page_only", "not_found"}
    assert result["highlight_regions"] == []
    assert "conflict" in result["location_reason"]


@pytest.mark.parametrize("attached_field", ["caption", "footnote"])
def test_table_attached_comment_cannot_borrow_body_region_geometry(tmp_path, attached_field):
    asset = _pdf(tmp_path, ())
    comment = "The sample properties are measured in the following experiment"
    table = _block(
        "Sample A 12.5 MPa Sample B 15.0 MPa", kind="table", bbox=(20, 30, 350, 100), **{attached_field: comment}
    )
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": comment}, [table]), "quote_not_found")
    assert table[attached_field] == comment


def test_partial_attached_comment_remains_unmatched_while_body_region_can_match(tmp_path):
    asset = _pdf(tmp_path, ())
    body = "The measured tensile strength is 12.5 MPa"
    note = "The sample properties are measured in the following experiment"
    table = _block(body, kind="table", bbox=(20, 30, 350, 100), footnote=note)
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": body + "; " + note}, [table])
    assert result["precision"] == "region" and result["partial_match"] is True
    assert result["location_method"] == "mineru_region_segments"
    assert result["matched_fragments"][0]["quote"] == body
    assert result["unmatched_fragments"] == [{"quote": note, "reason": "quote_not_found"}]
    assert len(result["highlight_regions"]) == 1


def test_independent_caption_block_locates_its_own_trusted_bbox(tmp_path):
    asset = _pdf(tmp_path, ())
    comment = "The sample properties are measured in the following experiment"
    table = _block("Sample A 12.5 MPa Sample B 15.0 MPa", kind="table", bbox=(20, 30, 350, 100), caption=comment)
    caption = _block(comment, block_id="b2", kind="caption", bbox=(20, 140, 350, 170))
    result = _locate(asset, {"asset_ref": "a", "page": 1, "quote": comment}, [table, caption])
    assert result["precision"] == "region" and result["location_method"] == "mineru_region_match"
    assert len(result["highlight_regions"]) == 1
    assert result["highlight_regions"][0]["bbox"] == caption["anchor"]["coordinate_region"]["bbox"]


def _structured_table(rows, *, block_id="table1", bbox=(30, 40, 300, 180), **kwargs):
    table = _block(" ".join(" ".join(row) for row in rows), block_id=block_id, kind="table", bbox=bbox, **kwargs)
    cells = []
    for ri, row in enumerate(rows):
        for ci, value in enumerate(row):
            cell = _block(
                value,
                block_id=f"{block_id}:cell:{ri}:{ci}",
                kind="table_cell",
                bbox=bbox,
                table_region=block_id,
                **kwargs,
            )
            cell["anchor"]["table_cell"] = {
                "row": ri,
                "column": ci,
                "row_span": 1,
                "col_span": 1,
                "coordinate_basis": "table_structure_not_cell_bbox",
            }
            cells.append(cell)
    return [table, *cells]


def test_serialized_table_rows_locate_body_instead_of_caption_only(tmp_path):
    asset = _pdf(tmp_path, ("Table 1 measured flame retardancy",))
    rows = [["MRP/%", "TPA/%", "LOI/%", "UL94V"], ["6", "24", "24.0", "V-0"], ["12", "18", "24.5", "V-0"]]
    blocks = _structured_table(rows)
    evidence = {
        "asset_ref": "a",
        "page": 1,
        "quote": "Table 1 measured flame retardancy. MRP/% | TPA/% | LOI/% | UL94V: 6|24|24.0|V-0; 12|18|24.5|V-0.",
    }
    result = _locate(asset, evidence, blocks)
    assert result["location_method"] == "mineru_table_rows"
    assert result["precision"] == "region" and result["partial_match"] is True
    assert result["highlight_regions"][0]["bbox"] == [30, 40, 300, 180]
    assert [part["quote"] for part in result["matched_fragments"]] == ["6|24|24.0|V-0", "12|18|24.5|V-0."]
    assert any("Table 1" in part["quote"] for part in result["unmatched_fragments"])
    assert "table_cell" not in result["anchor"]


def test_numeric_table_row_can_omit_explicit_non_numeric_row_label(tmp_path):
    asset = _pdf(tmp_path, ())
    rows = [["T0/C", "273.9", "250.9", "225.9"], ["W600/%", "1.6", "6.11", "10.9"]]
    result = _locate(
        asset,
        {"asset_ref": "a", "page": 1, "quote": "T0/C:273.9|250.9|225.9; W600/%:1.6|6.11|10.9."},
        _structured_table(rows),
    )
    assert result["location_method"] == "mineru_table_rows"
    assert len(result["matched_fragments"]) == 2
    assert result["highlight_regions"][0]["bbox"] == [30, 40, 300, 180]


@pytest.mark.parametrize(
    "quote",
    [
        "6|24|24.5|V-0",  # changed number
        "24|24.0|V-0",  # missing numeric first column
        "6|24|24.0",  # truncated last column
        "6|24|24|V-0",  # changed numeric representation
        "6|24|24.0|V-2",  # changed categorical result
        "6|24",  # too short
    ],
)
def test_serialized_table_rows_require_exact_complete_cell_sequence(tmp_path, quote):
    _page(
        _locate(
            _pdf(tmp_path, ()),
            {"asset_ref": "a", "page": 1, "quote": quote},
            _structured_table([["6", "24", "24.0", "V-0"]]),
        )
    )


def test_serialized_table_row_in_multiple_tables_stays_unlocated(tmp_path):
    rows = [["6", "24", "24.0", "V-0"]]
    blocks = _structured_table(rows) + _structured_table(rows, block_id="table2", bbox=(50, 200, 350, 250))
    _page(_locate(_pdf(tmp_path, ()), {"asset_ref": "a", "page": 1, "quote": "6|24|24.0|V-0"}, blocks))


def test_serialized_duplicate_table_rows_do_not_invent_unique_match(tmp_path):
    rows = [["6", "24", "24.0", "V-0"], ["6", "24", "24.0", "V-0"]]
    _page(_locate(_pdf(tmp_path, ()), {"asset_ref": "a", "page": 1, "quote": "6|24|24.0|V-0"}, _structured_table(rows)))


def test_serialized_table_rows_reject_missing_or_invalid_structural_cells(tmp_path):
    blocks = _structured_table([["6", "24", "24.0", "V-0"]])
    del blocks[2]  # source column 1 is missing; remaining cells are not a row
    _page(_locate(_pdf(tmp_path, ()), {"asset_ref": "a", "page": 1, "quote": "6|24.0|V-0"}, blocks))


def test_serialized_table_row_does_not_borrow_other_page_or_caption(tmp_path):
    asset = _pdf(tmp_path, ())
    blocks = _structured_table([["6", "24", "24.0", "V-0"]], page=2)
    blocks += [_block("unrelated", kind="table", caption="6|24|24.0|V-0")]
    _page(_locate(asset, {"asset_ref": "a", "page": 1, "quote": "6|24|24.0|V-0"}, blocks))


def test_serialized_table_row_preserves_comparator_and_decimal_and_inputs(tmp_path):
    from copy import deepcopy

    asset = _pdf(tmp_path, ())
    blocks = _structured_table([["0", "30", "20.0", ">30", "HB"]])
    evidence = {"asset_ref": "a", "page": 1, "quote": "0|30|20.0|>30|HB"}
    before = deepcopy((blocks, evidence))
    result = _locate(asset, evidence, blocks)
    assert result["location_method"] == "mineru_table_rows"
    assert (blocks, evidence) == before
    evidence["quote"] = "0|30|20.0|30|HB"
    _page(_locate(asset, evidence, blocks))


def test_serialized_table_rows_support_known_colspan_without_fake_cell_boxes(tmp_path):
    blocks = _structured_table([["Name", "10", "20", "30"]])
    blocks[-1]["anchor"]["table_cell"]["col_span"] = 2
    result = _locate(_pdf(tmp_path, ()), {"asset_ref": "a", "page": 1, "quote": "10|20|30"}, blocks)
    assert result["location_method"] == "mineru_table_rows"
    assert result["highlight_regions"][0]["bbox"] == [30, 40, 300, 180]


@pytest.mark.parametrize("damage", ["parent_text", "outside_page", "cell_box", "overlap_columns"])
def test_serialized_table_row_does_not_use_inconsistent_registry(tmp_path, damage):
    blocks = _structured_table([["6", "24", "24.0", "V-0"]])
    if damage == "parent_text":
        blocks[0]["text"] += " another value"
    elif damage == "outside_page":
        for block in blocks:
            block["anchor"]["coordinate_region"]["bbox"] = [30, 40, 500, 180]
    elif damage == "cell_box":
        blocks[1]["anchor"]["coordinate_region"]["bbox"] = [40, 40, 300, 180]
    else:
        blocks[2]["anchor"]["table_cell"]["column"] = 0
    _page(_locate(_pdf(tmp_path, ()), {"asset_ref": "a", "page": 1, "quote": "6|24|24.0|V-0"}, blocks))


def test_repeated_serialized_fragment_is_not_asserted_as_unique(tmp_path):
    blocks = _structured_table([["6", "24", "24.0", "V-0"]])
    _page(_locate(_pdf(tmp_path, ()), {"asset_ref": "a", "page": 1, "quote": "6|24|24.0|V-0;6|24|24.0|V-0"}, blocks))
