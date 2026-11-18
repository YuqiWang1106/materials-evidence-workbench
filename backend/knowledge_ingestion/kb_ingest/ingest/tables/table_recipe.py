"""Strict declarative table recipe validation, independent of model protocols.

Coordinates describe the selected orientation. Evidence remains in original
source coordinates; callers may confirm interpretation but cannot inject values.
"""

from __future__ import annotations

import math
import re

from openpyxl.utils.cell import column_index_from_string, range_boundaries

from kb_ingest.contract import FIELDS, RELATIONS

from .extractors.profile_regions import selected_ranges

HEADER_CONDITION_PATTERN = r"\bat\s*(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>MPa|kPa|Pa)\b"
COMMON = {
    "asset_id",
    "sheet",
    "layout",
    "orientation",
    "header_row",
    "header_rows",
    "header_bindings",
    "unit_row",
    "data_start_row",
    "data_end_row",
    "data_ranges",
}
LAYOUT = {
    "records": {"experiment_columns", "sample_columns", "test_columns", "fields"},
    "objects": {"objects", "relations", "fields"},
    "key_value": {"key_column", "value_column", "label_key", "record_scope", "run_key", "fields"},
    "curves": {"series", "run_key"},
    "channels": {"x_column", "y_columns", "calibration_rows", "run_key"},
}
FIELD = {
    "column",
    "target",
    "field_id",
    "qualifiers",
    "qualifier_columns",
    "qualifier_metadata_sources",
    "unit_column",
    "unit_row",
    "unit_cell",
    "header_cell",
    "basis_column",
    "value_kind",
    "parse",
    "header_scope",
}
OBJECT = {"kind", "key_columns", "owner", "material", "label_column", "identity_namespace"}
SERIES = {"x_column", "y_column", "extra_columns", "label_cell", "condition_from_label"}


def _expected(path, expected, value):
    raise ValueError(f"{path}: expected {expected}; received {type(value).__name__}")


def _string(value, path):
    if not isinstance(value, str):
        _expected(path, "string", value)


def _list(value, path):
    if not isinstance(value, list):
        _expected(path, "array", value)


def keys(value, allowed, required=(), path="configuration"):
    if not isinstance(value, dict):
        _expected(path, "object", value)
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path}: configuration keys must be strings")
    unsupported = set(value) - set(allowed)
    if unsupported:
        name = sorted(unsupported)[0]
        safe_name = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "<unsupported>"
        raise ValueError(f"{path}.{safe_name}: unsupported configuration key")
    missing = set(required) - set(value)
    if missing:
        raise ValueError(f"{path}.{sorted(missing)[0]}: required configuration key is missing")


def integer(value, upper, path="coordinate"):
    if type(value) is not int:
        _expected(path, f"one-based integer <= {upper}", value)
    if not 1 <= value <= upper:
        raise ValueError(f"{path}: expected one-based integer <= {upper}; received out-of-range int")


def column(value, table, path="column"):
    if isinstance(value, str):
        if not re.fullmatch("[A-Z]{1,3}", value):
            raise ValueError(f"{path}: expected one-based integer or Excel column letters; received invalid str")
        value = column_index_from_string(value)
    if type(value) is not int:
        _expected(path, f"one-based integer or Excel column letters <= {table['ncols']}", value)
    integer(value, table["ncols"], path)


def columns(values, table, allow_empty=False, path="columns"):
    _list(values, path)
    if (not values and not allow_empty) or len(values) > table["ncols"]:
        raise ValueError(
            f"{path}: expected {'possibly empty' if allow_empty else 'nonempty'}"
            f" array of at most {table['ncols']} columns; received invalid list"
        )
    for index, value in enumerate(values):
        column(value, table, f"{path}[{index}]")


