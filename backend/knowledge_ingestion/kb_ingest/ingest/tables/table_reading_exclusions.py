"""Validate planned exclusions against an extracted package."""


def validate_exclusions_against_package(recipe, package):
    """Reject exclusion of semantically used originals, not broad evidence excerpts.

    Call after deterministic extraction and common package checks, before adding
    exclusion annotations. Does not erase assertions or change use status.
    """
    from .table_recipe import validate_rectangle

    exclusions = recipe.get("excluded_ranges", [])
    if not exclusions:
        return {"checked_ranges": 0}
    tables = {
        (e["asset_ref"], e["content"].get("sheet")): e["content"]
        for e in package["evidence"]
        if e.get("content_kind") == "table_transcription"
    }
    mapped = set()
    # Successful extractors report all still-unmapped nonblank original addresses.
    # This adds shared header/label uses without treating a full row excerpt or
    # enclosing key/value record as semantically mapped in its entirety.
    for coverage in package["coverage"]:
        scope = coverage.get("scope")
        if isinstance(scope, dict) and scope.get("cell_state") == "extracted":
            mapped.update(
                (scope.get("asset_ref"), scope.get("sheet"), row, col) for row, col in coverage.get("cells", [])
            )
        if not isinstance(scope, dict) or scope.get("scope") != "unmapped_cells":
            continue
        key = (scope.get("asset_ref"), scope.get("sheet"))
        if key not in tables:
            continue
        unresolved = set(coverage.get("cell_addresses", []))
        for row in tables[key]["rows"]:
            for cell in row["cells"]:
                raw = cell["raw_value"]
                if (
                    raw is not None
                    and not (isinstance(raw, str) and not raw.strip())
                    and cell["address"] not in unresolved
                ):
                    mapped.add((*key, cell["row"], cell["column"]))
    # Direct bindings also include blank/ambiguous/formula assessments: excluding
    # such a cell must not disguise a mapping's explicitly observed missingness.
    for collection in ("objects", "assertions", "assessments"):
        for item in package[collection]:
            context = item.get("source_context", {})
            bindings = [
                *context.get("cell_bindings", []),
                *context.get("unit_candidate_bindings", []),
                *context.get("qualifier_bindings", {}).values(),
            ]
            for binding in bindings:
                if type(binding.get("column")) is not int:
                    continue
                key = (binding.get("asset_ref"), binding.get("sheet"))
                if "row" in binding:
                    rows = [binding["row"]]
                elif "rows" in binding:
                    rows = binding["rows"]
                elif "row_range" in binding:
                    rows = range(binding["row_range"][0], binding["row_range"][1] + 1)
                else:
                    continue
                mapped.update((*key, row, binding["column"]) for row in rows)
    for index, exclusion in enumerate(exclusions):
        key = (exclusion["asset_id"], exclusion["sheet"])
        table = tables.get(key)
        if table is None:
            raise ValueError("Excluded range has no retained original table")
        c0, r0, c1, r1 = validate_rectangle(exclusion["range"], table, f"excluded_ranges[{index}]")
        if any((*key, row, column) in mapped for row in range(r0, r1 + 1) for column in range(c0, c1 + 1)):
            raise ValueError(
                f"excluded_ranges[{index}] overlaps mapped value, identity, "
                "header or unit cells; narrow the mapping or exclusion"
            )
    return {"checked_ranges": len(exclusions)}
