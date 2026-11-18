"""Human confirmation of one paper's extracted entities before they are resolved against the knowledge base.

Follows the earlier candidate review: the extracted draft (`entity-graph.json`) is never changed; decisions are
kept beside it (`entity-reviews.json`) and both are versioned by hash, so a stale page cannot overwrite newer
decisions. Each decision is `confirmed`, `corrected` (with a correction) or `excluded`. Confirming applies the
decisions, then resolves, writes and groups claims exactly as automatic extraction did before.
"""

import json
import time
from pathlib import Path
from typing import TypedDict

from kb_ingest.core import StateConflict, atomic_json, fingerprint
from kb_ingest.core.contract_entity import normalize_name, present_aliases
from kb_ingest.core.workspace import graph_workspace
from kb_ingest.observation import hooks as _observe

from .entity_contract import ENTITY_TYPES
from .entity_merge import collapse_related
from .entity_model_calls import confirm_options as _confirm_options

DRAFT = "entity-graph.json"
REVIEWS = "entity-reviews.json"
BLOCKS = "entity-blocks.json"
DECISIONS = ("confirmed", "corrected", "excluded")
ENTITY_FIELDS = {"name", "type", "aliases", "merge_into"}
ATTRIBUTE_FIELDS = {"section", "text", "owner"}


class _EffectiveEntity(TypedDict, total=False):
    """The fields effective_graph reads and rewrites; entity rows carry further draft fields untouched."""

    name: str
    type: str
    mentions: list[object]
    aliases: list[str]


def attribute_key(row):
    return (
        "attribute:" + fingerprint([row["local_id"], row["section"], row["text"], row["block_id"], row["quote"]])[:20]
    )


def related_key(row):
    return "related:" + "|".join(sorted((row["a"], row["b"])))


def _core(draft):
    return {key: draft.get(key, []) for key in ("entities", "attributes", "related")}


def _locations(blocks, block_id, block_ids=None):
    from .entity_resolve import highlight_regions

    block = blocks.get(block_id) or {}
    regions = []
    for item in block_ids or [block_id]:
        other = blocks.get(item) or {}
        if other.get("page") == block.get("page"):
            regions.extend(highlight_regions(other))
    return {"page": block.get("page"), "asset_ref": block.get("asset_ref"), "highlight_regions": regions}


def load(root):
    path = root / DRAFT
    if not path.is_file():
        return None, {}, {}
    draft = json.loads(path.read_text())
    reviews = json.loads((root / REVIEWS).read_text()) if (root / REVIEWS).is_file() else {"reviews": {}}
    blocks = json.loads((root / BLOCKS).read_text()) if (root / BLOCKS).is_file() else {}
    return draft, reviews.get("reviews", {}), blocks


def draft_view(root):
    """The draft as the review page shows it: entities with their attributes and links, and every decision."""
    draft, reviews, blocks = load(root)
    if draft is None:
        return {"status": "not_available", "draft_sha256": None, "review_sha256": None, "entities": []}
    entities = {
        entity["local_id"]: {
            **entity,
            "key": entity["local_id"],
            "attributes": [],
            "related": [],
            "review": reviews.get(entity["local_id"]),
        }
        for entity in draft["entities"]
    }
    _add_attributes(draft, entities, blocks, reviews)
    _add_related(draft, entities, blocks, reviews)
    return {
        "status": "available",
        "draft_sha256": fingerprint(_core(draft)),
        "review_sha256": fingerprint(reviews),
        "entities": list(entities.values()),
        "unlinked": draft.get("unlinked", []),
        "decided": len(reviews),
        "summary": draft.get("summary"),
    }


def _add_attributes(draft, entities, blocks, reviews):
    for row in draft["attributes"]:
        if row["local_id"] in entities:
            key = attribute_key(row)
            cites = [
                {**item, **_locations(blocks, item["block_id"], item.get("block_ids"))}
                for item in row.get("citations") or []
                if item.get("block_id") in blocks
            ]
            entities[row["local_id"]]["attributes"].append(
                {
                    **row,
                    **_locations(blocks, row["block_id"], row.get("block_ids")),
                    "key": key,
                    "citations": cites,
                    "review": reviews.get(key),
                }
            )


