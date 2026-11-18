"""Source-bound document release gates; model review is not scientific validation."""

from __future__ import annotations

from copy import deepcopy

from kb_ingest.document_source_values import program_cell_source, project_source_cell

VERSION = "document-quality-2"


def resolve_cell_values(candidate, blocks):
    """The model selects cells; only the program copies the cell transcription."""
    candidate = deepcopy(candidate)
    registry = {b["block_id"]: b for b in blocks}
    for f in candidate["facts"]:
        # The provenance is made here, never accepted from model output. Keep the
        # selector so unchanged facts can be re-resolved after local corrections.
        f.pop("program_value_source", None)
        cell_id = f.get("value_cell")
        if cell_id:
            b = registry.get(cell_id)
            if not b or b["kind"] != "table_cell" or not str(b["text"]).strip():
                # Preserve the invalid selector without issuing provenance.
                # Verification rejects that target for bounded local repair;
                # independent facts in the same response remain eligible.
                continue
            try:
                projection = project_source_cell(b["text"], f.get("value_kind"), f.get("raw_value"), f.get("raw_unit"))
            except ValueError:
                # A conflicting model proposal or ambiguous transcription gets
                # no program provenance. The verifier rejects only this target.
                continue
            f["raw_value"] = projection["raw_value"]
            f["raw_unit"] = projection["raw_unit"]
            # Reading a cell does not change its semantic field/value kind.
            # The same numeral may be an amount, identifier or category.
            proof = {"block_id": cell_id, "quote": b["text"]}
            f["evidence"] = list(f.get("evidence", []))
            if proof not in f["evidence"]:
                f["evidence"].append(proof)
            f["program_value_source"] = program_cell_source(cell_id, b["text"], projection)
    return candidate
