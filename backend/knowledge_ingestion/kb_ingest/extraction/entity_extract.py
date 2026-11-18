"""One main request per window, followed only by budgeted, targeted omissions."""

from functools import partial

from kb_ingest.core.contract_entity import COMPOSITION_SECTION, normalize_name
from kb_ingest.extraction.entity_windows import segment_index, window_source

from .entity_contract import (
    ATTRIBUTE_INSTRUCTIONS,
    ATTRIBUTE_SCHEMA,
    SOURCE_FORMAT,
    WINDOW_EXTRACTION_INSTRUCTIONS,
    WINDOW_EXTRACTION_SCHEMA,
    validate_instance,
)

KNOWN_LIMIT = 150
WINDOW_REQUESTS = 1


def known_catalogue(graphs):
    """Entities already listed by earlier windows, so a later window reuses the same names."""
    seen, result = set(), []
    for graph in graphs:
        for entity in graph.get("entities", []):
            if entity.get("known"):
                continue
            key = (entity["type"], entity["name"])
            if key not in seen:
                seen.add(key)
                result.append(
                    {
                        "type": entity["type"],
                        "name": entity["name"],
                        "aliases": list(entity.get("aliases") or []),
                        "seen_at": [mention["block_id"] for mention in entity.get("mentions") or []],
                    }
                )
            else:
                result[[(item["type"], item["name"]) for item in result].index(key)]["seen_at"].extend(
                    mention["block_id"] for mention in entity.get("mentions") or []
                )
    return result[-KNOWN_LIMIT:]


def known_before(catalogue, window, order):
    """Known entities first mentioned before this window. A window only continues what came before it: a table's
    samples, read first but printed after the claims, are not targets for the claims window (whose composition
    ranges would otherwise be attached to them). `order` is the document's block ids in reading order."""
    position = {block_id: index for index, block_id in enumerate(order)}
    start = min((position[block["block_id"]] for block in window["blocks"] if block["block_id"] in position), default=0)
    return [
        {key: value for key, value in item.items() if key != "seen_at"}
        for item in catalogue
        if any(position.get(block_id, len(position)) < start for block_id in item.get("seen_at") or [])
    ]


FORMULATIONS_PER_CALL = 6
GAP_INSTRUCTIONS = (
    "\n"
    "target_entities 中的配方还缺「原料」，同一窗口的其他配方已经有了。"
    "在原文段落或表格中找到它们各自的组分和用量，为每个目标配方写「原料」：组成表的每一行写一段，cite "
    "该行的行名格和数值格；每个样品只写自己的数值。原文没有它的组成时，这个配方留空。"
)
GROUP_INSTRUCTIONS = """
本次只为 target_entities 中的配方写属性。为每个目标配方写全它的组成、制备、测试结果和解释。"""


def _quote(cited, index):
    """(first block id, quotation, covered block ids) for cited segment ids, in source order."""
    # An id copied with its brackets from the source text (<b12.3>) is the same id; a prose block's own id where
    # the block is split into segments (b12 for b12.1, b12.2) cites the whole block. A table block's id never
    # stands for its cells. The quotation is only ever the cited segments, so the checks see no extra text.
    cited = {str(segment_id).strip().strip("<>").strip() for segment_id in cited if segment_id}
    rows = {index[segment_id] for segment_id in cited if segment_id in index}
    blocks = cited - set(index)
    if blocks:
        rows |= {row for segment_id, row in index.items() if row[0] in blocks and segment_id != row[0] and not row[5]}
    rows = sorted(rows, key=lambda row: (row[1], row[2]))
    if not rows:
        return None, "", []
    pieces, previous = [], None
    for block_id, position, number, piece, cell, _ in rows:
        # Adjacent segments read on as one passage; table cells and separate pieces keep a cell separator.
        joined = (
            previous is not None
            and not cell
            and not previous[2]
            and (position, number) in {(previous[0], previous[1] + 1), (previous[0] + 1, 0)}
        )
        pieces.append(("" if joined or previous is None else " | ") + piece)
        previous = (position, number, cell)
    covered = list(dict.fromkeys(row[0] for row in rows))
    return rows[0][0], "".join(pieces), covered


def cite_to_quotes(result, index):
    """The model cites segment ids; fill in each mention's and attribute's block and quotation from the source.
    Unknown ids are ignored; an attribute with none left has no quotation and is dropped by the check."""
    entities = []
    for entity in result.get("entities", []):
        mentions = []
        for mention in entity.get("mentions") or []:
            block_id, quote, _ = _quote([mention.get("cite")], index)
            if block_id:
                mentions.append({"block_id": block_id, "quote": quote})
        entities.append({**{key: value for key, value in entity.items() if key != "mentions"}, "mentions": mentions})
    attributes = []
    for row in result.get("attributes", []):
        block_id, quote, covered = _quote(row.get("cite") or [], index)
        attributes.append(
            {
                **{key: value for key, value in row.items() if key != "cite"},
                "block_id": block_id,
                "quote": quote,
                "covered": covered,
            }
        )
    return {**result, "entities": entities, "attributes": attributes, "related": list(result.get("related") or [])}


