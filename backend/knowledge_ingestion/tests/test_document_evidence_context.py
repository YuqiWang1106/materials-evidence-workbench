"""Explicit source support cannot cross a structural owner or a table region."""

from copy import deepcopy

from kb_ingest.document_direct_contract import convert_direct
from kb_ingest.document_evidence_context import bind_review_support, build_support_context, replay_review_support
from evidence_support_fixtures import QUOTE, payload


def _blocks():
    return [
        {
            "block_id": "b1",
            "text": QUOTE,
            "kind": "native_text",
            "asset_ref": "a",
            "page": 1,
            "anchor": {"kind": "pdf", "page_numbers": [1]},
        },
        {
            "block_id": "value-a",
            "text": "28.0",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 2, "column": 1}},
        },
        {
            "block_id": "header-a",
            "text": "Width (mm)",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 0, "column": 1}},
        },
        {
            "block_id": "sample-b-row",
            "text": "Sample B",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 3, "column": 0}},
        },
        {
            "block_id": "other-table",
            "text": "Width (cm)",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-2",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 0, "column": 1}},
        },
    ]


def _candidate():
    candidate = convert_direct(payload())
    target = next(
        f for f in candidate["facts"] if f["field_id"] == "observation.value" and "/samples/0/tests/0/" in f["key"]
    )
    target["value_cell"] = "value-a"
    target["evidence"] = [{"block_id": "value-a", "quote": "28.0"}]
    return candidate, "f:" + target["key"]


def test_support_context_uses_exact_sample_test_and_method_links():
    candidate, target = _candidate()
    contexts = build_support_context(candidate, _blocks())
    options = contexts[target]
    assert any(x["role"] == "sample" and "/samples/0" in x["source_target"] for x in options)
    assert not any(x["role"] == "sample" and "/samples/1" in x["source_target"] for x in options)
    assert any(x["role"] == "method_link" and x["source_target"].startswith("r:") for x in options)
    assert any(x["role"] == "condition" and "/samples/0/tests/0/" in x["source_target"] for x in options)
    assert not any(x["role"] == "condition" and "/samples/0/tests/1/" in x["source_target"] for x in options)
    assert any(x["evidence"][0]["block_id"] == "header-a" for x in options)
    assert not any(x["evidence"][0]["block_id"] == "sample-b-row" for x in options)
    assert not any(x["evidence"][0]["block_id"] == "other-table" for x in options)


def test_binding_requires_explicit_per_target_choice_and_keeps_original_candidate():
    candidate, target = _candidate()
    original = deepcopy(candidate)
    contexts = build_support_context(candidate, _blocks())
    selected = next(x for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    bound, trace, invalid = bind_review_support(candidate, contexts, {target: [selected["id"]]}, _blocks())
    assert not invalid and candidate == original
    assert {"block_id": "header-a", "quote": "Width (mm)"} in next(
        f["evidence"] for f in bound["facts"] if "f:" + f["key"] == target
    )
    assert trace[0]["target"] == target and trace[0]["selected_ids"] == [selected["id"]]
    assert trace[0]["before_evidence_sha256"] != trace[0]["after_evidence_sha256"]
    other = next(k for k in contexts if k != target)
    _, _, invalid = bind_review_support(candidate, contexts, {other: [selected["id"]]}, _blocks())
    assert other in invalid


def test_binding_rejects_duplicate_and_context_only_value_without_original_value_source():
    candidate, target = _candidate()
    contexts = build_support_context(candidate, _blocks())
    selected = next(x for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    _, _, invalid = bind_review_support(candidate, contexts, {target: [selected["id"], selected["id"]]}, _blocks())
    assert target in invalid
    candidate["facts"] = [
        {**f, "evidence": [{"block_id": "b1", "quote": "Study 1"}]} if "f:" + f["key"] == target else f
        for f in candidate["facts"]
    ]
    _, _, invalid = bind_review_support(candidate, contexts, {target: [selected["id"]]}, _blocks())
    assert target in invalid


def test_same_number_belonging_to_other_sample_is_not_offered_as_column_context():
    candidate, target = _candidate()
    blocks = _blocks() + [
        {
            "block_id": "value-b",
            "text": "28.0",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 1, "column": 1}},
        }
    ]
    foreign = next(f for f in candidate["facts"] if f["field_id"] == "observation.value" and "/samples/1/" in f["key"])
    foreign["value_cell"] = "value-b"
    foreign["evidence"] = [{"block_id": "value-b", "quote": "28.0"}]
    options = build_support_context(candidate, blocks)[target]
    assert not any(x["evidence"][0]["block_id"] == "value-b" for x in options)


