"""JSON schemas for finite source-bound PDF table mapping templates."""

from __future__ import annotations

from copy import deepcopy

from kb_ingest.contract import FIELDS, RELATIONS

OBJECT_KINDS = sorted(
    {k for f in FIELDS.values() for k in f["owner_kinds"]}
    | {k for r in RELATIONS.values() for k in r["allowed_from_kinds"] + r["allowed_to_kinds"]}
)
STATEMENT_KINDS = [
    "source_report",
    "source_reported_measurement",
    "scope_claim",
    "author_interpretation",
    "source_reported_calculation",
]
VALUE_KINDS = ["scalar", "text", "category", "interval", "numeric_notation"]


def _object(properties):
    return {
        "type": "object",
        "properties": deepcopy(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def _array(items, minimum=0, maximum=512):
    return {"type": "array", "items": deepcopy(items), "minItems": minimum, "maxItems": maximum}


_STRING = {"type": "string", "minLength": 1, "maxLength": 4096}
_INT = {"type": "integer", "minimum": 0, "maximum": 100_000}
_SPAN = {"anyOf": [_array(_INT, 2, 2), {"type": "null"}]}
_AXIS_COORD = {"anyOf": [_INT, {"type": "string", "enum": ["current"]}]}
SELECTOR_SCHEMA = {
    "anyOf": [
        _object({"kind": {"type": "string", "enum": ["cell"]}, "cell_id": _STRING, "span": _SPAN}),
        _object(
            {
                "kind": {"type": "string", "enum": ["axis"]},
                "row": _AXIS_COORD,
                "column": _AXIS_COORD,
                "span": _SPAN,
                "merged": {"type": "boolean"},
            }
        ),
        _object({"kind": {"type": "string", "enum": ["text"]}, "block_id": _STRING, "span": _array(_INT, 2, 2)}),
        _object({"kind": {"type": "string", "enum": ["quote"]}, "block_id": _STRING, "quote": _STRING}),
    ]
}
_NULL_SELECTOR = {"anyOf": [SELECTOR_SCHEMA, {"type": "null"}]}


def _object_template_schema(*, existing=False, component=False):
    # Encode the same immutable-identity contract in model output and compiler
    # validation. An existing component must not redefine its prior links.
    null = {"type": "null"}
    kinds = (
        OBJECT_KINDS if existing else (["component"] if component else [k for k in OBJECT_KINDS if k != "component"])
    )
    return _object(
        {
            "id": _STRING,
            "kind": {"type": "string", "enum": kinds},
            "cardinality": {"type": "string", "enum": ["existing"] if existing else ["per_record", "shared"]},
            "identity": _array(SELECTOR_SCHEMA, 0 if existing else 1, 0 if existing else 32),
            "label": null if existing else SELECTOR_SCHEMA,
            "owner": _STRING if component and not existing else null,
            "material": _STRING if component and not existing else null,
            "existing_key": _STRING if existing else null,
            "evidence": _array(SELECTOR_SCHEMA, 0 if existing else 1, 0 if existing else 32),
        }
    )


OBJECT_TEMPLATE_SCHEMA = {
    "anyOf": [
        _object_template_schema(existing=True),
        _object_template_schema(component=True),
        _object_template_schema(),
    ]
}
TABLE_MAPPING_SCHEMA = _object(
    {
        "version": {"type": "string", "enum": ["1"]},
        "table_id": _STRING,
        "coordinate_base": {"type": "integer", "enum": [0]},
        "scope": _STRING,
        "selection": _object(
            {
                "rows": _array(_INT, 2, 2),
                "columns": _array(_INT, 2, 2),
                "axis": {"type": "string", "enum": ["row", "column"]},
            }
        ),
        "objects": _array(OBJECT_TEMPLATE_SCHEMA, 1, 128),
        "facts": _array(
            _object(
                {
                    "id": _STRING,
                    "target": _STRING,
                    "field_id": {"type": "string", "enum": sorted(FIELDS)},
                    "value": SELECTOR_SCHEMA,
                    "unit": _NULL_SELECTOR,
                    "basis": _NULL_SELECTOR,
                    "value_kind": {"type": "string", "enum": VALUE_KINDS},
                    "qualifiers": _array(_object({"key": _STRING, "source": SELECTOR_SCHEMA}), 0, 32),
                    "statement_kind": {"type": "string", "enum": STATEMENT_KINDS},
                    "evidence": _array(SELECTOR_SCHEMA, 1, 32),
                }
            ),
            0,
            256,
        ),
        "relations": _array(
            _object(
                {
                    "id": _STRING,
                    "from": _STRING,
                    "to": _STRING,
                    "predicate": {"type": "string", "enum": sorted(RELATIONS)},
                    "evidence": _array(SELECTOR_SCHEMA, 1, 32),
                }
            ),
            0,
            256,
        ),
    }
)
