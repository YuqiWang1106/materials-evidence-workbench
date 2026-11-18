"""Read tabular source evidence without scientific interpretation or execution.

Rows and columns are one-based. CSV values stay strings, including empty strings.
Excel values are the parser's typed values, not a reconstruction of displayed text.
XLSX formulas and cached values are separate. xlrd exposes XLS cached values only;
that limitation is explicitly returned and the unchanged asset remains authoritative.
"""

from __future__ import annotations

import csv
import json
import math
import re
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from xml.parsers import expat
from zipfile import ZipFile

import openpyxl
import xlrd
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

READER_VERSION = "tables-4"
STORED_VALUE_READER_VERSION = "tables-5-stored-values"
FORMULA_MODES = ("preserve", "stored_value")


def _json_value(value):
    if isinstance(value, timedelta):
        return {"kind": "duration", "days": value.days, "seconds": value.seconds, "microseconds": value.microseconds}
    if isinstance(value, ArrayFormula):
        return value.text if value.text is not None else {"kind": "excel_array_formula", "ref": value.ref}
    if isinstance(value, DataTableFormula):
        return {"kind": "excel_data_table_formula", **_formula_attributes(value)}
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def _cell(row, column, value, value_type, **extra):
    if isinstance(value, timedelta):
        extra["source_value_type"] = value_type
        value_type = "duration"
    return {
        "row": row,
        "column": column,
        "address": f"{get_column_letter(column)}{row}",
        "raw_value": _json_value(value),
        "value_type": value_type,
        "missing_kind": "null_cell" if value is None else "empty_string" if value == "" else None,
        **extra,
    }


def _limit(rows, columns, options):
    if rows * columns > options.get("max_cells", 1_000_000):
        raise ValueError("Table exceeds configured max_cells")


_DIALECT_DEFAULTS = {
    "delimiter": ",",
    "quotechar": '"',
    "escapechar": None,
    "doublequote": True,
    "skipinitialspace": False,
    "quoting": csv.QUOTE_MINIMAL,
    "strict": True,
    "lineterminator": "\r\n",
}


def _configured_dialect(options):
    configured = options.get("dialect", {})
    if isinstance(configured, str):
        named = csv.get_dialect(configured)
        configured = {key: getattr(named, key) for key in _DIALECT_DEFAULTS}
        # Registry dialects often default to lenient parsing, unsuitable for evidence.
        configured["strict"] = True
    if not isinstance(configured, dict) or set(configured) - _DIALECT_DEFAULTS.keys():
        raise ValueError("dialect must name a CSV dialect or contain only CSV dialect parameters")
    configured = {**configured, **{key: options[key] for key in _DIALECT_DEFAULTS if key in options}}
    if configured.get("strict", True) is not True:
        raise ValueError("Evidence reading requires strict CSV parsing")
    if configured.get("quoting", csv.QUOTE_MINIMAL) not in (csv.QUOTE_MINIMAL, csv.QUOTE_ALL, csv.QUOTE_NONE):
        raise ValueError("CSV numeric-coercing quoting modes cannot preserve original string fields")
    return configured


def _csv_records(text, dialect, options):
    reader = csv.reader(StringIO(text, newline=""), **dialect)
    previous_line, cell_count = 0, 0
    for values in reader:
        cell_count += len(values)
        if cell_count > options.get("max_cells", 1_000_000):
            raise ValueError("Table exceeds configured max_cells")
        yield values, [previous_line + 1, reader.line_num]
        previous_line = reader.line_num


