"""Versioned plan projection and consent; no separate preview extraction rules."""

import json
from copy import deepcopy

from kb_ingest.core import StateConflict, atomic_json, fingerprint


def is_plan_recipe(recipe):
    return recipe.get("version") == "2"


def scope_hashes(plan, assets, pipeline, *, reader_options=None, annotations=None):
    """Dependency closure is conservative, deterministic and owned by the backend.

    A join couples its two scopes. Source bytes and shared view interpretation are
    also dependencies; no frontend approximation may retain stale consent.
    """
    views = {v["id"]: v for v in plan["views"]}
    scopes = {s["id"]: s for s in plan["scopes"]}
    groups = {key: {key} for key in scopes}
    for join in plan.get("joins", []):
        left, right = join["left_scope"], join["right_scope"]
        groups[left].add(right)
        groups[right].add(left)
    changed = True
    while changed:
        changed = False
        for key, group in groups.items():
            expanded = set().union(group, *(groups[other] for other in group))
            if expanded != group:
                groups[key] = expanded
                changed = True
    hashes = {a["asset_id"]: a["sha256"] for a in assets}
    result = {}
    for key, dependencies in groups.items():
        selected_views = {scopes[s]["view"] for s in dependencies}
        result[key] = fingerprint(
            {
                "pipeline": pipeline,
                "version": plan["version"],
                "reader_options": {
                    views[v]["asset_id"]: (reader_options or {}).get(views[v]["asset_id"], {})
                    for v in sorted(selected_views)
                },
                "annotations": annotations or {},
                "views": [views[v] for v in sorted(selected_views)],
                "assets": {views[v]["asset_id"]: hashes[views[v]["asset_id"]] for v in sorted(selected_views)},
                "scopes": [scopes[s] for s in sorted(dependencies)],
                "objects": {name: obj for name, obj in plan["objects"].items() if obj["scope"] in dependencies},
                "fields": [f for f in plan["fields"] if f["scope"] in dependencies],
                "contexts": [c for c in plan.get("contexts", []) if c["scope"] in dependencies],
                "relations": [r for r in plan.get("relations", []) if r["scope"] in dependencies],
                "joins": [
                    j
                    for j in plan.get("joins", [])
                    if j["left_scope"] in dependencies or j["right_scope"] in dependencies
                ],
            }
        )
    return result


