"""Scientific output only; identities, formal fields and checks belong to Python."""

from .llm.prompts import load_prompt

CONTRACT_VERSION = "program-document-v5"


def obj(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def arr(item):
    return {"type": "array", "items": item}


S = {"type": "string"}
N = {"type": ["string", "null"]}
REFS = arr(S)
FACT = obj(
    ref=N,
    name=S,
    value_representation={"type": "string", "enum": ["reported_literal", "paraphrase"]},
    value={"type": ["string", "number", "null"]},
    unit=N,
    basis=N,
    conditions=arr(S),
    evidence=REFS,
    fact_type={"type": "string", "enum": ["measurement", "calculation", "interpretation", "claim", "description"]},
    attribution={"type": "string", "enum": ["authors", "cited_work", "product"]},
    statement_status=N,
)
TEST = obj(ref=S, label=S, evidence=REFS, method_ref=N, conditions=arr(FACT), results=arr(FACT))
SAMPLE = obj(
    ref=S,
    label=S,
    evidence=REFS,
    composition=arr(obj(**FACT["properties"], material=S)),
    preparation=arr(FACT),
    tests=arr(TEST),
)
PROGRAM_SCHEMA = obj(
    source=obj(title=N, evidence=REFS),
    evidence=arr(obj(ref=S, block_id=N, quote=S, page={"type": ["integer", "null"]}, asset_ref=N)),
    studies=arr(
        obj(
            ref=S,
            label=S,
            research_question=N,
            source_anchors=REFS,
            continuation_of=N,
            evidence=REFS,
            samples=arr(SAMPLE),
        )
    ),
    shared_methods=arr(obj(ref=S, label=S, evidence=REFS, applies_to=REFS, details=arr(FACT))),
    statements=arr(obj(**FACT["properties"], about=REFS)),
    issues=arr(
        obj(
            description=S,
            refs=REFS,
            evidence=REFS,
            scope={"type": "string", "enum": ["document", "sample", "field"]},
            target_refs=REFS,
        )
    ),
)
PROGRAM_INSTRUCTIONS = load_prompt("document-program-v1")
