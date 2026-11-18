"""Who a formulation is, beyond its code: a descriptive name, why it was made, and how it differs from its siblings.

"实施例1" or "3#" only means something inside one document. One call per paper proposes, for every formulation, a
short name built from its own composition, the purpose sentence that covers it, and what distinguishes it from the
other samples of the paper. The program keeps only what the paper supports: numbers must come from the samples'
attributes, the name must use the sample's own words, names are unique in the paper, and the purpose quote must be
in the source. The code stays as the identity (it fixes the formulation id), as an alias, and as a 「文中名称」
attribute quoting where the paper names it.
"""

import re
import unicodedata

from kb_ingest.core.contract_entity import normalize_name, numbers, present_aliases
from kb_ingest.llm import ModelCallError

from .entity_check import BlockText
from .entity_contract import IDENTITY_INSTRUCTIONS, IDENTITY_SCHEMA, validate_instance

CODE_SECTION = "文中名称"
PURPOSE_SECTION = "目的"
DIFFERENCE_SECTION = "区别"
NAMING_SECTIONS = ("原料", "制备过程")
PURPOSE_CUES = re.compile(r"(目的|为了|旨在|技术问题|以考察|为考察|考察|研究.{0,10}影响|用于说明|用以|以期)")
CANDIDATE_LIMIT = 16
CANDIDATE_CHARS = 300


def purpose_candidates(blocks):
    """Sentences that may state why samples were made (本发明的目的…, 为研究…的影响…), with their block ids."""
    rows = []
    for block in blocks:
        text = " ".join(str(block.get("text") or "").split())
        match = PURPOSE_CUES.search(text)
        if match:
            start = max(0, match.start() - 80)
            rows.append(
                {
                    "block_id": block["block_id"],
                    "page": block.get("page"),
                    "text": text[start : start + CANDIDATE_CHARS],
                }
            )
        if len(rows) >= CANDIDATE_LIMIT:
            break
    return rows


def _flat(text):
    return "".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def _own_words(name, corpus):
    """The name is made of the sample's own words: a Latin or formula token of it, or at least two of its
    two-character runs, occur in the sample's attributes (次磷酸镁/MCA 协效阻燃 ABS -> 次磷, 磷酸, MCA, ABS…)."""
    flat = _flat(corpus)
    target = _flat(name)
    if any(token in flat for token in re.findall(r"[a-z][a-z0-9@\-]+|[0-9]+[a-z@][a-z0-9@\-]*", target)):
        return True
    runs = {
        target[index : index + 2]
        for index in range(len(target) - 1)
        if re.fullmatch(r"[\u3400-\u9fff]{2}", target[index : index + 2])
    }
    return sum(1 for run in runs if run in flat) >= 2


NAME_CHARS = 30
VARIANT_CHARS = 16
_CODE_LIKE = re.compile(
    r"(实施例|对比例|对照例|比较例|样品|试样|example|sample|权利要求)\s*[0-9a-z]|(?<![a-z])(cn|us|ep|wo|jp|kr)\s*\d{5,}",
    re.I,
)


_BARE_CODE = re.compile(r"^(?:[0-9a-z]{1,4}#?|#?[0-9]+|[a-z]?[0-9]+[a-z]?)$", re.I)


def looks_like_code(code):
    """实施例1, 对比例2, 3#, S1, A: a label that only means something inside its paper. 「N-GO合成」 is not one."""
    text = unicodedata.normalize("NFKC", str(code or "")).strip()
    return bool(_CODE_LIKE.search(text) or _BARE_CODE.match(normalize_name(text)))


def name_problem(name, code, making, names):
    """Why a proposed name cannot be used, or None. A name says what the system is: no amounts, no sample code,
    no document number, at most NAME_CHARS characters, made of the sample's own words, and abbreviations only as
    the paper writes them."""
    if not name:
        return "no descriptive name"
    if normalize_name(name) == normalize_name(code):
        return "no descriptive name" if looks_like_code(code) else None
    if len(unicodedata.normalize("NFKC", name)) > NAME_CHARS:
        return "name too long"
    if (looks_like_code(code) and normalize_name(code) in normalize_name(name)) or _CODE_LIKE.search(
        unicodedata.normalize("NFKC", name)
    ):
        return "name contains a sample code or document number"
    vocabulary = _flat(making + " " + code + " " + " ".join(names))
    for token in re.findall(r"[a-z][a-z0-9@\-]*", _flat(name)):
        if len(token) >= 2 and token not in vocabulary:
            return "name uses an abbreviation not in the paper"
    if numbers(name, names):
        return "name contains an amount"
    if not _own_words(name, making + " " + code):
        return "name does not use this sample's words"
    return None


