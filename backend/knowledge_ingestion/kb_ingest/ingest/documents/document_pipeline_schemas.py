"""JSON schemas and frozen field-contract prompt used by document v2 extraction."""

from __future__ import annotations

from copy import deepcopy

from kb_ingest.contract import FIELDS
from kb_ingest.document_value_contract import VALUE_CONTRACT_INSTRUCTIONS
from kb_ingest.ingest.documents.extractors.documents import (
    DISCOVERY_SCHEMA,
    EXTRACTION_SCHEMA,
    STRING,
    STRING_ARRAY,
    SUPPORT_SCHEMA,
    _object_schema,
)
from kb_ingest.llm.prompts import load_prompt

PLAN_SCHEMA = deepcopy(DISCOVERY_SCHEMA)
unit_schema = PLAN_SCHEMA["properties"]["units"]["items"]
unit_schema["properties"]["depends_on"] = STRING_ARRAY
unit_schema["required"].append("depends_on")
PLAN_SCHEMA["properties"]["excluded"] = {
    "type": "array",
    "items": _object_schema({"block_id": STRING, "reason": STRING}),
}
PLAN_SCHEMA["required"].append("excluded")
EXTRACT_SCHEMA = deepcopy(EXTRACTION_SCHEMA)
fact_schema = EXTRACT_SCHEMA["properties"]["facts"]["items"]
fact_schema["properties"]["raw_value"] = {"type": ["string", "boolean", "null"]}
fact_schema["properties"]["value_cell"] = {"type": ["string", "null"]}
fact_schema["required"].append("value_cell")
for section in ("objects", "facts", "relations"):
    EXTRACT_SCHEMA["properties"][section]["items"]["properties"]["evidence"]["minItems"] = 1
issue_schema = EXTRACT_SCHEMA["properties"]["issues"]["items"]
issue_schema["properties"]["category"] = {
    "type": "string",
    "enum": ["source_not_reported", "reading_failed", "extraction_uncertain", "source_conflict", "unsupported_layout"],
}
issue_schema["required"].append("category")
relation_schema = EXTRACT_SCHEMA["properties"]["relations"]["items"]["properties"]
relation_schema["applicability"] = {
    "type": "string",
    "enum": ["as_reported", "requires_review", "unknown", "not_applicable"],
}
relation_schema["basis_kind"] = {
    "type": "string",
    "enum": ["source_explicit", "explicit_inheritance", "explicit_override", "model_source_proposal"],
}
REVIEW_SCHEMA = _object_schema(
    {
        "checks": {
            "type": "array",
            "items": _object_schema(
                {
                    "target": STRING,
                    "verdict": {"type": "string", "enum": ["supported", "unsupported", "uncertain"]},
                    "reason": STRING,
                    "evidence": SUPPORT_SCHEMA,
                }
            ),
        }
    }
)
VISUAL_SCHEMA = _object_schema({"transcript": STRING, "uncertainties": STRING_ARRAY})
CORRECTION_SCHEMA = deepcopy(EXTRACT_SCHEMA)
CORRECTION_SCHEMA["properties"]["withdrawals"] = {
    "type": "array",
    "items": _object_schema({"target": STRING, "reason": STRING, "evidence": SUPPORT_SCHEMA}),
}
CORRECTION_SCHEMA["required"].append("withdrawals")
COVERAGE_SCHEMA = _object_schema(
    {
        "checked_block_ids": STRING_ARRAY,
        "missing_items": {
            "type": "array",
            "items": _object_schema(
                {
                    "description": STRING,
                    "evidence": SUPPORT_SCHEMA,
                    "field_id": {"type": "string", "enum": sorted(FIELDS)},
                    "owner_kind": STRING,
                    "value_kind": STRING,
                }
            ),
        },
        "out_of_catalogue": {
            "type": "array",
            "items": _object_schema({"description": STRING, "reason": STRING, "evidence": SUPPORT_SCHEMA}),
        },
        "uncertainties": STRING_ARRAY,
    }
)


FIELD_CONTRACT_INSTRUCTIONS = load_prompt("document-field-contract-v1") + VALUE_CONTRACT_INSTRUCTIONS
