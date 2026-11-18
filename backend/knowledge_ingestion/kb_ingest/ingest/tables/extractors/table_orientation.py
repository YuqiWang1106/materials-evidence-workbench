"""Virtual orientation views with unchanged original cells for all evidence."""

from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries


def original_cell(cell):
    return cell.get("_original_cell", cell)


def evidence_cells(cells):
    return [original_cell(cell) for cell in cells if not cell.get("_absent_original")]


def oriented_table(table, profile):
    orientation = profile.get("orientation", "rows")
    if orientation == "rows":
        return table
    if orientation != "transpose" or profile.get("layout") not in ("records", "objects"):
        raise ValueError("orientation transpose supports only records and objects")
    rows = []
    for row in range(1, table["ncols"] + 1):
        cells = []
        for column in range(1, table["nrows"] + 1):
            originals = table["rows"][column - 1]["cells"]
            source = originals[row - 1] if row <= len(originals) else None
            if source is None:
                # Absent ragged fields are structural gaps, never source cells.
                source = {
                    "row": column,
                    "column": row,
                    "address": f"{get_column_letter(row)}{column}",
                    "raw_value": None,
                    "missing_kind": "absent_field",
                    "value_type": "absent",
                }
                cell = {**source, "_absent_original": True}
            else:
                cell = dict(source)
            cell.update(row=row, column=column, address=f"{get_column_letter(column)}{row}", _original_cell=source)
            cells.append(cell)
        rows.append({"row": row, "hidden": get_column_letter(row) in table.get("hidden_columns", []), "cells": cells})
    merges = []
    for region in table.get("merged_cells", []):
        c0, r0, c1, r1 = range_boundaries(region)
        merges.append(f"{get_column_letter(r0)}{c0}:{get_column_letter(r1)}{c1}")
    return {
        **table,
        "rows": rows,
        "nrows": table["ncols"],
        "ncols": table["nrows"],
        "merged_cells": merges,
        "_original_table": table,
        "_orientation": "transpose",
    }