def fallback_name(entity, rows, entities):
    """From the sample's composition: its first two referenced materials, joined as the paper names them."""
    seen = []
    for row in rows:
        if row["section"] != "原料":
            continue
        for ref in row.get("refs") or []:
            other = entities.get(ref)
            if other and other["type"] == "material" and other["name"] not in seen:
                seen.append(other["name"])
    return "/".join(seen[:2]) + " 体系" if seen else None


_SEPARATOR = re.compile(r"[、，,；;。\n]")
_AMOUNT = re.compile(
    r"(?<![A-Za-z0-9(（.])(\d+(?:\.\d+)?(?:\s*[:：]\s*\d+(?:\.\d+)?)*)\s*(wt\s*%|％|%|mg|g|份|mL|ml|℃|°C|h|min)?"
)


def _values(rows):
    """Extract material and amount pairs; digits inside a formula are not amounts."""
    found = []
    for row in rows:
        if row["section"] != "原料":
            continue
        for segment in _SEPARATOR.split(unicodedata.normalize("NFKC", row["text"])):
            # A number inside brackets belongs to a name or grade, not an amount.
            # Right after a closing bracket, only a number with a unit is an amount.
            match = next(
                (
                    found
                    for found in _AMOUNT.finditer(segment)
                    if segment[: found.start()].count("(") <= segment[: found.start()].count(")")
                    and (found.group(2) or not segment[: found.start()].endswith(")"))
                ),
                None,
            )
            if not match:
                continue
            word = segment[: match.start()].strip(" :：为（(")[-16:]
            value = re.sub(r"\s*[:：]\s*", ":", match.group(1)) + (
                " " + match.group(2).replace(" ", "") if match.group(2) else ""
            )
            pair = (word, value)
            if word and pair not in found:
                found.append(pair)
    return found


# Spaces next to Chinese characters are layout, not part of a name.
_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff）])\s+|\s+(?=[\u4e00-\u9fff（])")


def distinguishing_data(local_id, members, own):
    """The word and value this sample has and its same-named siblings do not (「La盐 0.15」); two if one is not
    enough to tell it apart. None when its composition gives nothing distinct."""
    others = [set(_values(own[key])) for key in members if key != local_id]
    mine = [pair for pair in _values(own[local_id]) if not all(pair in values for values in others)]
    for count in (1, 2):
        chosen = mine[:count]
        if chosen and all(any(pair not in values for pair in chosen) for values in others):
            label = "，".join(f"{word} {value}" for word, value in chosen)
            # Never cut inside a value: too long with its word, the bare value is used.
            return label if len(label) <= VARIANT_CHARS else "，".join(value for _, value in chosen)[:VARIANT_CHARS]
    return None


def unique_names(chosen):
    """Two formulations of one paper never share a name: a sample whose composition gives no distinct postfix
    (missing or identical data) takes its in-text code as the postfix, so the two are not merged later.
    chosen maps local_id -> [name, reason, proposal, code]."""
    groups = {}
    for local_id, (name, _reason, _row, code) in chosen.items():
        groups.setdefault(normalize_name(name), []).append(local_id)
    for members in groups.values():
        if len(members) > 1 and len({normalize_name(chosen[key][3]) for key in members}) == len(members):
            for key in members:
                chosen[key][0] = f"{chosen[key][0]}（{chosen[key][3]}）"


def identity_payload(graph, blocks):
    entities = {entity["local_id"]: entity for entity in graph["entities"]}
    rows = []
    for entity in graph["entities"]:
        if entity["type"] != "formulation":
            continue
        attributes = [
            {
                "section": row["section"],
                "text": row["text"],
                "refs": [entities[ref]["name"] for ref in row.get("refs") or [] if ref in entities],
            }
            for row in graph["attributes"]
            if row["local_id"] == entity["local_id"]
        ]
        rows.append({"local_id": entity["local_id"], "code": entity["name"], "attributes": attributes[:40]})
    return {"formulations": rows, "purpose_candidates": purpose_candidates(blocks)}


