"""Merge one paper's windows onto temporary ids before knowledge-base resolution."""

from collections import Counter
from typing import TypedDict

from kb_ingest.core.conservation import require_conserved
from kb_ingest.core.contract_entity import bare_property_name, normalize_name, present_aliases

from .entity_check import entity_keys, named_in


def _location(item):
    row = {"block_id": item["block_id"], "quote": item["quote"]}
    if item.get("block_ids") and item["block_ids"] != [item["block_id"]]:
        row["block_ids"] = list(item["block_ids"])
    return row


class _RelatedRow(TypedDict):
    a: str
    b: str
    locations: list[dict[str, object]]


def collapse_related(links: list[dict[str, object]]) -> list[_RelatedRow]:
    """Public merge service: one related row per unordered entity pair, locations kept."""
    grouped: dict[tuple[str, str], _RelatedRow] = {}
    for link in links:
        left = link["a"]
        right = link["b"]
        if not isinstance(left, str) or not isinstance(right, str):
            message = "related ends must be strings"
            raise TypeError(message)
        ends = (left, right) if left <= right else (right, left)
        if ends[0] == ends[1]:
            continue
        locations: list[dict[str, object]] = []
        row = grouped.setdefault(ends, {"a": ends[0], "b": ends[1], "locations": locations})
        location = _location(link)
        if location not in row["locations"]:
            row["locations"].append(location)
    return list(grouped.values())


def _canonical(members):
    """A name given together with an abbreviation is the full name; otherwise the most frequent, then longest."""
    listed = [member for member in members if not member.get("known")] or members
    with_alias = [member["name"] for member in listed if member.get("aliases")]
    pool = with_alias or [member["name"] for member in listed]
    counts = Counter(pool)
    return max(pool, key=lambda name: (counts[name], len(name)))


UNPLACED = "carried from an earlier window, but nothing in this paper places it"
UNOWNED = "its entity was not kept in this paper"


def _members(graphs):
    members = []
    for window, graph in enumerate(graphs):
        for entity in graph.get("entities", []):
            if entity["type"] == "property":
                # Units and test modes are not part of a property's identity (they stay in the 性能 lines).
                name, alias = bare_property_name(entity["name"])
                aliases = [
                    bare_property_name(item)[0] if bare_property_name(item)[1] is None else item
                    for item in entity.get("aliases") or []
                ]
                entity = {**entity, "name": name, "aliases": [*([alias] if alias else []), *aliases]}
            members.append({**entity, "window": window})
    return members


