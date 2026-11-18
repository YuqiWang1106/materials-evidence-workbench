"""Strict nested source output and deterministic conversion to document candidates.

The model describes source-bound scientific entities, never a free-form graph.
This module checks structure and explicit references; source truth still requires
literal evidence verification and independent review in ``document_quality``.
"""

from __future__ import annotations

import math
from copy import deepcopy

from . import contract, core
from .document_value_contract import VALUE_CONTRACT_INSTRUCTIONS
from .ingest.documents.extractors.documents import NULLABLE_STRING, STRING, SUPPORT_SCHEMA, _object_schema
from .llm.prompts import load_prompt


def _array(items):
    return {"type": "array", "items": items}


def _nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


_SUPPORT = {**deepcopy(SUPPORT_SCHEMA), "minItems": 1}
_SUPPORT["items"]["properties"]["quote"] = NULLABLE_STRING
_ISSUE_SUPPORT = deepcopy(_SUPPORT)
_ISSUE_SUPPORT.pop("minItems")
_FIELD = _object_schema(
    {
        "field_id": {"type": "string", "enum": sorted(contract.FIELDS)},
        "raw_value": {"type": ["string", "boolean", "null"]},
        "raw_unit": NULLABLE_STRING,
        "raw_basis": NULLABLE_STRING,
        "value_kind": {"type": "string", "enum": ["text", "scalar", "category", "interval", "numeric_notation"]},
        "qualifiers": _array(_object_schema({"key": STRING, "value": STRING})),
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
        "formation": {"type": "string", "enum": ["direct", "paraphrase", "explicit_inheritance", "explicit_override"]},
        "input_field_paths": _array(STRING),
        "value_cell": NULLABLE_STRING,
        "evidence": _SUPPORT,
    }
)


def _fields(kind, *, conditions=False):
    # Structural roles are selected before type branches. Catalogue kinds that
    # the document wire format cannot express are not advertised to the model.
    groups = {}
    for field_id, definition in sorted(contract.FIELDS.items()):
        if kind not in definition["owner_kinds"]:
            continue
        if conditions:
            if field_id != "condition.parameter":
                continue
        elif kind == "reported_test" and field_id in ("observation.value", "condition.parameter"):
            continue
        kinds = tuple(k for k in _FIELD["properties"]["value_kind"]["enum"] if k in definition["value_kinds"])
        if kinds:
            groups.setdefault(kinds, []).append(field_id)
    branches = []
    for kinds, field_ids in groups.items():
        schema = deepcopy(_FIELD)
        schema["properties"]["field_id"]["enum"] = field_ids
        schema["properties"]["value_kind"]["enum"] = list(kinds)
        branches.append(schema)
    return _array(branches[0] if len(branches) == 1 else {"anyOf": branches})


def _node(owner_kind, **children):
    return _object_schema(
        {
            "label": STRING,
            "source_identifier": NULLABLE_STRING,
            "evidence": _SUPPORT,
            "fields": _fields(owner_kind),
            **children,
        }
    )


_MATERIAL = _node("material")
_COMPONENT = _node("component", material=_MATERIAL)
_FORMULATION = _node("formulation", components=_array(_COMPONENT))
_METHOD = _node("method_definition")
_INTERPRETATION = _node("claim")
_TEST = _node(
    "reported_test",
    method=_nullable(_METHOD),
    method_ref=_nullable(_object_schema({"method_path": STRING, "evidence": _SUPPORT})),
    conditions=_fields("reported_test", conditions=True),
    observations=_array(_node("observation")),
)
_SAMPLE = _node(
    "sample",
    formulation=_nullable(_FORMULATION),
    preparation_steps=_array(_node("process_definition")),
    tests=_array(_TEST),
    interpretations=_array(_INTERPRETATION),
)
_STUDY = _node(
    "content_unit",
    kind={"type": "string", "enum": ["study", "example", "comparison"]},
    samples=_array(_SAMPLE),
    interpretations=_array(_INTERPRETATION),
    inherits_from=_array(_object_schema({"study_path": STRING, "evidence": _SUPPORT})),
)
DIRECT_SCHEMA = _object_schema(
    {
        "source": _nullable(_node("source")),
        "shared_methods": _array(_METHOD),
        "studies": _array(_STUDY),
        "patent_claims": _array(_node("patent_claim", components=_array(_COMPONENT))),
        "issues": _array(
            _object_schema(
                {
                    "description": STRING,
                    "object_paths": _array(STRING),
                    "severity": {"type": "string", "enum": ["warning", "error"]},
                    "category": {
                        "type": "string",
                        "enum": [
                            "source_not_reported",
                            "reading_failed",
                            "extraction_uncertain",
                            "source_conflict",
                            "unsupported_layout",
                        ],
                    },
                    "evidence": _ISSUE_SUPPORT,
                }
            )
        ),
        "unreported_fields": _array(
            _object_schema(
                {
                    "object_path": STRING,
                    "field_id": {"type": "string", "enum": sorted(contract.FIELDS)},
                    "reason": STRING,
                    "evidence": _SUPPORT,
                }
            )
        ),
    }
)


