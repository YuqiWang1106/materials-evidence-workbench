"""Reads for the paper page and the knowledge graph: nodes, edges, one entity, and the original page."""

import base64

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .extraction.entity_contract import ENTITY_TYPES
from .extraction.entity_resolve import highlight_regions
from .http import website_fields as _times
from .ingest.documents.document_pipeline_blocks import ir_blocks
from .storage.entity_store import EntityStore

DOCUMENT_TYPES = ("paper", "patent")


def highlights_by_block(documents):
    boxes = {}
    for block in ir_blocks(documents or []):
        regions = highlight_regions(block)
        if regions:
            boxes[block["block_id"]] = regions
    return boxes


def attach_highlights(entity, boxes):
    if not entity or not boxes:
        return entity

    def place(item):
        if not isinstance(item, dict) or item.get("highlight_regions") or not item.get("block_id"):
            return item
        regions = boxes.get(item["block_id"])
        return {**item, "highlight_regions": regions} if regions else item

    attributes = [
        {**place(item), "citations": [place(other) for other in item["citations"]]}
        if item.get("citations")
        else place(item)
        for item in entity.get("attributes") or []
    ]
    related = [
        {**item, "locations": [place(location) for location in item.get("locations") or []]}
        for item in entity.get("related") or []
    ]
    return {**entity, "attributes": attributes, "related": related}


def load_highlights(service, task):
    payload = service.load_artifact(task, "document-ir.json") or {}
    try:
        return highlights_by_block(payload.get("documents") or [])
    except (KeyError, TypeError, ValueError):
        return {}


def task_title(service, task):
    from .document_progress import document_title

    return document_title(service.store, task)


def only_source(entity, source_id):
    """An entity as one paper shows it: its links and attributes that this paper supports. A material shared by
    several papers lists only this paper's recipes here; the workspace graph shows all of them."""

    def here(item):
        return item.get("source_id") == source_id or any(
            other.get("source_id") == source_id for other in item.get("citations") or []
        )

    related = []
    for item in entity.get("related") or []:
        places = [place for place in item.get("locations") or [] if place.get("source_id") == source_id]
        if places:
            related.append({**item, "locations": places})
    return {**entity, "attributes": [item for item in entity.get("attributes") or [] if here(item)], "related": related}


def only_asset(entity, asset_id):
    if not entity or asset_id is None:
        return entity
    attributes = []
    for item in entity.get("attributes") or []:
        places = [p for p in [item, *(item.get("citations") or [])] if p.get("asset_ref") == asset_id]
        if places:
            value = {**item, **places[0], "citations": places[1:], "same_claims": []}
            attributes.append(value)
    related = [
        {**item, "locations": [p for p in item.get("locations") or [] if p.get("asset_ref") == asset_id]}
        for item in entity.get("related") or []
    ]
    return {**entity, "attributes": attributes, "related": [item for item in related if item["locations"]]}


def label_sources(entity, sources):
    """Every location says which paper and task it belongs to."""
    if not entity:
        return entity

    def label(item):
        source = sources.get(item.get("source_id")) if isinstance(item, dict) else None
        if source is None:
            for row in sources.values():
                if item.get("asset_ref") in row["asset_ids"]:
                    source = row
                    break
        return (
            {
                **item,
                "task_id": source["task_id"],
                "source_title": source.get("asset_names", {}).get(item.get("asset_ref"), source["title"]),
            }
            if source
            else item
        )

    def with_cites(item):
        item = label(item)
        return {**item, "citations": [label(other) for other in item["citations"]]} if item.get("citations") else item

    result = {
        **entity,
        "attributes": [with_cites(item) for item in entity.get("attributes") or []],
        "related": [
            {**item, "locations": [label(place) for place in item.get("locations") or []]}
            for item in entity.get("related") or []
        ],
    }
    for item in result["attributes"]:
        if item.get("same_claims"):
            item["same_claims"] = [label(other) for other in item["same_claims"]]
    if entity.get("source_id") in sources:
        result["source_title"] = sources[entity["source_id"]]["title"]
        result["task_id"] = sources[entity["source_id"]]["task_id"]
    return result


class EntityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300)
    decision: str
    correction: dict | None = None
    note: str = Field(default="", max_length=2000)


class EntityReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator: str = Field(min_length=1, max_length=200)
    items: list[EntityDecision] = Field(min_length=1, max_length=200)


class EntityConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operator: str = Field(min_length=1, max_length=200)


class SupplementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    window_ids: list[str] = Field(min_length=1, max_length=100)
    operator: str = Field(min_length=1, max_length=200)


class SupplementRefine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    unit_ids: list[str] = Field(min_length=1, max_length=100)
    operator: str = Field(min_length=1, max_length=200)
    acknowledge_unknown: bool
    max_chars: int = Field(default=1000, ge=400, le=1200)


class SupplementApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def routes(service, *, workspace_id, index=None):
    router = APIRouter(prefix="/api")

    def prepare_index():
        # Embeddings for the newly written entities; search falls back to word match until they exist.
        if index is not None:
            index.prepare_in_background()

    store = EntityStore(service.store)
    from .extraction.entity_supplement import create, load_run
    from .extraction.entity_supplement_run import apply, execute, recheck, refine

    def read(entity_id, task=None):
        entity = store.read_entity(workspace_id, entity_id)
        if entity is None:
            raise KeyError(entity_id)
        if task is not None:
            entity = attach_highlights(only_source(entity, task["source_id"]), load_highlights(service, task))
        return label_sources(entity, service.source_citation_index())

    @router.get("/tasks/{task_id}/entity-graph")
    async def entity_graph(
        task_id: str, entity_id: str | None = Query(default=None), asset_id: str | None = Query(default=None)
    ):
        task = await run_in_threadpool(service.store.get_task, task_id)
        if task["source_type"] not in DOCUMENT_TYPES:
            raise ValueError("entity graph is only available for a paper or patent")

        if asset_id is not None and asset_id not in task["asset_ids"]:
            raise ValueError("asset does not belong to this task")

        def load():
            # Files may be fully prepared before the atomic publication commits. They are not
            # a visible publication until the task receipt and graph have committed together.
            published = task["state"] == "saved"
            graph = (
                store.graph(workspace_id, task["source_id"], asset_id=asset_id)
                if published
                else {"nodes": [], "edges": []}
            )
            nodes = graph["nodes"]
            formulations = [row for row in nodes if row["type"] == "formulation"]
            chosen = (
                (
                    entity_id
                    or (formulations[0]["entity_id"] if formulations else (nodes[0]["entity_id"] if nodes else None))
                )
                if published
                else None
            )
            if chosen and chosen not in {node["entity_id"] for node in nodes}:
                raise ValueError("entity outside selected document")
            entity = only_asset(read(chosen, task), asset_id) if chosen else None
            resolved = (service.load_artifact(task, "entity-graph-resolved.json") or {}) if published else {}
            return {
                "source_id": task["source_id"],
                "title": service.store.get_asset(asset_id)["original_name"] if asset_id else task_title(service, task),
                "graph": graph,
                "entities": [{key: node[key] for key in ("entity_id", "type", "name")} for node in nodes],
                "formulations": formulations,
                "entity": entity,
                "summary": resolved.get("summary") if asset_id is None else None,
            }

        return await run_in_threadpool(load)

    @_times.entities.route(router.get, "/tasks/{task_id}/entity-draft")
    async def entity_draft(task_id: str):
        from .extraction.entity_review import draft_view, task_root
        from .extraction.entity_review_held import held_view

        def load():
            task, root = task_root(service, task_id)
            view = {**draft_view(root), "task_id": task_id, "state": task["state"], "title": task_title(service, task)}
            # Lines the extraction set aside: shown beside the draft, never written with it.
            return {**view, "held": held_view(root)}

        return await run_in_threadpool(load)

    @_times.entity_reviews.route(router.post, "/tasks/{task_id}/entity-draft/reviews")
    async def entity_draft_reviews(task_id: str, body: EntityReviewBatch):
        from .extraction.entity_review import review_task

        values = body.model_dump()
        values["items"] = [{key: value for key, value in item.items()} for item in values["items"]]
        return await run_in_threadpool(lambda: review_task(service, task_id, **values))

    @router.post("/tasks/{task_id}/entity-draft/confirm")
    async def entity_draft_confirm(task_id: str, body: EntityConfirm):
        from .api import public_task
        from .extraction.entity_review import confirm_task

        task = await run_in_threadpool(lambda: confirm_task(service, task_id, **body.model_dump()))
        prepare_index()
        return public_task(task)

    @_times.runs.route(router.get, "/tasks/{task_id}/entity-supplements")
    async def entity_supplements(task_id: str):
        from .extraction.entity_supplement import view

        return await run_in_threadpool(lambda: view(service, task_id))

    @_times.timestamps.route(router.post, "/tasks/{task_id}/entity-supplements")
    async def entity_supplement_create(task_id: str, body: SupplementCreate):
        run = await run_in_threadpool(lambda: create(service, task_id, **body.model_dump()))
        if run["state"] == "queued" and not service._stop.is_set():
            service.pool.submit(execute, service, task_id, run["run_id"])
        return run

    @_times.timestamps.route(router.post, "/tasks/{task_id}/entity-supplements/{run_id}/refine")
    async def entity_supplement_refine(task_id: str, run_id: str, body: SupplementRefine):
        run = await run_in_threadpool(lambda: refine(service, task_id, run_id, **body.model_dump()))
        if run["state"] == "queued" and not service._stop.is_set():
            service.pool.submit(execute, service, task_id, run["run_id"])
        return run

    @_times.timestamps.route(router.post, "/tasks/{task_id}/entity-supplements/{run_id}/recheck")
    async def entity_supplement_recheck(task_id: str, run_id: str):
        run = await run_in_threadpool(lambda: recheck(service, task_id, run_id))
        if run["state"] == "queued" and not service._stop.is_set():
            service.pool.submit(execute, service, task_id, run_id)
        return run

    @_times.timestamps.route(router.post, "/tasks/{task_id}/entity-supplements/{run_id}/resume")
    async def entity_supplement_resume(task_id: str, run_id: str):
        run = await run_in_threadpool(lambda: load_run(service, task_id, run_id))
        if run["state"] in ("queued", "running", "interrupted") and not service._stop.is_set():
            service.pool.submit(execute, service, task_id, run_id)
        return run

    @_times.timestamps.route(router.post, "/tasks/{task_id}/entity-supplements/{run_id}/apply")
    async def entity_supplement_apply(task_id: str, run_id: str, body: SupplementApply):
        result = await run_in_threadpool(lambda: apply(service, task_id, run_id, **body.model_dump()))
        prepare_index()
        return result

    @router.get("/entity-graph")
    async def workspace_graph(
        types: str | None = Query(default=None, max_length=200),
        source_id: str | None = Query(default=None, max_length=300),
    ):
        chosen = [item for item in (types or "").split(",") if item]
        if any(item not in ENTITY_TYPES for item in chosen):
            raise ValueError("unknown entity type")

        def load():
            sources = service.source_citation_index()
            graph = store.graph(workspace_id, source_id, entity_types=chosen or None)
            used = {node["source_id"] for node in graph["nodes"] if node.get("source_id")}
            return {
                **graph,
                "sources": {
                    key: {"task_id": value["task_id"], "title": value["title"]}
                    for key, value in sources.items()
                    if key in used or key == source_id
                },
            }

        return await run_in_threadpool(load)

    @router.get("/entities/search")
    async def entity_search(
        q: str = Query(min_length=1, max_length=200), entity_type: str | None = Query(default=None)
    ):
        if entity_type is not None and entity_type not in ENTITY_TYPES:
            raise ValueError("unknown entity type")
        return {"entities": await run_in_threadpool(store.search_entities, workspace_id, q, entity_type)}

    @router.get("/entities/{entity_id}")
    async def entity_detail(entity_id: str):
        return await run_in_threadpool(read, entity_id)

    @router.get("/tasks/{task_id}/source-page")
    async def source_page(task_id: str, asset_id: str = Query(min_length=1), page: int = Query(ge=1)):
        def render():
            task = service.store.get_task(task_id)
            if task["source_type"] not in DOCUMENT_TYPES:
                raise ValueError("PDF page preview is only available for a paper or patent")
            if asset_id not in task["asset_ids"]:
                raise ValueError("page is not from this file")
            asset = service.store.get_asset(asset_id)
            from .ingest.documents.readers.documents import render_page

            _, uri = render_page(
                {
                    "asset_id": asset_id,
                    "local_path": str(service.store.asset_path(asset_id)),
                    "sha256": asset["sha256"],
                },
                page,
            )
            return base64.b64decode(uri.split(",", 1)[1])

        body = await run_in_threadpool(render)
        return Response(body, media_type="image/png")

    return router
