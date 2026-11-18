"""Lossless deduplication of already-observed evidence for a frozen turn window.

Turn text, table scope, support IDs and call/artifact provenance stay on each
turn. Content-addressed results live in ``evidence_catalog.results``. Successful
read results replace only ``rows`` with ``observed_cell_refs``; each reference
contains the original row number and ordered original column numbers. The full
cell dictionaries live once per immutable table version/row/column in
``evidence_catalog.observed_cells``. Absent cells are never manufactured.
Confirmed source headers may seed the same cell catalog; ``table_headers``
keeps their complete column metadata and coordinates without creating calls or
analysis supports for these source facts.

Existing bounded analysis outputs are preserved, including group statistics and
their explicit omissions. Compatible artifact wrappers may reference that same
output; otherwise their existing artifact projection is preserved separately.
This module never recomputes, truncates or performs I/O. The caller enforces the
context limit after projecting the complete window.
"""

from copy import deepcopy

from .contract import strict_equal
from .core import StateConflict, fingerprint

INPUT_POLICY_VERSION = "table-summary-input-v2"
_READ_TOOLS = {"inspect_table", "read_table_rows"}
_ANALYSIS_KINDS = {"analyze_table": "table_analysis", "compare_tables": "table_comparison"}


def _changed():
    raise StateConflict("SUMMARY_INPUT_CHANGED")


def _contains_projection(projected, output):
    """An old artifact projection must add no facts missing from the tool output.

    The existing artifact projector renames operation ``n`` to ``point_count``.
    That exact alias is the only value transformation allowed when checking a
    shared result. JSON equality preserves distinctions such as zero/false.
    """
    if isinstance(projected, dict):
        if not isinstance(output, dict):
            return False
        for key, value in projected.items():
            target = "n" if key == "point_count" and key not in output and "n" in output else key
            if target not in output or not _contains_projection(value, output[target]):
                return False
        return True
    if isinstance(projected, list):
        return (
            isinstance(output, list)
            and len(projected) == len(output)
            and all(_contains_projection(left, right) for left, right in zip(projected, output))
        )
    return strict_equal(projected, output)


def _artifact_matches_output(artifact, output, expected_kind=None):
    if not isinstance(artifact, dict) or not isinstance(output, dict):
        return False
    if expected_kind is not None and artifact.get("kind") != expected_kind:
        return False
    allowed = {"kind", "table_version_id", "config", "result", "comparison_mode", "joined", "unit_conversion", "tables"}
    if set(artifact) - allowed:
        return False
    for key, value in artifact.items():
        if key == "kind":
            continue
        if key == "result":
            if not _contains_projection(value, output):
                return False
        elif key == "tables":
            targets = output.get("tables")
            if (
                not isinstance(value, list)
                or not isinstance(targets, list)
                or len(value) != len(targets)
                or not all(_artifact_matches_output(left, right) for left, right in zip(value, targets))
            ):
                return False
        elif key not in output or not strict_equal(value, output[key]):
            return False
    return "result" in artifact or "tables" in artifact


def _read_projection(output, observed):
    table_id = output.get("table_version_id")
    if not isinstance(table_id, str) or not table_id or not isinstance(output.get("rows"), list):
        return deepcopy(output)
    if "observed_cell_refs" in output:
        _changed()
    references = []
    table = observed.setdefault(table_id, {})
    for row in output["rows"]:
        if (
            not isinstance(row, dict)
            or type(row.get("row")) is not int
            or row["row"] < 1
            or not isinstance(row.get("cells"), list)
            or "columns" in row
        ):
            _changed()
        row_number = row["row"]
        cells = table.setdefault(row_number, {})
        columns = []
        for cell in row["cells"]:
            if (
                not isinstance(cell, dict)
                or type(cell.get("column")) is not int
                or cell["column"] < 1
                or type(cell.get("row")) is not int
                or cell["row"] != row_number
            ):
                _changed()
            column = cell["column"]
            if column in cells and not strict_equal(cells[column], cell):
                _changed()
            if column not in cells:
                cells[column] = deepcopy(cell)
            columns.append(column)
        references.append(
            {**{key: deepcopy(value) for key, value in row.items() if key != "cells"}, "columns": columns}
        )
    return {
        **{key: deepcopy(value) for key, value in output.items() if key != "rows"},
        "observed_cell_refs": references,
    }


def project_turn_evidence(messages, table_headers=None):
    """Return complete turns and a shared evidence catalog without input mutation.

    ``table_headers`` contains already-verified immutable source header rows,
    keyed by table version. These rows share cell identities with tool reads,
    but remain explicitly identified as source headers, not tool results.
    """
    results, observed, turns = {}, {}, []
    headers = {}
    for table_id, header in (table_headers or {}).items():
        if (
            not isinstance(header, dict)
            or header.get("table_version_id") != table_id
            or not isinstance(table_id, str)
            or not table_id
            or not isinstance(header.get("rows"), list)
        ):
            _changed()
        headers[table_id] = _read_projection(header, observed)

    def catalog(kind, value):
        key = "output" if kind == "tool_output" else "artifact"
        result = {"kind": kind, key: deepcopy(value)}
        identifier = "evidence-" + fingerprint(result)
        if identifier in results and not strict_equal(results[identifier], result):
            _changed()
        results.setdefault(identifier, result)
        return identifier

    for message in messages:
        projected = deepcopy(message)
        projected_calls = []
        for call in message["tool_calls"]:
            item = deepcopy(call)
            if "output" in call:
                output = call["output"]
                if (
                    call.get("name") in _READ_TOOLS
                    and call.get("status") == "completed"
                    and isinstance(output, dict)
                    and output.get("ok") is True
                ):
                    output = _read_projection(output, observed)
                item.pop("output")
                item["output_ref"] = catalog("tool_output", output)
            projected_calls.append(item)
        projected["tool_calls"] = projected_calls

        projected_artifacts = []
        for wrapper in message["artifacts"]:
            item = deepcopy(wrapper)
            artifact = item.pop("artifact")
            reference = None
            for call, projected_call in zip(message["tool_calls"], projected_calls):
                output = call.get("output")
                kind = _ANALYSIS_KINDS.get(call.get("name"))
                if (
                    kind is not None
                    and call.get("status") == "completed"
                    and isinstance(output, dict)
                    and output.get("ok") is True
                    and call.get("artifact_id") == wrapper.get("artifact_id")
                    and call.get("call_id") == wrapper.get("call_id")
                    and wrapper.get("turn_id") == message.get("turn_id")
                    and _artifact_matches_output(artifact, output, kind)
                ):
                    reference = projected_call["output_ref"]
                    break
            item["artifact_ref"] = reference or catalog("artifact_projection", artifact)
            if reference is not None:
                item["coverage"] = {
                    "projection": "existing-tool-output-v2",
                    "full_artifact_stored": True,
                    "limitations": "引用既有工具输出；实际范围及省略以所引用 evidence_catalog.results 输出中的 "
                    "coverage 与 omitted 计数字段为准，不代表完整 artifact 已进入上下文",
                }
            projected_artifacts.append(item)
        projected["artifacts"] = projected_artifacts
        turns.append(projected)

    cells = {
        table_id: {
            "table_version_id": table_id,
            "rows": [
                {"row": row, "cells": [columns[column] for column in sorted(columns)]}
                for row, columns in sorted(rows.items())
            ],
        }
        for table_id, rows in observed.items()
    }
    return {
        "input_policy_version": INPUT_POLICY_VERSION,
        "turns": turns,
        "evidence_catalog": {"results": results, "observed_cells": cells, "table_headers": headers},
    }