def _add_related(draft, entities, blocks, reviews):
    for row in draft["related"]:
        key = related_key(row)
        place = row["locations"][0] if row.get("locations") else {}
        for end, other in ((row["a"], row["b"]), (row["b"], row["a"])):
            if end in entities and other in entities:
                entities[end]["related"].append(
                    {
                        "key": key,
                        "local_id": other,
                        "name": entities[other]["name"],
                        "type": entities[other]["type"],
                        "locations": [
                            {**location, **_locations(blocks, location["block_id"], location.get("block_ids"))}
                            for location in row.get("locations") or []
                        ],
                        "quote": place.get("quote"),
                        "review": reviews.get(key),
                    }
                )


def _check(draft, reviews, key, decision, correction):
    entity_ids = {entity["local_id"]: entity for entity in draft["entities"]}
    attributes = {attribute_key(row): row for row in draft["attributes"]}
    related = {related_key(row) for row in draft["related"]}
    if decision not in DECISIONS:
        raise ValueError("decision must be confirmed, corrected or excluded")
    correction = correction or {}
    if decision != "corrected" and correction:
        raise ValueError("only a corrected decision carries a correction")
    if key in entity_ids:
        if set(correction) - ENTITY_FIELDS:
            raise ValueError("entity correction supports name, type, aliases and merge_into")
        if "type" in correction and correction["type"] not in ENTITY_TYPES:
            raise ValueError("unknown entity type")
        if "name" in correction and not str(correction["name"]).strip():
            raise ValueError("entity name is required")
        if "aliases" in correction and (
            not isinstance(correction["aliases"], list)
            or not all(isinstance(item, str) for item in correction["aliases"])
        ):
            raise ValueError("aliases must be a list of names")
        target = correction.get("merge_into")
        if target is not None:
            kind = correction.get("type", entity_ids[key]["type"])
            if target == key or target not in entity_ids or entity_ids[target]["type"] != kind:
                raise ValueError("an entity can only be merged into another entity of the same type")
            if (reviews.get(target) or {}).get("decision") == "excluded":
                raise ValueError("cannot merge into an excluded entity")
        return "entity"
    if key in attributes:
        if set(correction) - ATTRIBUTE_FIELDS:
            raise ValueError("attribute correction supports section, text and owner")
        if "owner" in correction and correction["owner"] not in entity_ids:
            raise ValueError("attribute owner must be an entity of this paper")
        for field in ("section", "text"):
            if field in correction and not str(correction[field]).strip():
                raise ValueError(field + " is required")
        return "attribute"
    if key in related:
        if decision == "corrected":
            raise ValueError("a related link can be confirmed or excluded")
        return "related"
    raise ValueError("unknown review target: " + str(key))


def record(root, *, expected_draft_sha256, expected_review_sha256, operator, items):
    """Validate every item against the current versions, then commit them together."""
    draft, reviews, _ = load(root)
    if draft is None:
        raise ValueError("no extracted draft to review")
    if fingerprint(_core(draft)) != expected_draft_sha256:
        raise StateConflict("draft version changed; reload before reviewing")
    if fingerprint(reviews) != expected_review_sha256:
        raise StateConflict("review version changed; reload before reviewing")
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 200:
        raise ValueError("review operator is required")
    if not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError("a review must contain 1 to 200 decisions")
    updated = dict(reviews)
    for item in items:
        key, decision, correction = item.get("key"), item.get("decision"), item.get("correction")
        _check(draft, updated, key, decision, correction)
        updated[key] = {
            "decision": decision,
            "correction": correction or {},
            "note": str(item.get("note") or "")[:2000],
            "operator": operator.strip(),
            "at": time.time(),
        }
    atomic_json(root / REVIEWS, {"reviews": updated})
    return {
        "draft_sha256": expected_draft_sha256,
        "review_sha256": fingerprint(updated),
        "reviews": {item["key"]: updated[item["key"]] for item in items},
    }


def effective_graph(draft, reviews, *, with_review_keys=False):
    """The draft after decisions: excluded items removed, corrections applied, merges folded into their target."""
    excluded, target = _exclusions_and_merges(draft, reviews)
    entities = _effective_entities(draft, reviews, excluded, target)
    attributes = _effective_attributes(draft, reviews, entities, target, with_review_keys)
    links = _effective_links(draft, reviews, entities, target)
    return {
        "entities": [entity for entity in entities.values() if entity.get("mentions")],
        "attributes": attributes,
        "related": collapse_related(links),
    }