def test_other_sample_identity_cell_is_not_offered_as_row_context():
    candidate, target = _candidate()
    blocks = _blocks() + [
        {
            "block_id": "sample-b-identity",
            "text": "Sample B",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 2, "column": 0}},
        }
    ]
    foreign = next(o for o in candidate["objects"] if o["kind"] == "sample" and "/samples/1" in o["key"])
    foreign["evidence"] = [{"block_id": "sample-b-identity", "quote": "Sample B"}]
    options = build_support_context(candidate, blocks)[target]
    assert not any(x["evidence"][0]["block_id"] == "sample-b-identity" for x in options)


def test_ownerless_claim_cannot_borrow_known_other_sample_identity():
    blocks = [
        {
            "block_id": "value",
            "kind": "table_cell",
            "text": "28.0",
            "table_region": "t",
            "anchor": {"table_cell": {"row": 2, "column": 1}},
        },
        {
            "block_id": "other-sample",
            "kind": "table_cell",
            "text": "Sample B",
            "table_region": "t",
            "anchor": {"table_cell": {"row": 2, "column": 0}},
        },
    ]
    candidate = {
        "objects": [
            {"key": "claim", "kind": "patent_claim", "evidence": []},
            {"key": "component", "kind": "component", "evidence": []},
            {"key": "sample-b", "kind": "sample", "evidence": [{"block_id": "other-sample", "quote": "Sample B"}]},
        ],
        "relations": [{"predicate": "contains_component", "from_key": "claim", "to_key": "component", "evidence": []}],
        "facts": [
            {
                "key": "amount",
                "object_key": "component",
                "field_id": "composition.amount",
                "value_cell": "value",
                "evidence": [{"block_id": "value", "quote": "28.0"}],
            }
        ],
    }
    options = build_support_context(candidate, blocks)["f:amount"]
    assert not any(x["evidence"][0]["block_id"] == "other-sample" for x in options)


def test_ambiguous_identity_cell_claimed_by_two_samples_is_not_support():
    candidate, target = _candidate()
    blocks = _blocks() + [
        {
            "block_id": "shared-label",
            "text": "Sample",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"table_cell": {"row": 2, "column": 0}},
        }
    ]
    samples = [o for o in candidate["objects"] if o["kind"] == "sample"]
    for sample in samples:
        sample["evidence"].append({"block_id": "shared-label", "quote": "Sample"})
    options = build_support_context(candidate, blocks)[target]
    assert not any(
        x["evidence"][0]["block_id"] == "shared-label" and x["role"] == "potential_row_context" for x in options
    )


def test_other_measurement_values_are_not_geometry_context_even_for_same_sample():
    candidate, target = _candidate()
    blocks = _blocks() + [
        {
            "block_id": "another-value",
            "kind": "table_cell",
            "text": "19.0",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"table_cell": {"row": 0, "column": 1}},
        }
    ]
    another = deepcopy(next(f for f in candidate["facts"] if "f:" + f["key"] == target))
    another.update(key="another", value_cell="another-value", evidence=[{"block_id": "another-value", "quote": "19.0"}])
    candidate["facts"].append(another)
    options = build_support_context(candidate, blocks)[target]
    assert not any(x["evidence"][0]["block_id"] == "another-value" for x in options)