def _groups(members):
    """Members that share a type and any normalized name or alias, as lists of member indexes in reading order."""
    parent = list(range(len(members)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    owner: dict[tuple[str, str], int] = {}
    for index, member in enumerate(members):
        for name_key in entity_keys(member):
            slot = (member["type"], name_key)
            if slot in owner:
                parent[find(index)] = find(owner[slot])
            else:
                owner[slot] = index
    groups: dict[int, list[int]] = {}
    for index in range(len(members)):
        groups.setdefault(find(index), []).append(index)
    return sorted(groups.values(), key=lambda items: items[0])


def _entity_row(group, local_id, name):
    mentions, aliases = [], []
    for member in group:
        for mention in member.get("mentions", []):
            if mention not in mentions:
                mentions.append(mention)
        aliases.extend([member["name"], *(member.get("aliases") or [])])
    row = {
        "local_id": local_id,
        "type": group[0]["type"],
        "name": name,
        "aliases": present_aliases(name, aliases),
        "mentions": mentions,
    }
    code = next((member["code"] for member in group if member.get("code")), None)
    if code:
        row["code"] = code
    return row


def _merge_entities(members, set_aside):
    """(entities, mapping from (window, local id) to the merged id). A group nothing places is set aside."""
    entities, mapping, used = [], {}, set()
    placed = 0
    for indexes in _groups(members):
        group = [members[index] for index in indexes]
        name = _canonical(group)
        local_id = group[0]["type"] + ":" + normalize_name(name)
        while local_id in used:
            local_id += "'"
        used.add(local_id)
        row = _entity_row(group, local_id, name)
        if not row["mentions"]:
            # Only carried from an earlier window that did not keep it: nothing in this paper places it.
            set_aside.extend(
                {"kind": "entity", "type": member["type"], "name": member["name"], "reason": UNPLACED}
                for member in group
            )
            continue
        placed += len(group)
        mapping.update({(member["window"], member["local_id"]): local_id for member in group})
        entities.append(row)
    require_conserved("merge entity", entered=len(members), kept=placed, set_aside=len(set_aside))
    return entities, mapping


def _carry_attributes(graphs, mapping, set_aside):
    attributes, seen = [], set()
    entered = repeated = unowned = 0
    for window, graph in enumerate(graphs):
        for attribute in graph.get("attributes", []):
            entered += 1
            local_id = mapping.get((window, attribute.get("local_id")))
            if local_id is None:
                set_aside.append({**attribute, "kind": "attribute", "reason": UNOWNED})
                unowned += 1
                continue
            refs = []
            for ref in attribute.get("refs") or []:
                target = mapping.get((window, ref))
                if target and target != local_id and target not in refs:
                    refs.append(target)
            row = {**attribute, "local_id": local_id, "refs": refs}
            key = (local_id, row["section"], row["text"], row["block_id"], row["quote"])
            if key in seen:
                repeated += 1  # The same line at the same place, given by two windows.
                continue
            seen.add(key)
            attributes.append(row)
    require_conserved("merge attribute", entered=entered, kept=len(attributes), set_aside=repeated + unowned)
    return attributes


def _carry_links(graphs, mapping, set_aside):
    related = []
    entered = joined = unowned = 0
    for window, graph in enumerate(graphs):
        for link in graph.get("related", []):
            entered += 1
            a, b = mapping.get((window, link.get("a"))), mapping.get((window, link.get("b")))
            if a is None or b is None:
                set_aside.append({**link, "kind": "related", "reason": UNOWNED})
                unowned += 1
            elif a == b:
                joined += 1  # Both ends became one entity.
            else:
                related.append({**link, "a": a, "b": b})
    require_conserved("merge related", entered=entered, kept=len(related), set_aside=joined + unowned)
    return related


def merge_windows(graphs):
    """Same type and the same normalized name or alias become one temporary entity. Attributes and links follow.

    Nothing leaves silently: what cannot be carried over is returned under `set_aside` with its reason.
    """
    set_aside: list[dict[str, object]] = []
    entities, mapping = _merge_entities(_members(graphs), set_aside)
    attributes = _carry_attributes(graphs, mapping, set_aside)
    related = _carry_links(graphs, mapping, set_aside)
    merged = {"entities": entities, "attributes": attributes, "related": collapse_related(related)}
    return {**merged, "set_aside": set_aside} if set_aside else merged


def linked_only(graph):
    """Formulations, organizations, and other entities that carry an attribute or a related link.
    The rest are listed, not written."""
    used = {attribute["local_id"] for attribute in graph["attributes"]}
    used |= {ref for attribute in graph["attributes"] for ref in attribute.get("refs") or []}
    used |= {end for link in graph["related"] for end in (link["a"], link["b"])}
    # Author affiliations rarely link to a formulation in a review, but stay findable by name.
    kept = [
        entity
        for entity in graph["entities"]
        if entity["type"] in ("formulation", "organization") or entity["local_id"] in used
    ]
    unlinked = [{"type": entity["type"], "name": entity["name"]} for entity in graph["entities"] if entity not in kept]
    return {**graph, "entities": kept}, unlinked


def link_named(graph):
    """An attribute whose quote names another entity of this paper refers to it, and the two are related at that quote.
    Run after merging, so an entity listed in any window counts."""
    attributes, extra = [], []
    for attribute in graph["attributes"]:
        refs = list(attribute.get("refs") or [])
        for local_id in named_in(attribute["quote"], graph["entities"], attribute["local_id"]):
            if local_id not in refs:
                refs.append(local_id)
                extra.append(
                    {
                        "a": attribute["local_id"],
                        "b": local_id,
                        "block_id": attribute["block_id"],
                        "quote": attribute["quote"],
                        "block_ids": attribute.get("block_ids"),
                    }
                )
        attributes.append({**attribute, "refs": refs})
    existing = [{"a": link["a"], "b": link["b"], **place} for link in graph["related"] for place in link["locations"]]
    return {**graph, "attributes": attributes, "related": collapse_related(existing + extra)}


def synonym_payload(graph):
    """Names of this paper's entities by type, for one synonym call. Types with fewer than two entities are left out."""
    payload = {}
    for entity in graph["entities"]:
        payload.setdefault(entity["type"], []).append(
            {"name": entity["name"], "aliases": list(entity.get("aliases") or [])}
        )
    return {key: value for key, value in payload.items() if len(value) > 1}


def merge_synonyms(graph, groups):
    """Merge entities the model grouped as one concept. Only names listed in this paper, of the same type, are used;
    the group's most mentioned entity keeps its id and name, the others become aliases."""
    by_name = {(entity["type"], normalize_name(entity["name"])): entity for entity in graph["entities"]}
    mapping, merged = {}, []
    for group in groups or []:
        members = []
        for name in group.get("names") or []:
            entity = by_name.get((group.get("type"), normalize_name(name)))
            if entity is not None and entity not in members and entity["local_id"] not in mapping:
                members.append(entity)
        if len(members) < 2:
            continue
        keep = max(members, key=lambda entity: (len(entity["mentions"]), len(entity["name"])))
        names = []
        for entity in members:
            names.extend([entity["name"], *(entity.get("aliases") or [])])
            if entity is not keep:
                mapping[entity["local_id"]] = keep["local_id"]
                for mention in entity["mentions"]:
                    if mention not in keep["mentions"]:
                        keep["mentions"].append(mention)
        keep["aliases"] = present_aliases(keep["name"], [*keep.get("aliases", []), *names])
        merged.append(
            {
                "type": keep["type"],
                "name": keep["name"],
                "merged": [entity["name"] for entity in members if entity is not keep],
            }
        )
    if not mapping:
        return graph, merged
    target = lambda local_id: mapping.get(local_id, local_id)
    attributes = []
    for attribute in graph["attributes"]:
        refs = []
        for ref in attribute.get("refs") or []:
            if target(ref) != target(attribute["local_id"]) and target(ref) not in refs:
                refs.append(target(ref))
        attributes.append({**attribute, "local_id": target(attribute["local_id"]), "refs": refs})
    links = [
        {"a": target(link["a"]), "b": target(link["b"]), **place}
        for link in graph["related"]
        for place in link["locations"]
    ]
    entities = [entity for entity in graph["entities"] if entity["local_id"] not in mapping]
    return {**graph, "entities": entities, "attributes": attributes, "related": collapse_related(links)}, merged
