"""Explicit row keys; common value semantics and package-wide quality checking."""

from kb_ingest.contract import FIELDS, add_coverage, add_evidence, add_issue, add_relation

from .table_orientation import evidence_cells
from .table_semantics import (
    classify_cell,
    identity_context,
    merge_object,
    record_unmapped,
    resolve_qualifiers,
    scalar_assertion,
)


def extract_rows(package, asset, table, profile):
    from .experimental import _anchor, _cell, _column

    sample_cols = [_column(c) for c in profile["sample_columns"]]
    test_cols = [_column(c) for c in profile["test_columns"]]
    exp_cols = [_column(c) for c in profile.get("experiment_columns", [])]
    if not sample_cols or not test_cols:
        raise ValueError("records requires explicit sample and test key columns")
    mappings = profile["fields"]
    if not mappings:
        raise ValueError("records requires field mappings")
    for index, spec in enumerate(mappings):
        kind = {"sample": "sample", "test": "reported_test", "observation": "observation"}.get(spec["target"])
        if not kind or kind not in FIELDS[spec["field_id"]]["owner_kinds"]:
            raise ValueError(
                f"Invalid row field owner at fields[{index}]: field_id={spec['field_id']!r}, "
                f"target={spec['target']!r}, kind={kind!r}; "
                f"allowed_kinds={sorted(FIELDS[spec['field_id']]['owner_kinds'])!r}"
            )
        if "unit" in spec or "basis" in spec:
            raise ValueError("Use original source unit/basis locations, not assumed constants")
    scope = [asset["asset_id"], table["sheet"]]
    if table.get("_orientation"):
        scope.append(table["_orientation"])
    if "_region_scope" in profile:
        scope.append(profile["_region_scope"])
    used = set()
    start = profile.get("data_start_row", (profile.get("header_row", 1) or 0) + 1)
    end = profile.get("data_end_row", table["nrows"])
    _cell(table, start, 1)
    _cell(table, end, 1)
    if start > end:
        raise ValueError("data_start_row exceeds data_end_row")
    for row in range(start, end + 1):
        keys = [_cell(table, row, c) for c in exp_cols + sample_cols + test_cols]
        if all(classify_cell(c)["status"] == "source_blank" for c in table["rows"][row - 1]["cells"]):
            continue
        ev = add_evidence(
            package,
            asset["asset_id"],
            _anchor(table, [row, row]),
            {"cells": evidence_cells(table["rows"][row - 1]["cells"])},
            "table_row",
        )
        used.update((row, c["column"]) for c in keys)
        if any(classify_cell(c, interpret_missing=False)["status"] != "value" for c in keys):
            add_issue(
                package,
                "Row identity is missing, erroneous or formula-derived; no guessed identity",
                evidence_refs=[ev],
                severity="error",
                code="missing_row_identity",
            )
            add_coverage(
                package,
                {"asset_ref": asset["asset_id"], "sheet": table["sheet"], "row": row},
                "not_checked",
                "Cannot attach this row to a source-declared identity",
            )
            continue

        def entity(kind, cols):
            cells = [_cell(table, row, c) for c in cols]
            values = [c["raw_value"] for c in cells]
            return merge_object(
                package,
                kind,
                [scope, values],
                " / ".join(map(str, values)),
                [ev],
                identity_context(asset, table, cells),
                identity_status="source_key_mapping",
            )

        sample = entity("sample", exp_cols + sample_cols)
        next(o for o in package["objects"] if o["object_id"] == sample)["label"] = " / ".join(
            str(_cell(table, row, c)["raw_value"]) for c in sample_cols
        )
        if exp_cols:
            experiment = entity("content_unit", exp_cols)
            add_relation(package, f"{package['header']['source_id']}:source", "contains", experiment, [ev])
            add_relation(package, experiment, "contains_sample", sample, [ev])
        else:
            add_relation(package, f"{package['header']['source_id']}:source", "reports", sample, [ev])
        test = entity("reported_test", exp_cols + sample_cols + test_cols)
        add_relation(package, sample, "tested_by", test, [ev])
        for spec in mappings:
            target = sample if spec["target"] == "sample" else test
            col = _column(spec["column"])
            if spec["target"] == "observation" and classify_cell(_cell(table, row, col))["status"] == "value":
                qualifiers, _ = resolve_qualifiers(table, row, spec, used)
                target = merge_object(
                    package,
                    "observation",
                    [scope, test, row, col, qualifiers],
                    str(_cell(table, profile.get("header_row", 1), col)["raw_value"])
                    if profile.get("header_row", 1)
                    else f"source column {col}",
                    [ev],
                    identity_context(asset, table, keys),
                    identity_status="source_cell_observation",
                )
                add_relation(package, test, "produces_observation", target, [ev])
            scalar_assertion(
                package,
                asset,
                table,
                profile,
                row,
                spec,
                target,
                evidence=[ev],
                used=used,
                statement_kind="source_recorded_result"
                if spec["target"] == "observation"
                else "source_recorded_metadata",
                assessment_owner_kind="observation" if spec["target"] == "observation" else None,
            )
    record_unmapped(package, asset, table, used)