def test_excess_geometry_context_is_reported_without_truncated_allowlist():
    candidate, target = _candidate()
    blocks = _blocks()
    next(b for b in blocks if b["block_id"] == "value-a")["anchor"]["table_cell"]["row"] = 100
    blocks += [
        {
            "block_id": f"unknown-{i}",
            "text": f"Header {i}",
            "kind": "table_cell",
            "table_region": "table-1",
            "asset_ref": "a",
            "page": 2,
            "anchor": {"table_cell": {"row": i, "column": 1}},
        }
        for i in range(80)
    ]
    contexts, overflows = build_support_context(candidate, blocks, return_overflow=True)
    assert target in overflows
    assert contexts[target] == []
    try:
        build_support_context(candidate, blocks)
    except ValueError as exc:
        assert target in str(exc)
    else:
        raise AssertionError("unreported support overflow")


def test_large_single_support_quote_is_reported_as_overflow():
    candidate, target = _candidate()
    blocks = _blocks() + [
        {
            "block_id": "huge-caption",
            "text": "Header " + ("x" * 17000),
            "kind": "table_caption",
            "table_region": "table-1",
            "asset_ref": "a",
            "page": 2,
        }
    ]
    contexts, overflows = build_support_context(candidate, blocks, return_overflow=True)
    assert target in overflows and contexts[target] == []


def test_same_column_different_metric_remains_unproven_until_selected():
    candidate, target = _candidate()
    blocks = _blocks() + [
        {
            "block_id": "earlier-metric",
            "text": "Strength (MPa)",
            "kind": "table_cell",
            "asset_ref": "a",
            "page": 2,
            "table_region": "table-1",
            "anchor": {"kind": "pdf", "page_numbers": [2], "table_cell": {"row": 1, "column": 1}},
        }
    ]
    options = build_support_context(candidate, blocks)[target]
    option = next(x for x in options if x["evidence"][0]["block_id"] == "earlier-metric")
    assert option["role"] == "potential_column_context"
    bound, trace, invalid = bind_review_support(candidate, {target: options}, {target: []}, blocks)
    assert not invalid and not trace[0]["selected_ids"]
    assert bound == candidate


