"""Declarative row-to-object assembly without material- or file-specific rules."""

from kb_ingest.contract import FIELDS, RELATIONS, add_evidence, add_relation

from .table_orientation import evidence_cells
from .table_semantics import classify_cell, identity_context, merge_object, record_unmapped, scalar_assertion


def extract_objects(package, asset, table, profile):
    from .experimental import _anchor, _cell, _column

    definitions = profile["objects"]
    if not isinstance(definitions, dict) or not definitions or not profile.get("fields"):
        raise ValueError("objects requires named object definitions and field mappings")
    known_kinds = set().union(
        *(set(f["owner_kinds"]) for f in FIELDS.values()),
        *(set(r["allowed_from_kinds"] + r["allowed_to_kinds"]) for r in RELATIONS.values()),
    )
    for alias, definition in definitions.items():
        if definition["kind"] not in known_kinds or definition["kind"] == "source" or not definition.get("key_columns"):
            raise ValueError("Each declared object requires a known scientific kind and source key columns")
        if definition["kind"] == "component" and not all(k in definition for k in ("owner", "material")):
            raise ValueError("Components require explicit formulation owner and material object aliases")
        for reference in ("owner", "material"):
            if reference in definition and definition[reference] not in definitions:
                raise ValueError("Object owner/material alias is not declared")
        if definition["kind"] == "component":
            if definitions[definition["owner"]]["kind"] not in ("formulation", "patent_claim") or definitions[
                definition["material"]
            ]["kind"] not in ("material", "formulation"):
                raise ValueError("Component owner/material kinds violate the common contract")
        if "identity_namespace" in definition and (
            not isinstance(definition["identity_namespace"], str) or not definition["identity_namespace"].strip()
        ):
            raise ValueError("Explicit identity namespace must be a non-empty string")
    for field in profile["fields"]:
        if (
            field["target"] not in definitions
            or definitions[field["target"]]["kind"] not in FIELDS[field["field_id"]]["owner_kinds"]
        ):
            raise ValueError("Invalid declared object field owner")
    for relation in profile.get("relations", []):
        rule = RELATIONS[relation["predicate"]]
        if (
            definitions[relation["from"]]["kind"] not in rule["allowed_from_kinds"]
            or definitions[relation["to"]]["kind"] not in rule["allowed_to_kinds"]
        ):
            raise ValueError("Declared relation endpoints violate the common contract")
    start = profile.get("data_start_row", (profile.get("header_row", 1) or 0) + 1)
    end = profile.get("data_end_row", table["nrows"])
    _cell(table, start, 1)
    _cell(table, end, 1)
    if start > end:
        raise ValueError("data_start_row exceeds data_end_row")
    used = set()
    for row in range(start, end + 1):
        if all(classify_cell(c)["status"] == "source_blank" for c in table["rows"][row - 1]["cells"]):
            continue
        row_ev = add_evidence(
            package,
            asset["asset_id"],
            _anchor(table, [row, row]),
            {"cells": evidence_cells(table["rows"][row - 1]["cells"])},
            "table_row",
        )
        built, pending = {}, dict(definitions)
        while pending:
            ready = [a for a, d in pending.items() if all(d[k] in built for k in ("owner", "material") if k in d)]
            if not ready:
                raise ValueError("Cyclic or unresolved object owner/material dependency")
            for alias in ready:
                definition = pending.pop(alias)
                cells = [_cell(table, row, _column(c)) for c in definition["key_columns"]]
                used.update((row, c["column"]) for c in cells)
                if any(classify_cell(c, interpret_missing=False)["status"] != "value" for c in cells):
                    raise ValueError("Declared object identity has blank, error or formula cells")
                values = [c["raw_value"] for c in cells]
                context = identity_context(asset, table, cells)
                namespace = definition.get("identity_namespace")
                scope = ["declared_namespace", namespace] if namespace else [asset["asset_id"], table["sheet"], alias]
                if table.get("_orientation") and not namespace:
                    scope.append(table["_orientation"])
                if "_region_scope" in profile:
                    scope.append(profile["_region_scope"])
                extra = {name + "_ref": built[definition[name]] for name in ("owner", "material") if name in definition}
                if namespace:
                    context["identity_namespace"] = namespace
                    context["identity_binding"] = "caller_declared_exact_source_keys"
                label_cell = (
                    _cell(table, row, _column(definition["label_column"]))
                    if definition.get("label_column") is not None
                    else None
                )
                if label_cell:
                    used.add((row, label_cell["column"]))
                label = str(label_cell["raw_value"]) if label_cell else " / ".join(map(str, values))
                built[alias] = merge_object(
                    package,
                    definition["kind"],
                    [scope, values],
                    label,
                    [row_ev],
                    context,
                    identity_status="source_key_mapping",
                    **extra,
                )
        for relation in profile.get("relations", []):
            add_relation(
                package,
                built[relation["from"]],
                relation["predicate"],
                built[relation["to"]],
                [row_ev],
                basis_kind="source_explicit",
            )
        for field in profile["fields"]:
            scalar_assertion(
                package,
                asset,
                table,
                profile,
                row,
                field,
                built[field["target"]],
                evidence=[row_ev],
                used=used,
                statement_kind="source_recorded_result"
                if definitions[field["target"]]["kind"] == "observation"
                else "source_recorded_metadata",
            )
    record_unmapped(package, asset, table, used)