def validate_rectangle(value, table, path="range"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*:[A-Z]{1,3}[1-9][0-9]*", value):
        raise ValueError(f"{path}: expected original A1:B2 rectangle")
    c0, r0, c1, r1 = range_boundaries(value)
    if not (1 <= c0 <= c1 <= table["ncols"] and 1 <= r0 <= r1 <= table["nrows"]):
        raise ValueError(f"{path}: range outside original table bounds")
    return c0, r0, c1, r1


def validate_profile_list(profiles, tables, asset_id):
    if not isinstance(profiles, list) or len(profiles) > 128:
        raise ValueError("profiles must contain at most 128 profiles")
    for profile_index, profile in enumerate(profiles):
        path = f"profiles[{profile_index}]"
        if not isinstance(profile, dict):
            _expected(path, "object", profile)
        if "layout" not in profile:
            raise ValueError(f"{path}.layout: required configuration key is missing")
        _string(profile["layout"], path + ".layout")
        if profile["layout"] not in LAYOUT:
            raise ValueError(f"{path}.layout: expected one of {'|'.join(LAYOUT)}; received unsupported str")
        layout = profile["layout"]
        keys(profile, COMMON | LAYOUT[layout], {"asset_id", "sheet", "layout"}, path)
        _string(profile["asset_id"], path + ".asset_id")
        if profile["sheet"] is not None:
            _string(profile["sheet"], path + ".sheet")
        table = next((t for t in tables if t["sheet"] == profile["sheet"]), None)
        if profile["asset_id"] != asset_id or table is None or not table.get("extraction_safe", True):
            raise ValueError("Profile must identify one safely read original table")
        from .extractors.table_orientation import oriented_table

        table = oriented_table(table, profile)
        ranges = selected_ranges(profile, table["nrows"], path)
        if ranges is not None:
            for header_name in ("header_row", "unit_row"):
                if profile.get(header_name) is not None and profile[header_name] >= ranges[0][0]:
                    raise ValueError(f"{path}.{header_name}: shared header/unit row must precede every data range")
        for name in ("header_row", "unit_row", "data_start_row", "data_end_row"):
            if name in profile and (profile[name] is not None or name in ("data_start_row", "data_end_row")):
                integer(profile[name], table["nrows"], path + "." + name)
        if ranges is None and profile.get("data_start_row", 1) > profile.get("data_end_row", table["nrows"]):
            raise ValueError(f"{path}: expected data_start_row <= data_end_row; received reversed range")
        start = (
            ranges[0][0]
            if ranges
            else profile.get("data_start_row", 1 if layout == "key_value" else (profile.get("header_row", 1) or 0) + 1)
        )
        if start > table["nrows"]:
            raise ValueError(f"{path}: data starts outside table bounds")
        for name in ("header_row", "unit_row"):
            if profile.get(name) is not None and profile[name] >= start:
                raise ValueError(f"{path}.{name}: header/unit row must precede data")
        if "header_rows" in profile:
            _list(profile["header_rows"], path + ".header_rows")
            if len(profile["header_rows"]) > 32:
                raise ValueError(f"{path}.header_rows: at most 32 rows")
            start = (
                ranges[0][0]
                if ranges is not None
                else profile.get("data_start_row", (profile.get("header_row") or 0) + 1)
            )
            for header_index, row in enumerate(profile["header_rows"]):
                integer(row, table["nrows"], f"{path}.header_rows[{header_index}]")
                if row >= start:
                    raise ValueError(f"{path}.header_rows: headers must precede data_start_row")
        if "header_bindings" in profile:
            _list(profile["header_bindings"], path + ".header_bindings")
            if len(profile["header_bindings"]) > 128:
                raise ValueError(f"{path}.header_bindings: at most 128 bindings")
            from .extractors.experimental import _address, _column

            for index, rule in enumerate(profile["header_bindings"]):
                binding_path = f"{path}.header_bindings[{index}]"
                keys(rule, {"role", "source_cell", "columns"}, {"role", "source_cell", "columns"}, binding_path)
                if rule["role"] not in ("material", "stage", "metric", "unit", "note"):
                    raise ValueError(f"{binding_path}.role: unsupported header role")
                address = rule["source_cell"]
                if not isinstance(address, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", address):
                    raise ValueError(f"{binding_path}.source_cell: expected source A1 address")
                cell = _address(table, address)
                data_ranges = ranges or [(start, profile.get("data_end_row", table["nrows"]))]
                invalid_position = (
                    any(lo <= cell["row"] <= hi for lo, hi in data_ranges)
                    if rule["role"] == "unit"
                    else cell["row"] >= start
                )
                if cell.get("_absent_original") or invalid_position:
                    raise ValueError(
                        f"{binding_path}.source_cell: title must precede "
                        "data; shared unit must be outside every data range"
                    )
                if isinstance(cell["raw_value"], str) and len(cell["raw_value"]) > 512:
                    raise ValueError(f"{binding_path}.source_cell: header text exceeds 512 characters")
                columns(rule["columns"], table, path=binding_path + ".columns")
                if len(rule["columns"]) > 128 or len({_column(c) for c in rule["columns"]}) != len(rule["columns"]):
                    raise ValueError(f"{binding_path}.columns: at most 128 unique target columns")
                if layout == "key_value":
                    mapped = [profile.get("value_column", 2)]
                elif layout == "curves":
                    mapped = [
                        s.get(name)
                        for s in profile.get("series", [])
                        if isinstance(s, dict)
                        for name in ("x_column", "y_column")
                    ]
                elif layout == "channels":
                    mapped = [profile.get("x_column"), *profile.get("y_columns", [])]
                else:
                    mapped = [f.get("column") for f in profile.get("fields", []) if isinstance(f, dict)]
                if any(
                    _column(c) not in {_column(m) for m in mapped if isinstance(m, (str, int))} for c in rule["columns"]
                ):
                    raise ValueError(f"{binding_path}.columns: target must be an executed mapped field or curve axis")
        for name in ("run_key", "label_key"):
            if name in profile:
                _string(profile[name], path + "." + name)
        for name in ("key_column", "value_column", "x_column"):
            if name in profile:
                column(profile[name], table, path + "." + name)
        for name in ("experiment_columns", "sample_columns", "test_columns", "y_columns"):
            if name in profile:
                columns(profile[name], table, name == "experiment_columns", path + "." + name)
        if layout == "records" and not {"sample_columns", "test_columns", "fields"} <= profile.keys():
            raise ValueError("records requires sample_columns, test_columns, fields")
        if layout == "objects" and not {"objects", "fields"} <= profile.keys():
            raise ValueError("objects requires named objects and fields")
        if layout == "key_value" and not isinstance(profile.get("run_key"), str):
            raise ValueError("key_value requires run_key")
        if layout == "channels" and not {"x_column", "y_columns"} <= profile.keys():
            raise ValueError("channels requires x_column and y_columns")
        if "calibration_rows" in profile:
            _list(profile["calibration_rows"], path + ".calibration_rows")
        for row_index, row in enumerate(profile.get("calibration_rows", [])):
            integer(row, table["nrows"], f"{path}.calibration_rows[{row_index}]")
        if "record_scope" in profile:
            scope = profile["record_scope"]
            keys(scope, {"row_range", "key_column", "value_column", "label_key"}, {"row_range"}, path + ".record_scope")
            if not isinstance(scope["row_range"], list) or len(scope["row_range"]) != 2:
                _expected(path + ".record_scope.row_range", "array [start,end]", scope["row_range"])
            for row_index, row in enumerate(scope["row_range"]):
                integer(row, table["nrows"], f"{path}.record_scope.row_range[{row_index}]")
            if scope["row_range"][0] > scope["row_range"][1]:
                raise ValueError(f"{path}.record_scope.row_range must be ordered")
            if scope["row_range"][0] > start or scope["row_range"][1] < profile.get("data_end_row", table["nrows"]):
                raise ValueError(f"{path}.record_scope must contain the mapped data block")
            for name in ("key_column", "value_column"):
                if name in scope:
                    column(scope[name], table, path + ".record_scope." + name)
            if "label_key" in scope:
                _string(scope["label_key"], path + ".record_scope.label_key")
        if "objects" in profile:
            if not isinstance(profile["objects"], dict) or not profile["objects"]:
                _expected(path + ".objects", "nonempty object of definitions", profile["objects"])
            for alias, definition in profile["objects"].items():
                object_path = f"{path}.objects.{alias}"
                keys(definition, OBJECT, {"kind", "key_columns"}, object_path)
                _string(definition["kind"], object_path + ".kind")
                columns(definition["key_columns"], table, path=object_path + ".key_columns")
                if "label_column" in definition:
                    column(definition["label_column"], table, object_path + ".label_column")
                for name in ("owner", "material", "identity_namespace"):
                    if name in definition:
                        _string(definition[name], object_path + "." + name)
                        if not definition[name].strip():
                            raise ValueError(f"{object_path}.{name} must not be empty")
            _validate_objects(profile, path)
        if "relations" in profile:
            _list(profile["relations"], path + ".relations")
        for relation_index, relation in enumerate(profile.get("relations", [])):
            relation_path = f"{path}.relations[{relation_index}]"
            keys(relation, {"from", "to", "predicate"}, {"from", "to", "predicate"}, relation_path)
            for name in ("from", "to", "predicate"):
                _string(relation[name], relation_path + "." + name)
            if relation["predicate"] not in RELATIONS:
                raise ValueError(
                    f"{relation_path}.predicate: expected common catalogue relation ID; received unknown str"
                )
            objects = profile.get("objects", {})
            if relation["from"] not in objects or relation["to"] not in objects:
                raise ValueError(f"{relation_path}: relation endpoint is not a declared object")
            rule = RELATIONS[relation["predicate"]]
            if (
                objects[relation["from"]]["kind"] not in rule["allowed_from_kinds"]
                or objects[relation["to"]]["kind"] not in rule["allowed_to_kinds"]
            ):
                raise ValueError(f"{relation_path}: relation owner kinds violate common catalogue")
        if "series" in profile:
            _list(profile["series"], path + ".series")
        for series_index, spec in enumerate(profile.get("series", [])):
            series_path = f"{path}.series[{series_index}]"
            keys(spec, SERIES, {"x_column", "y_column"}, series_path)
            column(spec["x_column"], table, series_path + ".x_column")
            column(spec["y_column"], table, series_path + ".y_column")
            columns(spec.get("extra_columns", []), table, True, series_path + ".extra_columns")
            if "label_cell" in spec and not any(
                c["address"] == spec["label_cell"] for r in table["rows"] for c in r["cells"]
            ):
                _string(spec["label_cell"], series_path + ".label_cell")
                raise ValueError(
                    f"{series_path}.label_cell: expected source cell address in this table; received invalid str"
                )
            if "condition_from_label" in spec:
                rule = spec["condition_from_label"]
                rule_path = series_path + ".condition_from_label"
                keys(
                    rule,
                    {"pattern", "parameter_id", "parameter_label"},
                    {"pattern", "parameter_id", "parameter_label"},
                    rule_path,
                )
                for name in ("pattern", "parameter_id", "parameter_label"):
                    _string(rule[name], rule_path + "." + name)
                if rule["pattern"] != HEADER_CONDITION_PATTERN:
                    raise ValueError(
                        f"{rule_path}.pattern: expected supplied "
                        "allowed_condition_pattern exactly; received unsupported str"
                    )
        if layout == "curves" and not profile.get("series"):
            raise ValueError("curves requires series")
        fields = profile.get("fields", {} if layout == "key_value" else [])
        if not isinstance(fields, dict if layout == "key_value" else list):
            _expected(
                path + ".fields", "object of source-key fields" if layout == "key_value" else "array of fields", fields
            )
        if layout in ("records", "objects") and not fields:
            raise ValueError(f"{path}.fields requires at least one source column mapping")
        iterable = fields.items() if isinstance(fields, dict) else enumerate(fields)
        for field_key, field in iterable:
            field_path = f"{path}.fields[{field_key}]" if isinstance(fields, list) else f"{path}.fields.{field_key}"
            keys(field, FIELD, {"field_id"}, field_path)
            _string(field["field_id"], field_path + ".field_id")
            if field["field_id"] not in FIELDS:
                raise ValueError(f"{field_path}.field_id: expected common catalogue field ID; received unknown str")
            if layout != "key_value" and not {"column", "target"} <= field.keys():
                raise ValueError(f"{field_path}: requires a source column and target owner")
            target = field.get("target", "run")
            _string(target, field_path + ".target")
            if layout == "records":
                kind = {"sample": "sample", "test": "reported_test", "observation": "observation"}.get(target)
            elif layout == "objects":
                kind = profile["objects"].get(target, {}).get("kind")
            else:
                kind = {"run": "instrument_run", "observation": "observation"}.get(target)
            if kind not in FIELDS[field["field_id"]]["owner_kinds"]:
                raise ValueError(f"{field_path}: target owner kind does not allow this field")
            if layout == "key_value":
                from .extractors.experimental import _cell, _column

                key_column = _column(profile.get("key_column", 1))
                end = profile.get("data_end_row", table["nrows"])
                if not isinstance(field_key, str) or not any(
                    str(_cell(table, row, key_column)["raw_value"]) == field_key for row in range(start, end + 1)
                ):
                    raise ValueError(f"{field_path}: source key is absent from the mapped key_value block")
            for name in ("column", "unit_column", "basis_column"):
                if name in field:
                    column(field[name], table, field_path + "." + name)
            for name in ("unit_cell", "header_cell"):
                if name in field:
                    from .extractors.experimental import _address

                    if not isinstance(field[name], str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", field[name]):
                        raise ValueError(f"{field_path}.{name}: expected an original source cell address")
                    cell = _address(table, field[name])
                    if cell.get("_absent_original") or (name == "header_cell" and cell["row"] >= start):
                        raise ValueError(f"{field_path}.{name}: source cell must exist and header precede data")
            if "header_scope" in field:
                scope = field["header_scope"]
                _string(scope, field_path + ".header_scope")
                if layout == "key_value" or not re.fullmatch(r"[A-Z]+[1-9][0-9]*:[A-Z]+[1-9][0-9]*", scope):
                    raise ValueError(
                        f"{field_path}.header_scope: expected bounded header rectangle for a column-based field"
                    )
                c0, r0, c1, r1 = range_boundaries(scope)
                col = (
                    column_index_from_string(field["column"])
                    if isinstance(field.get("column"), str)
                    else field.get("column")
                )
                start = (
                    ranges[0][0]
                    if ranges is not None
                    else profile.get("data_start_row", (profile.get("header_row") or 0) + 1)
                )
                if (
                    type(col) is not int
                    or not 1 <= c0 <= col <= c1 <= table["ncols"]
                    or not 1 <= r0 <= r1 < start
                    or r1 > table["nrows"]
                    or (c1 - c0 + 1) * (r1 - r0 + 1) > 2000
                ):
                    raise ValueError(
                        f"{field_path}.header_scope: must include mapped "
                        "column, stay above data, and contain <=2000 source cells"
                    )
            if "unit_row" in field:
                integer(field["unit_row"], table["nrows"], field_path + ".unit_row")
            for name in ("target", "parse", "value_kind"):
                if name in field:
                    _string(field[name], field_path + "." + name)
            if "parse" in field and (layout != "key_value" or field["parse"] != "number"):
                raise ValueError(f"{field_path}.parse: only key_value number parsing is supported")
            if "qualifiers" in field:
                qualifiers = field["qualifiers"]
                if not isinstance(qualifiers, dict):
                    _expected(field_path + ".qualifiers", "object of scalar values", qualifiers)
                for name, value in qualifiers.items():
                    if (
                        not isinstance(value, (str, int, float, bool))
                        or isinstance(value, float)
                        and not math.isfinite(value)
                    ):
                        _expected(field_path + ".qualifiers." + name, "finite string|number|boolean", value)
            qualifier_columns = field.get("qualifier_columns", {})
            if not isinstance(qualifier_columns, dict):
                _expected(field_path + ".qualifier_columns", "object of source columns", qualifier_columns)
            for name, col in qualifier_columns.items():
                column(col, table, field_path + ".qualifier_columns." + name)
            metadata_sources = field.get("qualifier_metadata_sources", {})
            if not isinstance(metadata_sources, dict):
                _expected(field_path + ".qualifier_metadata_sources", "object of sheet-name bindings", metadata_sources)
            for name, source in metadata_sources.items():
                source_path = field_path + ".qualifier_metadata_sources." + name
                keys(source, {"kind", "span"}, {"kind", "span"}, source_path)
                span = source["span"]
                qualifier = field.get("qualifiers", {}).get(name)
                if (
                    source["kind"] != "sheet_name"
                    or name in qualifier_columns
                    or table.get("format") != "xlsx"
                    or not isinstance(table.get("sheet"), str)
                    or not isinstance(qualifier, str)
                    or not isinstance(span, list)
                    or len(span) != 2
                    or any(type(value) is not int for value in span)
                    or not 0 <= span[0] < span[1] <= len(table["sheet"])
                    or table["sheet"][span[0] : span[1]] != qualifier
                ):
                    raise ValueError(
                        source_path
                        + ": sheet-name span must select the exact static qualifier from the original XLSX title"
                    )
            if "value_kind" in field and field["value_kind"] not in FIELDS[field["field_id"]]["value_kinds"]:
                raise ValueError(
                    f"{field_path}.value_kind: expected a value kind "
                    "allowed by the common field; received unsupported str"
                )
    return profiles


def _validate_objects(profile, path):
    definitions = profile["objects"]
    kinds = set().union(
        *(set(f["owner_kinds"]) for f in FIELDS.values()),
        *(set(r["allowed_from_kinds"] + r["allowed_to_kinds"]) for r in RELATIONS.values()),
    )
    for alias, definition in definitions.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", alias):
            raise ValueError(f"{path}.objects requires bounded named aliases")
        if definition["kind"] not in kinds or definition["kind"] == "source":
            raise ValueError(f"{path}.objects.{alias}: unknown scientific object kind")
        for key in ("owner", "material"):
            if key in definition and definition[key] not in definitions:
                raise ValueError(f"{path}.objects.{alias}.{key}: unknown owner/material alias")
        if definition["kind"] == "component":
            if not {"owner", "material"} <= definition.keys():
                raise ValueError(f"{path}.objects.{alias}: component requires owner/material aliases")
            if definitions[definition["owner"]]["kind"] not in ("formulation", "patent_claim") or definitions[
                definition["material"]
            ]["kind"] not in ("material", "formulation"):
                raise ValueError(f"{path}.objects.{alias}: component owner/material kinds violate common catalogue")
    pending, ready = dict(definitions), set()
    while pending:
        available = [
            alias
            for alias, definition in pending.items()
            if all(definition[key] in ready for key in ("owner", "material") if key in definition)
        ]
        if not available:
            raise ValueError(f"{path}.objects: cyclic owner/material references")
        for alias in available:
            pending.pop(alias)
            ready.add(alias)