def test_replay_keeps_prior_explicit_support_after_unrelated_record_append():
    candidate, target = _candidate()
    blocks = _blocks()
    contexts = build_support_context(candidate, blocks)
    chosen = next(x["id"] for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    bound, trace, invalid = bind_review_support(candidate, contexts, {target: [chosen]}, blocks)
    assert not invalid
    corrected = deepcopy(candidate)
    new_fact = deepcopy(next(f for f in candidate["facts"] if "/samples/1/" in f["key"]))
    new_fact["key"] += "-new"
    corrected["facts"].append(new_fact)
    replayed, diagnostic = replay_review_support(corrected, trace, blocks)
    assert not diagnostic and corrected != replayed
    assert replayed["facts"] == [*bound["facts"], new_fact]


def test_replay_allows_append_only_literal_fact_evidence_but_preserves_prior_refs():
    candidate, target = _candidate()
    blocks = _blocks()
    contexts = build_support_context(candidate, blocks)
    chosen = next(x["id"] for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    _, trace, invalid = bind_review_support(candidate, contexts, {target: [chosen]}, blocks)
    assert not invalid
    corrected = deepcopy(candidate)
    fact = next(f for f in corrected["facts"] if "f:" + f["key"] == target)
    fact["evidence"].append({"block_id": "b1", "quote": "Sample A width is 28.0 mm"})
    replayed, diagnostic = replay_review_support(corrected, trace, blocks)
    assert not diagnostic and replayed is not None
    references = next(f["evidence"] for f in replayed["facts"] if "f:" + f["key"] == target)
    assert {"block_id": "value-a", "quote": "28.0"} in references
    assert {"block_id": "header-a", "quote": "Width (mm)"} in references
    assert {"block_id": "b1", "quote": "Sample A width is 28.0 mm"} in references


def test_replay_allows_model_to_repeat_prior_selected_support_without_duplication():
    candidate, target = _candidate()
    blocks = _blocks()
    contexts = build_support_context(candidate, blocks)
    chosen = next(x["id"] for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    _, trace, invalid = bind_review_support(candidate, contexts, {target: [chosen]}, blocks)
    assert not invalid
    corrected = deepcopy(candidate)
    next(f for f in corrected["facts"] if "f:" + f["key"] == target)["evidence"].append(
        {"block_id": "header-a", "quote": "Width (mm)"}
    )
    replayed, diagnostic = replay_review_support(corrected, trace, blocks)
    assert not diagnostic and replayed is not None
    references = next(f["evidence"] for f in replayed["facts"] if "f:" + f["key"] == target)
    assert references.count({"block_id": "header-a", "quote": "Width (mm)"}) == 1


def test_replay_rejects_new_citation_that_is_not_literal_source_text():
    candidate, target = _candidate()
    blocks = _blocks()
    contexts = build_support_context(candidate, blocks)
    chosen = next(x["id"] for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    _, trace, invalid = bind_review_support(candidate, contexts, {target: [chosen]}, blocks)
    assert not invalid
    corrected = deepcopy(candidate)
    next(f for f in corrected["facts"] if "f:" + f["key"] == target)["evidence"].append(
        {"block_id": "b1", "quote": "invented table condition"}
    )
    replayed, diagnostic = replay_review_support(corrected, trace, blocks)
    assert replayed is None and target in diagnostic


def test_replay_ignores_overflow_on_unrelated_new_fact():
    candidate, target = _candidate()
    blocks = _blocks()
    contexts = build_support_context(candidate, blocks)
    chosen = next(x["id"] for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    bound, trace, invalid = bind_review_support(candidate, contexts, {target: [chosen]}, blocks)
    assert not invalid
    corrected = deepcopy(candidate)
    new_fact = deepcopy(next(f for f in candidate["facts"] if "f:" + f["key"] == target))
    new_fact.update(key="new-fact", value_cell="new-value", evidence=[{"block_id": "new-value", "quote": "12.0"}])
    corrected["facts"].append(new_fact)
    blocks.append(
        {
            "block_id": "new-value",
            "kind": "table_cell",
            "text": "12.0",
            "table_region": "other-table",
            "asset_ref": "a",
            "page": 2,
            "anchor": {"table_cell": {"row": 100, "column": 1}},
        }
    )
    blocks += [
        {
            "block_id": f"other-header-{i}",
            "text": f"Header {i}",
            "kind": "table_cell",
            "table_region": "other-table",
            "asset_ref": "a",
            "page": 2,
            "anchor": {"table_cell": {"row": i, "column": 1}},
        }
        for i in range(80)
    ]
    replayed, diagnostic = replay_review_support(corrected, trace, blocks)
    assert not diagnostic and replayed is not None
    assert replayed["facts"][0] == bound["facts"][0]


def test_replay_rejects_changed_fact_or_method_context_without_borrowing():
    candidate, target = _candidate()
    blocks = _blocks()
    contexts = build_support_context(candidate, blocks)
    chosen = next(x["id"] for x in contexts[target] if x["evidence"][0]["block_id"] == "header-a")
    _, trace, invalid = bind_review_support(candidate, contexts, {target: [chosen]}, blocks)
    assert not invalid
    changed_fact = deepcopy(candidate)
    next(f for f in changed_fact["facts"] if "f:" + f["key"] == target)["raw_unit"] = "MPa"
    replayed, diagnostic = replay_review_support(changed_fact, trace, blocks)
    assert replayed is None and target in diagnostic
    changed_citation = deepcopy(candidate)
    next(f for f in changed_citation["facts"] if "f:" + f["key"] == target)["evidence"] = [
        {"block_id": "b1", "quote": "Sample A width is 28.0 mm"}
    ]
    replayed, diagnostic = replay_review_support(changed_citation, trace, blocks)
    assert replayed is None and target in diagnostic
    changed_method = deepcopy(candidate)
    next(o for o in changed_method["objects"] if o["kind"] == "method_definition")["evidence"] = [
        {"block_id": "b1", "quote": "Both samples follow Method M."}
    ]
    replayed, diagnostic = replay_review_support(changed_method, trace, blocks)
    assert replayed is None and target in diagnostic