def _csv_selection(text, configured, options):
    if "delimiter" in configured:
        return {**_DIALECT_DEFAULTS, **configured}, "explicit_dialect", []
    candidates, diagnostics, singles = [], [], []
    for delimiter in ",;\t|":
        if "quotechar" in configured or configured.get("quoting") == csv.QUOTE_NONE:
            quotes = [configured.get("quotechar", _DIALECT_DEFAULTS["quotechar"])]
        else:
            # A quote style may first occur anywhere in an instrument export.
            # Scan the complete unchanged source for possible field-opening
            # quotes, then compare complete parses rather than trusting a prefix.
            spacing = " *" if configured.get("skipinitialspace") else ""
            pattern = r"(?:^|[" + re.escape(delimiter) + r"\r\n])" + spacing + r"(['\"])"
            quotes = sorted({m.group(1) for m in re.finditer(pattern, text)}) or ['"']
        for quote in quotes:
            dialect = {**_DIALECT_DEFAULTS, "delimiter": delimiter, "quotechar": quote, **configured}
            try:
                widths, digest = [], sha256()
                for values, physical_lines in _csv_records(text, dialect, options):
                    digest.update(
                        json.dumps([values, physical_lines], ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    digest.update(b"\n")
                    if values:
                        widths.append(len(values))
            except csv.Error as error:
                diagnostics.append({"dialect": dialect, "status": "parse_error", "description": str(error)})
                continue
            supported = bool(widths) and widths[0] > 1 and (len(widths) == 1 or widths[1] > 1)
            signature = digest.hexdigest()
            diagnostics.append(
                {
                    "dialect": dialect,
                    "status": "candidate" if supported else "insufficient_structure",
                    "logical_records": len(widths),
                    "multi_field_records": sum(w > 1 for w in widths),
                    "min_columns": min(widths, default=0),
                    "max_columns": max(widths, default=0),
                    "interpretation_sha256": signature,
                }
            )
            if supported:
                candidates.append((dialect, signature))
            if max(widths, default=0) <= 1:
                singles.append((dialect, signature))
    if candidates and len({signature for _, signature in candidates}) == 1:
        return candidates[0][0], "unique_structure_candidate", diagnostics
    if (
        not candidates
        and singles
        and len(singles) == len(diagnostics)
        and len({signature for _, signature in singles}) == 1
    ):
        # Retain the chosen quote rules too. Replacing them with comma/doublequote
        # defaults would corrupt genuinely single-column single-quoted sources.
        return singles[0][0], "single_column_structure" if text else "empty_source", diagnostics
    return None, "ambiguous_dialect", diagnostics


def _csv(path, options):
    encoding = options.get("encoding", "utf-8-sig")
    with path.open("r", encoding=encoding, newline="") as stream:
        text = stream.read()
    configured = _configured_dialect(options)
    dialect, basis, candidates = _csv_selection(text, configured, options)
    safe, rows, issues = dialect is not None, [], []
    if safe:
        for n, (values, physical_lines) in enumerate(_csv_records(text, dialect, options), 1):
            rows.append(
                {
                    "row": n,
                    "physical_lines": physical_lines,
                    "hidden": False,
                    "cells": [_cell(n, c, v, "string") for c, v in enumerate(values, 1)],
                }
            )
    else:
        issues.append(
            {
                "code": "csv_dialect_ambiguous",
                "severity": "error",
                "description": (
                    "CSV dialect is ambiguous; decoded text retained without assigning cells. Configure "
                    "delimiter and quoting explicitly."
                ),
            }
        )
    diagnostics = {
        "status": "parsed" if safe else "requires_configuration",
        "selection_basis": basis,
        "candidate_dialects": candidates,
        "effective_reader_options": {"encoding": encoding, "dialect": dialect or configured},
    }
    table = {
        "format": "csv",
        "sheet": None,
        "reader_version": READER_VERSION,
        "encoding": encoding,
        "delimiter": dialect["delimiter"] if safe else None,
        "dialect": dialect,
        "rows": rows,
        "extraction_safe": safe,
        "parse_diagnostics": diagnostics,
        "nrows": len(rows),
        "ncols": max((len(r["cells"]) for r in rows), default=0),
        "merged_cells": [],
        "hidden": False,
        "issues": issues,
        "value_representation": "decoded_csv_field; asset retains quoting and bytes",
    }
    if not safe:
        table["raw_text"] = text
    return [table]


def _formula_attributes(value):
    if isinstance(value, ArrayFormula):
        return {"ref": value.ref}
    attributes = {key: getattr(value, key) for key in ("ref", "ca", "dt2D", "dtr", "r1", "r2", "del1", "del2")}
    # openpyxl's data-table reader leaves XML boolean tokens as strings.
    for key in ("ca", "dt2D", "dtr", "del1", "del2"):
        if attributes[key] not in (True, False, "1", "0", "true", "false"):
            raise ValueError(f"Invalid data-table formula boolean attribute: {key}")
        attributes[key] = attributes[key] in (True, "1", "true")
    return attributes


def _formula_metadata(cell):
    value = cell.value
    kind = (
        "array"
        if isinstance(value, ArrayFormula)
        else "data_table"
        if isinstance(value, DataTableFormula)
        else "scalar"
    )
    expression = value if isinstance(value, str) else value.text if isinstance(value, ArrayFormula) else None
    return {
        "formula": expression,
        "formula_kind": kind,
        "formula_origin": cell.coordinate,
        "formula_ref": getattr(value, "ref", cell.coordinate),
        "formula_attributes": _formula_attributes(value) if kind != "scalar" else {},
    }


def _xlsx_preflight(archive, options):
    """Bound worksheet rectangles before either in-memory workbook is allocated.

    Stream XML without constructing a tree. Stale dimension hints are ignored:
    stored cells, merged regions and array formula extents count. Scan XML
    parts by root type rather than assuming standard worksheet filenames.
    """
    total_cells = 0
    for part in archive.infolist():
        if not part.filename.endswith(".xml"):
            continue
        parser = expat.ParserCreate(namespace_separator="}")
        bounds = {"row": 1, "col": 1, "current_row": 0, "current_col": 0, "worksheet": False, "depth": 0}

        def start(name, attributes):
            bounds["depth"] += 1
            if bounds["depth"] > 256:
                raise ValueError("Workbook XML nesting exceeds safety boundary")
            local = name.rsplit("}", 1)[-1]
            if bounds["depth"] == 1:
                bounds["worksheet"] = local == "worksheet"
            if not bounds["worksheet"]:
                return
            if local == "row":
                bounds["current_row"] = int(attributes.get("r", bounds["current_row"] + 1))
                bounds["current_col"] = 0
            if local == "c":
                bounds["current_col"] += 1
                if not attributes.get("r"):
                    bounds["row"] = max(bounds["row"], bounds["current_row"])
                    bounds["col"] = max(bounds["col"], bounds["current_col"])
            reference = (
                attributes.get("ref") if local in ("mergeCell", "f") else attributes.get("r") if local == "c" else None
            )
            if reference:
                try:
                    _, _, column, row = range_boundaries(reference)
                    if row is None or column is None:
                        raise ValueError("Unbounded workbook range")
                    bounds["row"] = max(bounds["row"], row)
                    bounds["col"] = max(bounds["col"], column)
                    if local == "c":
                        bounds["current_col"] = column
                except (TypeError, ValueError):
                    raise ValueError("Invalid workbook source range") from None
            _limit(bounds["row"], bounds["col"], options)

        def end(_name):
            bounds["depth"] -= 1

        def forbid_doctype(*_args):
            raise ValueError("Workbook XML DTD is unsupported")

        parser.StartElementHandler = start
        parser.EndElementHandler = end
        parser.StartDoctypeDeclHandler = forbid_doctype
        with archive.open(part) as source:
            while chunk := source.read(65536):
                parser.Parse(chunk, False)
            parser.Parse(b"", True)
        if bounds["worksheet"]:
            total_cells += bounds["row"] * bounds["col"]
            if total_cells > options.get("max_cells", 1_000_000):
                raise ValueError("Workbook total exceeds configured max_cells")


def _xlsx(path, options):
    stored_values = options.get("formula_mode", "preserve") == "stored_value"
    with ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist()) > options.get("max_uncompressed_bytes", 128_000_000):
            raise ValueError("Workbook exceeds configured max_uncompressed_bytes")
        _xlsx_preflight(archive, options)
    formulas = openpyxl.load_workbook(path, data_only=False, keep_links=False)
    cached = openpyxl.load_workbook(path, data_only=True, keep_links=False)
    tables = []
    try:
        for sheet in formulas:
            _limit(sheet.max_row, sheet.max_column, options)
            cache_sheet = cached[sheet.title]
            formula_cells, max_row, max_column = {}, sheet.max_row, sheet.max_column
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.data_type != "f":
                        continue
                    metadata = _formula_metadata(cell)
                    c0, r0, c1, r1 = range_boundaries(metadata["formula_ref"])
                    max_row, max_column = max(max_row, r1), max(max_column, c1)
                    _limit(max_row, max_column, options)
                    for r in range(r0, r1 + 1):
                        for c in range(c0, c1 + 1):
                            if (r, c) in formula_cells:
                                raise ValueError("Workbook has overlapping formula ranges")
                            formula_cells[(r, c)] = metadata
            merged_origins = {}
            merges = sorted(str(r) for r in sheet.merged_cells.ranges)
            for region in sheet.merged_cells.ranges:
                origin = f"{get_column_letter(region.min_col)}{region.min_row}"
                for r in range(region.min_row, region.max_row + 1):
                    for c in range(region.min_col, region.max_col + 1):
                        merged_origins[(r, c)] = origin
            rows = []
            for n, row in enumerate(sheet.iter_rows(max_row=max_row, max_col=max_column), 1):
                cells = []
                for cell in row:
                    value, value_type = cell.value, cell.data_type
                    extra = {
                        "number_format": cell.number_format,
                        "merged_origin": merged_origins.get((cell.row, cell.column)),
                        "display_text": None,
                    }
                    if (cell.row, cell.column) in formula_cells:
                        extra.update(formula_cells[(cell.row, cell.column)])
                        cached_value = cache_sheet.cell(cell.row, cell.column).value
                        extra.update(
                            cached_value=_json_value(cached_value),
                            cache_status="available" if cached_value is not None else "unavailable",
                            cache_freshness="not_verified",
                        )
                        if stored_values:
                            # One value representation feeds preview, execution and
                            # evidence replay. Never calculate or follow references.
                            # Keep the expression only as provenance, outside the
                            # downstream formula-expression interpretation path.
                            source_formula = {
                                key: extra.pop(key)
                                for key in (
                                    *formula_cells[(cell.row, cell.column)],
                                    "cached_value",
                                    "cache_status",
                                    "cache_freshness",
                                )
                            }
                            extra["source_formula"] = source_formula
                            value = cached_value
                            value_type = cache_sheet.cell(cell.row, cell.column).data_type
                            if cached_value is None:
                                extra["missing_kind"] = "formula_cache_unavailable"
                    if cell.comment:
                        extra["comment"] = {"text": cell.comment.text, "author": cell.comment.author}
                    cells.append(_cell(cell.row, cell.column, value, value_type, **extra))
                rows.append({"row": n, "hidden": bool(sheet.row_dimensions[n].hidden), "cells": cells})
            hidden_columns = sorted(
                {
                    c
                    for k, d in sheet.column_dimensions.items()
                    if d.hidden
                    for c in range(d.min or column_index_from_string(k), (d.max or column_index_from_string(k)) + 1)
                }
            )
            tables.append(
                {
                    "format": "xlsx",
                    "sheet": sheet.title,
                    "reader_version": STORED_VALUE_READER_VERSION if stored_values else READER_VERSION,
                    "rows": rows,
                    "nrows": max_row,
                    "ncols": max_column,
                    "merged_cells": merges,
                    "hidden": sheet.sheet_state != "visible",
                    "hidden_columns": [get_column_letter(c) for c in hidden_columns],
                    "extraction_safe": True,
                    "parse_diagnostics": {
                        "status": "parsed",
                        "selection_basis": "xlsx_typed_cells",
                        "effective_reader_options": {"formula_mode": "stored_value"} if stored_values else {},
                    },
                    "issues": [],
                    "value_representation": "openpyxl typed value; display text not rendered",
                }
            )
    finally:
        formulas.close()
        cached.close()
    return tables


def _xls(path, options):
    book = xlrd.open_workbook(path, formatting_info=True)
    tables = []
    types = {
        xlrd.XL_CELL_EMPTY: "empty",
        xlrd.XL_CELL_TEXT: "string",
        xlrd.XL_CELL_NUMBER: "number",
        xlrd.XL_CELL_DATE: "excel_date_serial",
        xlrd.XL_CELL_BOOLEAN: "boolean",
        xlrd.XL_CELL_ERROR: "error",
        xlrd.XL_CELL_BLANK: "blank",
    }
    try:
        for sheet in book.sheets():
            _limit(sheet.nrows, sheet.ncols, options)
            merged_origins, merges = {}, []
            for r0, r1, c0, c1 in sheet.merged_cells:
                origin = f"{get_column_letter(c0 + 1)}{r0 + 1}"
                merges.append(f"{origin}:{get_column_letter(c1)}{r1}")
                for r in range(r0 + 1, r1 + 1):
                    for c in range(c0 + 1, c1 + 1):
                        merged_origins[(r, c)] = origin
            rows = []
            for r in range(sheet.nrows):
                cells = []
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    value = bool(cell.value) if cell.ctype == xlrd.XL_CELL_BOOLEAN else cell.value
                    xf = book.xf_list[cell.xf_index]
                    number_format = book.format_map[xf.format_key].format_str
                    cells.append(
                        _cell(
                            r + 1,
                            c + 1,
                            value,
                            types.get(cell.ctype, "unknown"),
                            number_format=number_format,
                            display_text=None,
                            merged_origin=merged_origins.get((r + 1, c + 1)),
                        )
                    )
                rows.append(
                    {"row": r + 1, "hidden": bool(getattr(sheet.rowinfo_map.get(r), "hidden", False)), "cells": cells}
                )
            tables.append(
                {
                    "format": "xls",
                    "sheet": sheet.name,
                    "reader_version": READER_VERSION,
                    "rows": rows,
                    "nrows": sheet.nrows,
                    "ncols": sheet.ncols,
                    "merged_cells": sorted(merges),
                    "hidden": bool(sheet.visibility),
                    "hidden_columns": [
                        get_column_letter(c + 1) for c, info in sheet.colinfo_map.items() if info.hidden
                    ],
                    "excel_date_mode": book.datemode,
                    "extraction_safe": True,
                    "parse_diagnostics": {
                        "status": "parsed_with_limitations",
                        "selection_basis": "xls_cached_typed_cells",
                        "effective_reader_options": {},
                    },
                    "issues": [
                        {
                            "code": "xls_formula_expression_unavailable",
                            "description": (
                                "xlrd exposes XLS cached cell values, not formula expressions; formula "
                                "provenance is not established. Original workbook retained."
                            ),
                        }
                    ],
                    "value_representation": "xlrd cached typed value; display text not rendered",
                }
            )
    finally:
        book.release_resources()
    return tables


def read_tables(path: str | Path, *, options: dict | None = None) -> list[dict]:
    """Read CSV/XLS/XLSX. Unsupported or corrupt inputs raise, never return fake success."""
    path, options = Path(path), options or {}
    if options.get("formula_mode", "preserve") not in FORMULA_MODES:
        raise ValueError("formula_mode must be preserve or stored_value; formulas are never evaluated")
    if path.stat().st_size > options.get("max_bytes", 50_000_000):
        raise ValueError("Table asset exceeds configured max_bytes")
    extension = path.suffix.lower()
    handler = {".csv": _csv, ".xls": _xls, ".xlsx": _xlsx}.get(extension)
    if handler is None:
        raise ValueError(f"Unsupported table format: {extension}")
    try:
        return handler(path, options)
    except Exception as error:
        # Keep caller-visible failure explicit; the service records retryable job errors.
        raise ValueError(f"Cannot read {extension} table ({type(error).__name__}): {error}") from error