def _decision(reviews, key):
    return reviews.get(key) or {}


def _exclusions_and_merges(draft, reviews):
    """Entities excluded outright, and the merge target of every other entity a reviewer folded into another."""
    excluded = {
        entity["local_id"]
        for entity in draft["entities"]
        if _decision(reviews, entity["local_id"]).get("decision") == "excluded"
    }
    target = {}
    for entity in draft["entities"]:
        into = _decision(reviews, entity["local_id"]).get("correction", {}).get("merge_into")
        if into and entity["local_id"] not in excluded:
            target[entity["local_id"]] = into
    return excluded, target


def _final_id(target, local_id):
    seen = set()
    while local_id in target and local_id not in seen:
        seen.add(local_id)
        local_id = target[local_id]
    return local_id


def _effective_entities(draft, reviews, excluded, target):
    entities: dict[str, _EffectiveEntity] = {}
    for entity in draft["entities"]:
        local_id = entity["local_id"]
        if local_id in excluded or _final_id(target, local_id) in excluded:
            continue
        decision = _decision(reviews, local_id)
        correction = decision.get("correction", {}) if decision.get("decision") == "corrected" else {}
        name = " ".join(str(correction.get("name", entity["name"])).split())
        aliases = correction.get("aliases", entity.get("aliases") or [])
        row = {
            **entity,
            "name": name,
            "type": correction.get("type", entity["type"]),
            "aliases": present_aliases(name, [*aliases, *([entity["name"]] if name != entity["name"] else [])]),
        }
        owner = _final_id(target, local_id)
        if owner == local_id:
            entities[local_id] = {
                **entities.get(local_id, {}),
                **row,
                "mentions": [*entities.get(local_id, {}).get("mentions", []), *row["mentions"]],
                "aliases": present_aliases(name, [*entities.get(local_id, {}).get("aliases", []), *row["aliases"]]),
            }
        else:
            # Merged away: its names become aliases and its mentions move to the target.
            holder = entities.setdefault(owner, {"mentions": [], "aliases": []})
            holder["mentions"] = [*holder.get("mentions", []), *row["mentions"]]
            holder["aliases"] = [*holder.get("aliases", []), row["name"], *row["aliases"]]
    for entity in entities.values():
        entity["aliases"] = present_aliases(entity["name"], entity["aliases"])
    return entities


def _effective_attributes(draft, reviews, entities, target, with_review_keys):
    attributes = []
    for row in draft["attributes"]:
        review = _decision(reviews, attribute_key(row))
        if review.get("decision") == "excluded":
            continue
        correction = review.get("correction", {}) if review.get("decision") == "corrected" else {}
        owner = _final_id(target, correction.get("owner", row["local_id"]))
        if owner not in entities:
            continue
        refs = []
        for ref in row.get("refs") or []:
            ref = _final_id(target, ref)
            if ref in entities and ref != owner and ref not in refs:
                refs.append(ref)
        attributes.append(
            {
                **row,
                "local_id": owner,
                "section": " ".join(str(correction.get("section", row["section"])).split()),
                "text": str(correction.get("text", row["text"])).strip(),
                "refs": refs,
                **({"_review_key": attribute_key(row)} if with_review_keys else {}),
            }
        )
    return attributes


def _effective_links(draft, reviews, entities, target):
    links: list[dict[str, object]] = []
    for row in draft["related"]:
        if _decision(reviews, related_key(row)).get("decision") == "excluded":
            continue
        a, b = _final_id(target, row["a"]), _final_id(target, row["b"])
        if a in entities and b in entities and a != b:
            links.extend({"a": a, "b": b, **place} for place in row.get("locations") or [])
    return links


def registry(root):
    return json.loads((root / BLOCKS).read_text()) if (root / BLOCKS).is_file() else {}


def summary_counts(graph):
    counts: dict[str, int] = {}
    for entity in graph["entities"]:
        counts[entity["type"]] = counts.get(entity["type"], 0) + 1
    return counts