def _require_nonempty_strings(schema):
    """Expose the local validator's string rule to structured generation too."""
    kinds = schema.get("type", [])
    if kinds == "string" or isinstance(kinds, list) and "string" in kinds:
        schema.update(minLength=1, pattern=r"\S")
    for child in schema.get("properties", {}).values():
        _require_nonempty_strings(child)
    if "items" in schema:
        _require_nonempty_strings(schema["items"])
    for child in schema.get("anyOf", []):
        _require_nonempty_strings(child)


_require_nonempty_strings(DIRECT_SCHEMA)

DIRECT_INSTRUCTIONS = load_prompt("document-direct-v1") + VALUE_CONTRACT_INSTRUCTIONS


def resolve_direct_evidence(payload, blocks):
    """Read explicitly referenced source blocks; never guess or repair a locator."""
    validate_direct(payload)
    registry = {b["block_id"]: b["text"] for b in blocks}
    result = deepcopy(payload)

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if set(value) == {"block_id", "quote"} and value["quote"] is None:
                text = registry.get(value["block_id"])
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Referenced evidence block is missing or unreadable: " + value["block_id"])
                value["quote"] = text
            else:
                for child in value.values():
                    visit(child)

    visit(result)
    return result


def _validate(value, schema, path="$"):
    """Validate this schema's small JSON subset without adding a dependency."""
    if "anyOf" in schema:
        for alternative in schema["anyOf"]:
            try:
                _validate(value, alternative, path)
                return
            except ValueError:
                pass
        raise ValueError(f"{path}: does not match any permitted shape")
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": type(value) is bool,
        "null": value is None,
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
    }
    if kinds and not any(matches.get(kind, False) for kind in kinds):
        raise ValueError(f"{path}: invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unregistered or disallowed value")
    if isinstance(value, dict):
        properties = schema["properties"]
        if set(value) != set(properties):
            raise ValueError(f"{path}: missing or unknown keys")
        for key, child in properties.items():
            _validate(value[key], child, f"{path}/{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{path}: evidence is required")
        for i, child in enumerate(value):
            _validate(child, schema["items"], f"{path}/{i}")
    elif isinstance(value, str) and not value.strip():
        raise ValueError(f"{path}: empty text is not evidence or a value; use null when allowed")


# Node locations alone determine kinds and structural ownership.
_CHILDREN = {
    "source": (),
    "material": (),
    "component": (("material", "material", False),),
    "formulation": (("components", "component", True),),
    "method_definition": (),
    "claim": (),
    "reported_test": (("method", "method_definition", False), ("observations", "observation", True)),
    "observation": (),
    "process_definition": (),
    "sample": (
        ("formulation", "formulation", False),
        ("preparation_steps", "process_definition", True),
        ("tests", "reported_test", True),
        ("interpretations", "claim", True),
    ),
    "content_unit": (("samples", "sample", True), ("interpretations", "claim", True)),
    "patent_claim": (("components", "component", True),),
}


def _nodes(payload):
    def walk(node, kind, path, parent=None, claimed=False):
        if node is None:
            return
        claimed = claimed or kind in ("patent_claim", "claim")
        yield path, kind, node, parent, claimed
        for name, child_kind, repeated in _CHILDREN[kind]:
            children = enumerate(node[name]) if repeated else [(None, node[name])]
            for index, child in children:
                child_path = f"{path}/{name}" + (f"/{index}" if index is not None else "")
                yield from walk(child, child_kind, child_path, path, claimed)

    yield from walk(payload["source"], "source", "/source")
    for name, kind in (
        ("shared_methods", "method_definition"),
        ("studies", "content_unit"),
        ("patent_claims", "patent_claim"),
    ):
        for index, node in enumerate(payload[name]):
            yield from walk(node, kind, f"/{name}/{index}")


def _node_fields(path, kind, node):
    for i, field in enumerate(node["fields"]):
        yield f"{path}/fields/{i}", field
    if kind == "reported_test":
        for i, field in enumerate(node["conditions"]):
            yield f"{path}/conditions/{i}", field


