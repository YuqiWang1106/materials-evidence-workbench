"""Mark interpretations that state the same thing about the same entities, so search shows them together."""

from kb_ingest.core import fingerprint

from .entity_contract import CLAIM_INSTRUCTIONS, CLAIM_SCHEMA, validate_instance

MAX_PAIRS = 60


def claim_pairs(claims, source_id):
    """Pairs of 结论 / 解释 with the same involved entities (at least two): one from this paper, one from another.
    Two attributes of one paper are not paired: a comparison written on each compared formulation is the same
    source sentence, not a second finding."""
    by_set: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for claim in claims:
        if len(claim["involves"]) >= 2:
            by_set.setdefault(tuple(claim["involves"]), []).append(claim)
    pairs = []
    for group in by_set.values():
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                if source_id not in (left["source_id"], right["source_id"]) or left["source_id"] == right["source_id"]:
                    continue
                if left["claim_group"] and left["claim_group"] == right["claim_group"]:
                    continue
                pairs.append((left, right))
    return pairs[:MAX_PAIRS]


def group_claims(store, workspace_id, source_id, call):
    """One model call judges every candidate pair; pairs judged the same share one claim_group.
    Both texts and both locations stay as they are."""
    summary, groups = plan_claim_groups(store.claims_by_refs(workspace_id), source_id, call)
    store.set_claim_groups(workspace_id, groups)
    return summary


def plan_claim_groups(claims, source_id, call):
    """Prepare group changes without writing, so model calls never hold the publication transaction."""
    pairs = claim_pairs(claims, source_id)
    if not pairs:
        return {"pairs": 0, "same": 0}, {}
    payload = {
        "pairs": [
            {
                "pair_id": "p" + str(index + 1),
                "first": left["text"] + "（原文：" + left["quote"] + "）",
                "second": right["text"] + "（原文：" + right["quote"] + "）",
            }
            for index, (left, right) in enumerate(pairs)
        ]
    }
    result = call("claims", CLAIM_INSTRUCTIONS, payload, CLAIM_SCHEMA)
    validate_instance(result, CLAIM_SCHEMA)
    same = {row["pair_id"] for row in result["judgements"] if row["same"]}
    by_id = {claim["attribute_id"]: claim for claim in claims}
    groups = _new_group_names(by_id, _claim_unions(claims, pairs, same))
    return {"pairs": len(pairs), "same": len(same), "grouped": len(groups)}, groups


def _find(parent, key):
    parent.setdefault(key, key)
    while parent[key] != key:
        parent[key] = parent[parent[key]]
        key = parent[key]
    return key


def _claim_unions(claims, pairs, same):
    """Union-find over claims: existing groups stay joined and pairs judged the same are joined."""
    parent: dict[str, str] = {}
    for claim in claims:
        if claim["claim_group"]:
            parent[_find(parent, claim["attribute_id"])] = _find(parent, "group:" + claim["claim_group"])
    for index, (left, right) in enumerate(pairs):
        if "p" + str(index + 1) in same:
            parent[_find(parent, left["attribute_id"])] = _find(parent, right["attribute_id"])
    return parent


def _new_group_names(by_id, parent):
    """Group name per claim whose group changes."""
    members: dict[str, list[str]] = {}
    for key in list(parent):
        if key in by_id:
            members.setdefault(_find(parent, key), []).append(key)
    groups = {}
    for root, keys in members.items():
        if len(keys) < 2 and not str(root).startswith("group:"):
            continue
        name = root[len("group:") :] if str(root).startswith("group:") else "claim:" + fingerprint(sorted(keys))[:20]
        for key in keys:
            if by_id[key]["claim_group"] != name:
                groups[key] = name
    return groups
