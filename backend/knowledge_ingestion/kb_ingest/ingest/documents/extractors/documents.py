"""Paper and patent extraction share one evidence-bound candidate model."""

from __future__ import annotations

import json

from kb_ingest.contract import FIELDS, RELATIONS
from kb_ingest.llm.prompts import load_prompt

DISCOVERY_STAGE_VERSION = "document-discovery-v1"
DISCOVERY_PROMPT_VERSION = "document-discovery-prompt-v1"
EXTRACTION_STAGE_VERSION = "document-extraction-v1"
EXTRACTION_PROMPT_VERSION = "document-extraction-prompt-v1"


def _object_schema(properties: dict) -> dict:
    # Schemas are JSON trees, not Python alias graphs. Reused fragments must be
    # independently editable (source ID enums must not constrain sample names).
    return {
        "type": "object",
        "properties": json.loads(json.dumps(properties)),
        "required": list(properties),
        "additionalProperties": False,
    }


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
STRING_ARRAY = {"type": "array", "items": STRING}
SUPPORT_SCHEMA = {"type": "array", "items": _object_schema({"block_id": STRING, "quote": STRING})}
DISCOVERY_SCHEMA = _object_schema(
    {
        "units": {
            "type": "array",
            "items": _object_schema(
                {
                    "key": STRING,
                    "label": STRING,
                    "kind": {
                        "type": "string",
                        "enum": ["study", "patent_claims", "example", "comparison", "methods", "source_metadata"],
                    },
                    "scope": STRING,
                    "block_ids": STRING_ARRAY,
                    "context_block_ids": STRING_ARRAY,
                    "visual_pages": {"type": "array", "items": {"type": "integer"}},
                    "object_labels": STRING_ARRAY,
                }
            ),
        },
        "gaps": STRING_ARRAY,
    }
)
OBJECT_KINDS = sorted(
    set(k for f in FIELDS.values() for k in f["owner_kinds"])
    | set(k for r in RELATIONS.values() for k in r["allowed_from_kinds"] + r["allowed_to_kinds"])
)
EXTRACTION_SCHEMA = _object_schema(
    {
        "objects": {
            "type": "array",
            "items": _object_schema(
                {
                    "key": STRING,
                    "kind": {"type": "string", "enum": OBJECT_KINDS},
                    "label": STRING,
                    "scope": STRING,
                    "owner_key": NULLABLE_STRING,
                    "material_key": NULLABLE_STRING,
                    "source_identifier": NULLABLE_STRING,
                    "identity_evidence": SUPPORT_SCHEMA,
                    "evidence": SUPPORT_SCHEMA,
                }
            ),
        },
        "facts": {
            "type": "array",
            "items": _object_schema(
                {
                    "key": STRING,
                    "object_key": STRING,
                    "field_id": {"type": "string", "enum": sorted(FIELDS)},
                    "raw_value": {"type": ["string", "number", "boolean", "null"]},
                    "raw_unit": NULLABLE_STRING,
                    "raw_basis": NULLABLE_STRING,
                    "value_kind": {
                        "type": "string",
                        "enum": ["scalar", "text", "category", "interval", "numeric_notation"],
                    },
                    "qualifiers": {"type": "array", "items": _object_schema({"key": STRING, "value": STRING})},
                    "statement_kind": {
                        "type": "string",
                        "enum": [
                            "source_report",
                            "source_reported_measurement",
                            "scope_claim",
                            "author_interpretation",
                            "source_reported_calculation",
                        ],
                    },
                    "formation": {
                        "type": "string",
                        "enum": ["direct", "paraphrase", "explicit_inheritance", "explicit_override"],
                    },
                    "input_fact_keys": STRING_ARRAY,
                    "evidence": SUPPORT_SCHEMA,
                }
            ),
        },
        "relations": {
            "type": "array",
            "items": _object_schema(
                {
                    "from_key": STRING,
                    "predicate": {"type": "string", "enum": sorted(RELATIONS)},
                    "to_key": STRING,
                    "applicability": STRING,
                    "basis_kind": STRING,
                    "evidence": SUPPORT_SCHEMA,
                }
            ),
        },
        "issues": {
            "type": "array",
            "items": _object_schema(
                {
                    "description": STRING,
                    "object_keys": STRING_ARRAY,
                    "severity": {"type": "string", "enum": ["warning", "error"]},
                    "evidence": SUPPORT_SCHEMA,
                }
            ),
        },
    }
)

DISCOVERY_INSTRUCTIONS = load_prompt("document-discovery-v1")

EXTRACTION_INSTRUCTIONS = load_prompt("document-extraction-v1")