def formulation_groups(entities, size=FORMULATIONS_PER_CALL):
    """Formulations of one window in groups, so a dense window (a table of many samples) is not cut short."""
    ids = [entity["local_id"] for entity in entities if entity["type"] == "formulation"]
    return [ids[index : index + size] for index in range(0, len(ids), size)] or [[]]


def extract_window(window, call, known=None, extra_calls=None, *, logical_binding=None):
    """Extract the full graph once; extra_calls limits every optional follow-up together."""
    if logical_binding is not None:
        call = partial(call, logical_binding=logical_binding)
    source = window_source(window["blocks"])
    index = segment_index(window["blocks"])
    prior = [
        {
            "local_id": "k" + str(index + 1),
            "type": entity["type"],
            "name": entity["name"],
            "aliases": list(entity.get("aliases") or []),
            "mentions": [],
            "known": True,
        }
        for index, entity in enumerate(item for item in (known or []) if item["type"] == "formulation")
    ]
    result = call(
        "entities-attributes-related",
        WINDOW_EXTRACTION_INSTRUCTIONS,
        {"source": source, "known_entities": list(known or []), "known_formulations": prior},
        WINDOW_EXTRACTION_SCHEMA,
    )
    validate_instance(result, WINDOW_EXTRACTION_SCHEMA)
    result = cite_to_quotes(result, index)
    listed = list(result["entities"])
    ids = [entity["local_id"] for entity in listed]
    if len(ids) != len(set(ids)) or set(ids) & {entity["local_id"] for entity in prior}:
        raise ValueError("entity IDs must be unique and cannot use reserved known formulation IDs")
    # A sample mentioned again has a current quote; remap references to its prior
    # ID onto that new row instead of duplicating an owner or losing its facts.
    names = {(entity["type"], normalize_name(entity["name"])): entity["local_id"] for entity in listed}
    remap = {
        entity["local_id"]: names[(entity["type"], normalize_name(entity["name"]))]
        for entity in prior
        if (entity["type"], normalize_name(entity["name"])) in names
    }
    everything = listed + [entity for entity in prior if entity["local_id"] not in remap]
    # A follow-up request needs the entities' ids and names, not their mentions (already cited in the source).
    named = [
        {key: entity[key] for key in ("local_id", "type", "name", "aliases", "known") if entity.get(key) is not None}
        for entity in everything
    ]
    collected = [
        {
            **row,
            "local_id": remap.get(row["local_id"], row["local_id"]),
            "refs": [remap.get(ref, ref) for ref in row["refs"]],
        }
        for row in result["attributes"]
    ]
    links = [
        {**row, "a": remap.get(row["a"], row["a"]), "b": remap.get(row["b"], row["b"])} for row in result["related"]
    ]
    # Dense windows no longer re-extract every formulation automatically. Only
    # newly listed formulations with no attributes get a bounded second chance.
    described = {row["local_id"] for row in collected}
    samples = [entity for entity in listed if entity["type"] == "formulation"]
    missing = [entity for entity in samples if entity["local_id"] not in described]
    groups = formulation_groups(missing) if len(samples) > FORMULATIONS_PER_CALL and missing else []
    if extra_calls is not None:
        limit = max(0, extra_calls)
        if 0 < limit < len(groups):
            groups = groups[: limit - 1] + [[item for group in groups[limit - 1 :] for item in group]]
        elif limit == 0:
            groups = []
    for targets in groups:
        result = call(
            "attributes-related",
            SOURCE_FORMAT + "\n" + ATTRIBUTE_INSTRUCTIONS + GROUP_INSTRUCTIONS,
            {"source": source, "entities": named, "target_entities": targets},
            ATTRIBUTE_SCHEMA,
        )
        validate_instance(result, ATTRIBUTE_SCHEMA)
        result = cite_to_quotes(result, index)
        collected.extend(row for row in result["attributes"] if row["local_id"] in targets)
        links.extend(result["related"])
    spare = None if extra_calls is None else max(0, extra_calls - len(groups))
    return fill_gaps(source, everything, collected, links, call, spare, index, named)


def missing_composition(entities, attributes):
    """Formulations of this window without 原料 while at least two others have it (a table read only in part)."""
    formulations = [
        entity["local_id"] for entity in entities if entity["type"] == "formulation" and not entity.get("known")
    ]
    with_data = {row["local_id"] for row in attributes if row.get("section") == COMPOSITION_SECTION}
    present = [local_id for local_id in formulations if local_id in with_data]
    return [local_id for local_id in formulations if local_id not in with_data] if len(present) >= 2 else []


def fill_gaps(source, everything, collected, links, call, extra_calls, index=None, named=None):
    """One targeted call for formulations whose composition the first pass left out, when the budget allows."""
    gaps = missing_composition(everything, collected)
    if gaps and (extra_calls is None or extra_calls > 0):
        result = call(
            "attributes-gaps",
            SOURCE_FORMAT + "\n" + ATTRIBUTE_INSTRUCTIONS + GAP_INSTRUCTIONS,
            {"source": source, "entities": named or everything, "target_entities": gaps},
            ATTRIBUTE_SCHEMA,
        )
        validate_instance(result, ATTRIBUTE_SCHEMA)
        result = cite_to_quotes(result, index or {})
        collected = collected + [row for row in result["attributes"] if row["local_id"] in gaps]
        links = links + result["related"]
    return {"entities": everything, "attributes": collected, "related": links}
