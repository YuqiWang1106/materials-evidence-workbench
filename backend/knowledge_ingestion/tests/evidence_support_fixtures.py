"""Synthetic inputs for the document evidence contract.

Only fixture declarations needed by test_document_evidence_context are included;
sample labels, material, method and measurement examples are fictional.
"""
from copy import deepcopy

QUOTE = (
    "Study 1: Sample A contains 12.50 g Material X. Sample B contains 20 g Material X. "
    "Both samples follow Method M. Sample A width is 28.0 mm and length 45 mm at 23 °C. "
    "Sample B width is 30.0 mm. Prepared at 200 °C. Example 2 follows Example 1 except Material X is 15 g. "
    "Claim 1 requires 10-30 g Material X. The authors describe an observation."
)

EVIDENCE = [{"block_id": "b1", "quote": QUOTE}]

def field(field_id, value, *, unit=None, basis=None, statement="source_report", **extra):
    return {
        "field_id": field_id,
        "raw_value": value,
        "raw_unit": unit,
        "raw_basis": basis,
        "value_kind": "text",
        "qualifiers": [],
        "statement_kind": statement,
        "formation": "direct",
        "input_field_paths": [],
        "value_cell": None,
        "evidence": deepcopy(EVIDENCE),
        **extra,
    }

def node(label, fields=None, **extra):
    return {"label": label, "source_identifier": None, "evidence": deepcopy(EVIDENCE), "fields": fields or [], **extra}

def component(value, unit):
    return node(
        "Material X",
        [field("composition.amount", value, unit=unit)],
        material=node("Material X", [field("material.identity", "Material X")]),
    )

def reported_test_node(label, value, unit):
    return node(
        label,
        method=None,
        method_ref={"method_path": "/shared_methods/0", "evidence": deepcopy(EVIDENCE)},
        conditions=[field("condition.parameter", "23", unit="°C")],
        observations=[
            node(label, [field("observation.value", value, unit=unit, statement="source_reported_measurement")])
        ],
    )

def sample(label, amount, unit, value):
    return node(
        label,
        formulation=node(label, components=[component(amount, unit)]),
        preparation_steps=[node("Extrusion", [field("process.parameter", "200", unit="°C")])],
        tests=[reported_test_node("width", value, "mm")],
        interpretations=[],
    )

def study(label="Study 1"):
    return node(label, [field("study.scope", label)], kind="study", samples=[], interpretations=[], inherits_from=[])

def payload():
    result = {
        "source": None,
        "shared_methods": [node("Method M", [field("method.standard", "Method M")])],
        "studies": [study()],
        "patent_claims": [],
        "issues": [],
        "unreported_fields": [],
    }
    result["studies"][0]["samples"] = [
        sample("Sample A", "12.50", "g", "28.0"),
        sample("Sample B", "20", "g", "30.0"),
    ]
    result["studies"][0]["samples"][0]["tests"].append(reported_test_node("length", "45", "mm"))
    return result
