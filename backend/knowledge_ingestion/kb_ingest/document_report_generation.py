"""Finite model wire protocol compiled into the existing source-bound template.

This compiler never repairs a rejected provider response. Historical protocols
remain templates; only newly generated v6 responses are typed segments.
"""

from .core import fingerprint
from .document_program_contract import arr, obj

GENERATION_VERSION = "report-segments-v1"
SEGMENT_PROMPT_VERSION = "study-report-v6"
MAX_SEGMENTS = 80
MAX_TEXT_SEGMENT = 1000


def structured_report_schema(snapshot):
    from .document_reports import KINDS, SECTION_IDS, _label_placeholders

    enum = lambda values: {"type": "string", "enum": list(values)}
    branches = [obj(type=enum(["text"]), text={"type": "string", "minLength": 1, "maxLength": MAX_TEXT_SEGMENT})]
    facts = snapshot["facts"]
    if facts:
        branches.append(obj(type=enum(["fact"]), fact_id=enum(f["id"] for f in facts)))
    # A label field has its own eligible-ID set; a material-only field cannot
    # accidentally authorize a name, value, condition or arbitrary source path.
    for field in ("name", "material"):
        ids = [f["id"] for f in facts if field in _label_placeholders(f)]
        if ids:
            branches.append(obj(type=enum(["label"]), fact_id=enum(ids), field=enum([field])))
    refs = [r["id"] for key in ("facts", "citations", "gaps") for r in snapshot[key]]
    ref_array = {**arr(enum(refs) if refs else {"type": "string"}), "maxItems": 60 if refs else 0}
    paragraph = obj(
        segments={**arr({"anyOf": branches}), "minItems": 1, "maxItems": MAX_SEGMENTS}, kind=enum(KINDS), refs=ref_array
    )
    return obj(
        sections={
            **arr(obj(id=enum(SECTION_IDS), paragraphs={**arr(paragraph), "minItems": 1, "maxItems": 12})),
            "minItems": 6,
            "maxItems": 6,
        }
    )


def generation_audit(raw, snapshot):
    if snapshot.get("version") != SEGMENT_PROMPT_VERSION:
        return None
    return {"protocol": GENERATION_VERSION, "response_output_sha256": fingerprint(raw)}


def compile_report_output(raw, snapshot):
    from .document_reports import KINDS, SECTION_IDS, _label_placeholders, _report_version, validate_report_output

    if _report_version(snapshot) != SEGMENT_PROMPT_VERSION:
        return validate_report_output(raw, snapshot)
    if type(raw) is not dict or set(raw) != {"sections"} or type(raw["sections"]) is not list:
        raise ValueError("invalid structured report object")
    if [s.get("id") if type(s) is dict else None for s in raw["sections"]] != list(SECTION_IDS):
        raise ValueError("report must contain exactly the ordered six sections")
    facts = {f["id"]: f for f in snapshot["facts"]}
    allowed = set(facts) | {r["id"] for key in ("citations", "gaps") for r in snapshot[key]}
    result = {"sections": []}
    for section in raw["sections"]:
        if (
            set(section) != {"id", "paragraphs"}
            or type(section["paragraphs"]) is not list
            or not 1 <= len(section["paragraphs"]) <= 12
        ):
            raise ValueError("invalid structured report section")
        paragraphs = []
        for paragraph in section["paragraphs"]:
            if (
                type(paragraph) is not dict
                or set(paragraph) != {"segments", "kind", "refs"}
                or paragraph["kind"] not in KINDS
            ):
                raise ValueError("invalid structured report paragraph")
            refs = paragraph["refs"]
            segments = paragraph["segments"]
            if type(refs) is not list or len(refs) > 60 or any(type(r) is not str or r not in allowed for r in refs):
                raise ValueError("report reference outside input whitelist")
            if type(segments) is not list or not 1 <= len(segments) <= MAX_SEGMENTS:
                raise ValueError("invalid report segments")
            compiled = []
            refs = list(dict.fromkeys(refs))
            for segment in segments:
                if type(segment) is not dict:
                    raise ValueError("invalid report segment")
                kind = segment.get("type")
                if kind == "text":
                    text = segment.get("text")
                    if (
                        set(segment) != {"type", "text"}
                        or type(text) is not str
                        or not text.strip()
                        or len(text) > MAX_TEXT_SEGMENT
                        or "{" in text
                        or "}" in text
                    ):
                        raise ValueError("invalid report text segment")
                    compiled.append(text)
                elif kind in ("fact", "label"):
                    expected = {"type", "fact_id"} | ({"field"} if kind == "label" else set())
                    fid = segment.get("fact_id")
                    if set(segment) != expected or type(fid) is not str or fid not in facts:
                        raise ValueError("invalid report fact segment")
                    if kind == "fact":
                        token = "{{fact:" + fid + "}}"
                    else:
                        field = segment["field"]
                        labels = _label_placeholders(facts[fid])
                        if type(field) is not str or field not in labels:
                            raise ValueError("invalid report label segment")
                        token = labels[field]
                    compiled.append(token)
                    if fid not in refs:
                        refs.append(fid)
                else:
                    raise ValueError("unknown report segment type")
            paragraphs.append({"text": "".join(compiled), "kind": paragraph["kind"], "refs": refs})
        result["sections"].append({"id": section["id"], "paragraphs": paragraphs})
    # Validate after concatenation too: adjacent prose must not split a forbidden
    # quantity across segments. Source insertion remains the existing one-pass renderer.
    return validate_report_output(result, snapshot)