def normalize_names(graph):
    """Two entities a reviewer renamed to the same name and type become one, as merging windows would have done.
    Formulations with different in-text codes stay two samples: their shared name gets the code as a postfix."""
    from .entity_merge import merge_windows

    groups: dict[str, list[dict[str, object]]] = {}
    for entity in graph["entities"]:
        if entity["type"] == "formulation":
            groups.setdefault(normalize_name(entity["name"]), []).append(entity)
    renamed = {}
    for members in groups.values():
        codes = [normalize_name(entity.get("code") or "") for entity in members]
        if len(members) > 1 and all(codes) and len(set(codes)) == len(codes):
            for entity in members:
                renamed[entity["local_id"]] = f"{entity['name']}（{entity['code']}）"
    if renamed:
        graph = {
            **graph,
            "entities": [
                {**entity, "name": renamed[entity["local_id"]]} if entity["local_id"] in renamed else entity
                for entity in graph["entities"]
            ],
        }
    windows = [
        {
            "entities": graph["entities"],
            "attributes": graph["attributes"],
            "related": [
                {"a": link["a"], "b": link["b"], **place} for link in graph["related"] for place in link["locations"]
            ],
        }
    ]
    return (
        merge_windows(windows)
        if len({(e["type"], normalize_name(e["name"])) for e in graph["entities"]}) < len(graph["entities"])
        else graph
    )


def _entity_task(service, task_id):
    task = service.store.get_task(task_id)
    if (
        task["source_type"] not in ("paper", "patent")
        or task["options"].get("_document_extraction_mode") != "entity-graph-v1"
    ):
        raise ValueError("entity review is only available for papers and patents extracted as entities")
    return task


def task_root(service, task_id):
    graph_workspace(service)
    task = _entity_task(service, task_id)
    return task, service._document_artifact_root(task)


def review_task(service, task_id, **values):
    task = _entity_task(service, task_id)
    with service.store.lock("source:" + task["source_id"]):
        task = service.store.get_task(task_id)
        if task["state"] != "needs_review":
            raise StateConflict("只有待核对的提取结果可以修改")
        return record(service._document_artifact_root(task), **values)


def confirm_task(service, task_id, *, expected_draft_sha256, expected_review_sha256, operator, model_client=None):
    """Apply decisions and write into the knowledge base: needs_review -> publishing -> saved.
    A failure returns the task to needs_review with its draft and decisions intact."""
    from kb_ingest.extraction.entity_pipeline import confirm_entity_draft, task_call, task_embedder
    from kb_ingest.extraction.entity_review_receipt import publication_for
    from kb_ingest.storage.entity_store import EntityStore

    task = _entity_task(service, task_id)
    with service.store.lock("source:" + task["source_id"]):
        task = service.store.get_task(task_id)
        if task["state"] == "saved":
            return task
        root = service._document_artifact_root(task)
        with _observe.stage(root, "review_validation", "publication_error"):
            draft, reviews, blocks = load(root)
            if draft is None:
                raise ValueError("no extracted draft to confirm")
            if fingerprint(_core(draft)) != expected_draft_sha256 or fingerprint(reviews) != expected_review_sha256:
                raise StateConflict("draft or review version changed; reload before confirming")
            if not isinstance(operator, str) or not operator.strip():
                raise ValueError("review operator is required")
            from .entity_review_evidence import validate_review_evidence

            validate_review_evidence(draft, reviews, blocks)
        workspace = graph_workspace(service)
        service.store.begin_publish(task_id)
        try:
            options = _confirm_options(service, task, task_id, workspace, model_client)
            package = service.candidate(task_id)
            run_id = package["header"]["processing_run_id"]
            call = task_call(options, package, run_id)
            embedder = task_embedder(options, run_id, service.store.root / "cache" / "entity-vectors")

            confirm_entity_draft(
                Path(root),
                entity_store=EntityStore(service.store),
                workspace_id=workspace,
                source_id=task["source_id"],
                call=call,
                embedder=embedder,
                publish=publication_for(service, task_id, operator),
            )
        except Exception as exc:
            _observe.tracker(root).fail(exc)
            service.store.publication_failed(task_id, f"{type(exc).__name__}: 写入知识库未完成，可重试", unknown=False)
            raise
        return service.store.get_task(task_id)