def apply_identity(graph, proposals, blocks):
    """Checked names, purposes and differences onto the paper's formulations. Returns (graph, log)."""
    text = BlockText(blocks)
    entities = {entity["local_id"]: entity for entity in graph["entities"]}
    formulations = [entity for entity in graph["entities"] if entity["type"] == "formulation"]
    own = {
        entity["local_id"]: [row for row in graph["attributes"] if row["local_id"] == entity["local_id"]]
        for entity in formulations
    }
    corpus = {key: " ".join(row["text"] + " " + row["quote"] for row in rows) for key, rows in own.items()}
    # A name says what the sample is made of and how: its words and numbers come from composition and preparation,
    # never only from test results (an oxygen index does not name a sample).
    making = {
        key: " ".join(row["text"] + " " + row["quote"] for row in rows if row["section"] in NAMING_SECTIONS)
        or corpus[key]
        for key, rows in own.items()
    }
    all_numbers = set().union(*(numbers(value) for value in corpus.values())) if corpus else set()
    names = [name for entity in graph["entities"] for name in [entity["name"], *entity.get("aliases", [])]]
    proposals = {row["local_id"]: row for row in proposals or [] if row.get("local_id") in entities}
    chosen, log = {}, []
    for entity in formulations:
        code = entity.get("code") or entity["name"]
        row = proposals.get(entity["local_id"]) or {}
        name = _CJK_SPACE.sub("", " ".join(str(row.get("name") or "").split()))
        reason = name_problem(name, code, making[entity["local_id"]], names)
        if reason is not None:
            name = fallback_name(entity, own[entity["local_id"]], entities) or code
        chosen[entity["local_id"]] = [name, reason, row, code]
    # Siblings that differ only in amounts share the name; a short postfix (the varying parameter) tells them apart.
    groups = {}
    for local_id, (name, *_rest) in chosen.items():
        groups.setdefault(normalize_name(name), []).append(local_id)
    for members in groups.values():
        if len(members) < 2:
            continue
        variants = {}
        for local_id in members:
            variant = " ".join(str(chosen[local_id][2].get("variant") or "").split())
            if not variant or len(variant) > VARIANT_CHARS or not numbers(variant, names) <= numbers(making[local_id]):
                variant = distinguishing_data(local_id, members, own)
            variants[local_id] = variant
        if len({normalize_name(value) for value in variants.values()}) < len(members):
            variants = {local_id: distinguishing_data(local_id, members, own) for local_id in members}
        for local_id in members:
            if variants[local_id]:
                chosen[local_id][0] = f"{chosen[local_id][0]}（{variants[local_id]}）"
    unique_names(chosen)
    attributes = list(graph["attributes"])
    for entity in formulations:
        name, reason, row, code = chosen[entity["local_id"]]
        mention = entity["mentions"][0]
        entity.update(
            name=name,
            code=code,
            aliases=[
                alias
                for alias in present_aliases(name, entity.get("aliases", []))
                if normalize_name(alias) != normalize_name(code)
            ],
        )
        attributes.append(
            {
                "local_id": entity["local_id"],
                "section": CODE_SECTION,
                "text": code,
                "block_id": mention["block_id"],
                "quote": mention["quote"],
                "refs": [],
            }
        )
        purpose = row.get("purpose_text")
        placed = text.place(row.get("purpose_block_id"), row.get("purpose_quote"), text.order) if purpose else None
        if placed:
            attributes.append(
                {
                    "local_id": entity["local_id"],
                    "section": PURPOSE_SECTION,
                    "text": " ".join(purpose.split()),
                    "block_id": placed[0],
                    "quote": row["purpose_quote"],
                    "block_ids": placed[1],
                    "refs": [],
                }
            )
        differs = " ".join(str(row.get("differs") or "").split())
        data = [item for item in own[entity["local_id"]] if item["section"] == "原料"] or own[entity["local_id"]]
        if differs and data and numbers(differs, names) <= all_numbers:
            anchor = data[0]
            # A reading of the composition data: located at the sample's own composition, as the proposal asks.
            attributes.append(
                {
                    "local_id": entity["local_id"],
                    "section": DIFFERENCE_SECTION,
                    "text": differs,
                    "block_id": anchor["block_id"],
                    "quote": anchor["quote"],
                    "block_ids": anchor.get("block_ids") or [anchor["block_id"]],
                    "refs": [],
                }
            )
        log.append(
            {
                "code": code,
                "name": name,
                "name_rejected": reason,
                "purpose": bool(placed),
                "differs": bool(differs and data and numbers(differs, names) <= all_numbers),
            }
        )
    return {**graph, "attributes": attributes}, log


def identity_phase(graph, blocks, call, unresolved=None):
    payload = identity_payload(graph, blocks)
    if not payload["formulations"]:
        return graph, []
    try:
        result = call("identity", IDENTITY_INSTRUCTIONS, payload, IDENTITY_SCHEMA)
        validate_instance(result, IDENTITY_SCHEMA)
        proposals = result["formulations"]
    except (ModelCallError, ValueError) as exc:
        # Without an answer every formulation keeps its code; nothing else depends on this call.
        if unresolved is None:
            raise
        unresolved.append({"phase": "identity", "reason": type(exc).__name__})
        proposals = []
    return apply_identity(graph, proposals, blocks)