def validate_direct(payload) -> None:
    """Reject malformed structures, invented fields and illegal field ownership."""
    _validate(payload, DIRECT_SCHEMA)
    for path, kind, node, _, claimed in _nodes(payload):
        for field_path, field in _node_fields(path, kind, node):
            definition = contract.FIELDS[field["field_id"]]
            if kind not in definition["owner_kinds"] or field["value_kind"] not in definition["value_kinds"]:
                raise ValueError(f"{field_path}: field owner or value kind is not registered")
            if "/conditions/" in field_path and field["field_id"] != "condition.parameter":
                raise ValueError(f"{field_path}: conditions require condition.parameter")
            if claimed and field["statement_kind"] == "source_reported_measurement":
                raise ValueError(f"{field_path}: claims and interpretations cannot be measurements")
            qualifiers = [q["key"] for q in field["qualifiers"]]
            if len(qualifiers) != len(set(qualifiers)):
                raise ValueError(f"{field_path}: duplicate qualifier")


def convert_direct(payload, *, blocks=None) -> dict:
    """Compile nested records; unresolved/cyclic references become blocking issues.

    When source IR is supplied, resolve cell requests before deciding whether a
    fact is missing or inherited. No provenance marker is model-controlled.
    """
    validate_direct(payload)
    result = {"objects": [], "facts": [], "relations": [], "issues": [], "unreported_fields": []}
    nodes = {path: (kind, node, parent) for path, kind, node, parent, _ in _nodes(payload)}
    pending_fields = {}
    key = lambda path: "direct:" + path

    def issue(description, paths=(), evidence=(), category="extraction_uncertain"):
        result["issues"].append(
            {
                "description": description,
                "object_keys": [key(p) for p in paths if p in nodes],
                "severity": "error",
                "category": category,
                "evidence": deepcopy(list(evidence)),
                "origin": "direct_conversion",
            }
        )

    def edge(left, predicate, right, evidence, basis="source_explicit"):
        result["relations"].append(
            {
                "from_key": key(left),
                "predicate": predicate,
                "to_key": key(right),
                "applicability": "as_reported",
                "basis_kind": basis,
                "evidence": deepcopy(evidence),
            }
        )

    for path, (kind, node, parent) in nodes.items():
        result["objects"].append(
            {
                "key": key(path),
                "kind": kind,
                "label": node["label"],
                "scope": key(path),
                "source_identifier": node["source_identifier"],
                "owner_key": key(parent) if kind == "component" else None,
                "material_key": key(path + "/material") if kind == "component" else None,
                "identity_evidence": deepcopy(node["evidence"]),
                "evidence": deepcopy(node["evidence"]),
            }
        )
        for field_path, field in _node_fields(path, kind, node):
            fact = {k: deepcopy(v) for k, v in field.items() if k != "input_field_paths"}
            fact.update(
                key=key(field_path), object_key=key(path), input_fact_keys=[key(p) for p in field["input_field_paths"]]
            )
            pending_fields[field_path] = (fact, path)
        if parent is not None:
            parent_kind = nodes[parent][0]
            predicate = {
                ("content_unit", "sample"): "contains_sample",
                ("sample", "formulation"): "has_formulation",
                ("sample", "process_definition"): "process_applicability",
                ("sample", "reported_test"): "tested_by",
                ("reported_test", "method_definition"): "uses_method",
                ("reported_test", "observation"): "produces_observation",
                ("formulation", "component"): "contains_component",
                ("patent_claim", "component"): "contains_component",
                ("component", "material"): "refers_to_material",
            }.get((parent_kind, kind))
            if predicate:
                edge(parent, predicate, path, node["evidence"])
            elif kind == "claim":
                edge(path, "about_sample" if parent_kind == "sample" else "about_study", parent, node["evidence"])
            if kind == "observation":
                edge(path, "observes_subject", nodes[parent][2], node["evidence"])
        elif "/source" in nodes and kind != "source":
            predicate = {
                "content_unit": "contains_study",
                "patent_claim": "contains_claim",
                "method_definition": "describes_test",
            }[kind]
            edge("/source", predicate, path, node["evidence"])

    study_links = {}
    bad_studies = set()
    for path, (kind, node, _) in nodes.items():
        if kind != "content_unit":
            continue
        study_links[path] = [r["study_path"] for r in node["inherits_from"]]
        for ref in node["inherits_from"]:
            if ref["study_path"] not in nodes or nodes[ref["study_path"]][0] != "content_unit":
                bad_studies.add(path)
                issue("Unresolved study inheritance reference", [path], ref["evidence"])

    def reaches(start, target, seen=None):
        seen = set() if seen is None else seen
        if start == target:
            return True
        if start in seen:
            return False
        seen.add(start)
        return any(reaches(child, target, seen) for child in study_links.get(start, []))

    for path, refs in study_links.items():
        if any(reaches(ref, path) for ref in refs):
            bad_studies.add(path)
            issue("Cyclic study inheritance is not expanded", [path], nodes[path][1]["evidence"])
    # Descendants of a broken inheritance scope must not expand values either.
    while True:
        added = {path for path, refs in study_links.items() if set(refs) & bad_studies} - bad_studies
        if not added:
            break
        bad_studies.update(added)
    for path in study_links:
        if path not in bad_studies:
            for ref in nodes[path][1]["inherits_from"]:
                edge(path, "inherits_from", ref["study_path"], ref["evidence"], "explicit_inheritance")

    for path, (kind, node, _) in nodes.items():
        if kind != "reported_test" or node["method_ref"] is None:
            continue
        ref = node["method_ref"]
        target = ref["method_path"]
        if node["method"] is not None:
            issue("A test cannot declare both inline and shared method", [path], ref["evidence"])
        elif (
            target not in nodes or not target.startswith("/shared_methods/") or nodes[target][0] != "method_definition"
        ):
            issue("Unresolved shared method reference", [path], ref["evidence"])
        else:
            edge(path, "uses_method", target, ref["evidence"])

    if blocks is not None:
        from .ingest.documents.document_quality import resolve_cell_values

        projected = resolve_cell_values({"facts": [f for f, _ in pending_fields.values()]}, blocks)
        by_key = {f["key"]: f for f in projected["facts"]}
        pending_fields = {path: (by_key[f["key"]], owner) for path, (f, owner) in pending_fields.items()}

    resolved, failed, visiting = set(), set(), set()

    def resolve_field(path):
        if path in resolved:
            return True
        if path in failed or path not in pending_fields or path in visiting:
            return False
        fact, owner = pending_fields[path]
        formation = fact["formation"]
        refs = [r.removeprefix("direct:") for r in fact["input_fact_keys"]]
        derived = formation in ("explicit_inheritance", "explicit_override")
        reason = None
        if fact["raw_value"] is None:
            reason = "Missing raw value is not an assertion; preserve the uncertainty"
        elif derived and (not refs or any(owner == p or owner.startswith(p + "/") for p in bad_studies)):
            reason = "Inheritance lacks resolved original inputs or has an invalid study scope"
        elif not derived and refs:
            reason = "Only explicit inheritance or override may reference input fields"
        else:
            visiting.add(path)
            if any(not resolve_field(ref) for ref in refs):
                reason = "Unresolved or cyclic inheritance input; dependent field withheld"
            elif derived:
                inputs = [pending_fields[ref][0] for ref in refs]
                comparable = (
                    ("field_id", "statement_kind")
                    if formation == "explicit_override"
                    else ("field_id", "raw_unit", "raw_basis", "qualifiers", "statement_kind")
                )
                if any(any(fact[k] != source[k] for k in comparable) for source in inputs):
                    reason = "Inheritance input conflicts in field, unit, basis, qualifiers or statement kind"
                elif formation == "explicit_inheritance" and any(fact["raw_value"] != f["raw_value"] for f in inputs):
                    reason = "Inherited value conflicts with original input; explicit override required"
            visiting.remove(path)
        if reason:
            failed.add(path)
            issue(
                reason, [owner], fact["evidence"], "source_conflict" if "conflict" in reason else "extraction_uncertain"
            )
            return False
        resolved.add(path)
        return True

    for path in pending_fields:
        resolve_field(path)
    result["facts"] = [fact for path, (fact, _) in pending_fields.items() if path in resolved]
    conflicts = {}
    for fact in result["facts"]:
        if fact["field_id"] not in ("composition.amount", "condition.parameter", "observation.value"):
            continue
        signature = core.fingerprint(
            [fact[k] for k in ("object_key", "field_id", "raw_unit", "raw_basis", "qualifiers", "statement_kind")]
        )
        conflicts.setdefault(signature, []).append(fact)
    for facts in conflicts.values():
        if len({core.fingerprint(f["raw_value"]) for f in facts}) > 1:
            owner = facts[0]["object_key"].removeprefix("direct:")
            evidence = [e for f in facts for e in f["evidence"]]
            issue(
                "Conflicting directly reported values retained for source review", [owner], evidence, "source_conflict"
            )
    for item in payload["issues"]:
        unknown = [p for p in item["object_paths"] if p not in nodes]
        if unknown:
            issue("Issue contains unresolved object references", evidence=item["evidence"])
        result["issues"].append(
            {
                **{k: deepcopy(v) for k, v in item.items() if k != "object_paths"},
                "object_keys": [key(p) for p in item["object_paths"]],
                "origin": "source_reported",
            }
        )
    for item in payload["unreported_fields"]:
        target = item["object_path"]
        if target not in nodes or nodes[target][0] not in contract.FIELDS[item["field_id"]]["owner_kinds"]:
            issue("Unreported field has unresolved subject or illegal owner", [target], item["evidence"])
        else:
            result["unreported_fields"].append(
                {**{k: deepcopy(v) for k, v in item.items() if k != "object_path"}, "object_key": key(target)}
            )
    return result