def preview(service, draft, payload):
    from .table_plan import preview_plan
    from .table_reading import _selected_tables
    from .table_results import result_view

    assets = service._table_assets(draft["asset_ids"])
    from .row_measurements import inspect_draft, is_profile

    restricted = is_profile(draft["recipe"])
    applicability = completion = None
    if restricted:
        generated, applicability, completion = inspect_draft(service, draft)
        if generated != draft["recipe"]["reading_plan"]:
            raise ValueError("行式定义与执行计划不一致")
        if not applicability["ready"]:
            return {
                "source_id": draft["source_id"],
                "revision": draft["revision"],
                "draft_sha256": draft["draft_sha256"],
                "entry_profile": draft["recipe"]["entry_profile"],
                "applicability": applicability,
                "completion": completion or {"selected_scope_complete": False, "whole_file_complete": False},
                "rows": [],
                "items": [],
                "total": 0,
                "offset": payload.get("offset", 0),
                "limit": payload.get("limit", 30),
                "has_more": False,
                "model_calls": 0,
                "reading_confirmed": False,
                "diagnostics": applicability["issues"],
            }
    scope_id = payload.get("scope_id")
    plan = draft["recipe"]["reading_plan"]
    if scope_id is not None and scope_id not in {s["id"] for s in plan["scopes"]}:
        raise ValueError("请选择当前读法中的数据组")
    offset, limit = payload.get("offset", 0), payload.get("limit", 30)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("读法预览分页范围无效")
    # One replaceable cache per source, pinned to immutable bytes, reader choices,
    # the complete draft and the running code. It is an execution artifact, not a
    # reusable interpretation for another file. Source bytes were checked above.
    cache_key = fingerprint(
        {
            "draft": draft["draft_sha256"],
            "pipeline": service.pipeline_fingerprint,
            "assets": {a["asset_id"]: a["sha256"] for a in assets},
        }
    )
    cache_path = service.store.root / "cache" / "table-preview" / (fingerprint(draft["source_id"]) + ".json")
    projected = None
    try:
        cached = json.loads(cache_path.read_text())
        if cached.get("key") == cache_key and fingerprint(cached["projection"]) == cached.get("sha256"):
            projected = cached["projection"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if projected is None:
        tables = {a["asset_id"]: _selected_tables(a, draft["reader_options"].get(a["asset_id"], {})) for a in assets}
        projected = preview_plan(
            assets, draft["source_id"], plan, "preview:" + draft["draft_sha256"], tables_by_asset=tables
        )
        from kb_ingest.contract import validate_package

        validate_package(projected["package"])
        atomic_json(cache_path, {"key": cache_key, "projection": projected, "sha256": fingerprint(projected)})
    package = projected["package"]
    if draft["recipe"].get("standard_review"):
        package = deepcopy(package)
        package["header"]["standard_table_review"] = {
            **draft["recipe"]["standard_review"],
            "human_meaning_confirmed": False,
            "scientific_expert_review": False,
        }
    if restricted:
        from .row_measurements import annotate

        annotate(package, draft["recipe"], completion)
    from .table_reading import apply_plan_exclusions

    apply_plan_exclusions(package, draft["recipe"].get("excluded_ranges", []), "preview")
    page = result_view(
        package,
        {"task_id": None, "source_id": draft["source_id"], "state": "preview"},
        offset=offset,
        limit=limit,
        scope_id=scope_id,
    )
    latest = service.store.get_table_draft(draft["source_id"])
    if latest["draft_sha256"] != draft["draft_sha256"] or latest["revision"] != draft["revision"]:
        raise StateConflict("草稿已变化，请重新查看读法预览")
    hashes = scope_hashes(
        plan,
        assets,
        service.pipeline_fingerprint,
        reader_options=draft["reader_options"],
        annotations={k: draft["recipe"].get(k, []) for k in ("supplements", "excluded_ranges")},
    )
    # Keep the full graph out of paginated responses. Object/edge pagination is
    # explicit, so a limited projection is never presented as the complete graph.
    objects = {o["object_id"]: o for o in package["objects"]}
    scoped_objects = {
        o["object_id"]
        for o in package["objects"]
        if any(
            v.get("record_scope", {}).get("scope") == scope_id
            for v in o.get("source_context", {}).get("occurrences", [])
        )
    }
    edges = [
        r
        for r in package["relations"]
        if scope_id is None or r["from_ref"] in scoped_objects or r["to_ref"] in scoped_objects
    ]
    relations = [
        {**edge, "from_label": objects[edge["from_ref"]]["label"], "to_label": objects[edge["to_ref"]]["label"]}
        for edge in edges[offset : offset + limit]
    ]
    manifest = {
        "plan_hash": projected["plan_hash"],
        "compiler_version": projected["compiler_version"],
        "quality_version": projected["quality_version"],
        "pipeline_fingerprint": service.pipeline_fingerprint,
        "scope_hashes": hashes,
        "asset_hashes": {a["asset_id"]: a["sha256"] for a in assets},
    }
    return {
        **page,
        **(
            {
                "entry_profile": draft["recipe"]["entry_profile"],
                "applicability": applicability,
                "completion": completion,
            }
            if restricted
            else {}
        ),
        "source_id": draft["source_id"],
        "revision": draft["revision"],
        "draft_sha256": draft["draft_sha256"],
        "version": "2",
        "scope_id": scope_id,
        "plan_hash": projected["plan_hash"],
        "scope_hashes": hashes,
        "execution_manifest": manifest,
        "items": page["rows"],
        "relations": relations,
        "relation_total": len(edges),
        "relation_has_more": offset + len(relations) < len(edges),
        "reading_confirmed": False,
        "model_calls": 0,
        "preview_kind": "sampled_review",
        "diagnostics": deepcopy(package["issues"][:100]),
        "diagnostic_total": len(package["issues"]),
    }


def confirm_manifest(service, draft, payload):
    view = preview(service, draft, {"offset": 0, "limit": 1})
    if view.get("applicability", {}).get("ready") is False:
        raise StateConflict("所选范围仍有关键问题，请修改读法")
    if payload.get("expected_plan_hash") != view["plan_hash"]:
        raise StateConflict("读法与预览版本不同，请重新预览并确认 / plan preview required")
    reviewed = payload.get("reviewed_scope_hashes", draft["recipe"].get("reviewed_scopes", {}))
    if reviewed != view["scope_hashes"]:
        raise StateConflict("仍有数据组尚未核对，或读法已变化，请重新预览确认")
    return view["execution_manifest"]
