"""Block evidence and citation indexes exposed on the ingestion service."""

import json

from kb_ingest.document_progress import document_title
from kb_ingest.storage.task_files import checked_child

PASSAGE_CHARS = 1500
DOCUMENT_TYPES = ("paper", "patent")


def load_artifact(service, task, name):
    try:
        path = checked_child(service._document_artifact_root(task), name)
        return json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def source_index(service):
    """Use the published task for citations; a newer draft must not redirect old evidence."""
    rows = {}
    for task in [*service.store.list_published_tasks(), *service.store.list_tasks()]:
        if task["source_type"] in DOCUMENT_TYPES and task["source_id"] not in rows:
            rows[task["source_id"]] = {
                "task_id": task["task_id"],
                "title": document_title(service.store, task),
                "asset_names": {a: service.store.get_asset(a)["original_name"] for a in task["asset_ids"]},
                "asset_ids": list(task.get("asset_ids") or []),
            }
    return rows


def read_source_passage(service, source_id, block_id, around=2):
    """The original text of one block and its neighbours in reading order, for a quote's context."""
    row = source_index(service).get(source_id)
    if row is None:
        raise KeyError(source_id)
    task = service.store.get_task(row["task_id"])
    blocks = load_artifact(service, task, "entity-blocks.json") or {}
    if blocks and all("text" in item for item in blocks.values()):
        ordered = [{"block_id": key, **item} for key, item in blocks.items()]
    else:
        ordered = _rebuild_source_blocks(service, task)
    cell = next((item for item in ordered if item["block_id"] == block_id and item.get("kind") == "table_cell"), None)
    if cell is not None and cell.get("table_region"):
        block_id = cell["table_region"]  # A table value's context is its whole table.
    ordered = [item for item in ordered if (item.get("text") or "").strip() and item.get("kind") != "table_cell"]
    position = next((index for index, item in enumerate(ordered) if item["block_id"] == block_id), None)
    if position is None:
        raise KeyError(block_id)
    selected_asset = ordered[position].get("asset_ref")
    if selected_asset is not None:
        ordered = [item for item in ordered if item.get("asset_ref") == selected_asset]
        position = next(index for index, item in enumerate(ordered) if item["block_id"] == block_id)
    elif len(task["asset_ids"]) > 1:
        # Legacy blocks without a document locator cannot safely supply neighbouring documents.
        ordered = [ordered[position]]
        position = 0
    around = max(0, min(int(around), 5))
    rows = []
    for item in ordered[max(0, position - around) : position + around + 1]:
        text = item["text"] if len(item["text"]) <= PASSAGE_CHARS else item["text"][:PASSAGE_CHARS] + "…"
        rows.append(
            {
                "block_id": item["block_id"],
                "page": item.get("page"),
                "text": text,
                **({"asset_ref": selected_asset} if selected_asset else {}),
                **({"cited": True} if item["block_id"] == block_id else {}),
            }
        )
    return {
        "source": row.get("asset_names", {}).get(selected_asset, row["title"]),
        "blocks": rows,
        **({"asset_ref": selected_asset} if selected_asset else {}),
    }


def _rebuild_source_blocks(service, task):
    from .entity_pipeline_blocks import rebuild_blocks

    payload = load_artifact(service, task, "document-ir.json") or {}
    assets = []
    for asset_id in task.get("asset_ids") or []:
        asset = dict(service.store.get_asset(asset_id))
        asset["local_path"] = str(service.store.asset_path(asset_id))
        assets.append(asset)
    return rebuild_blocks(payload.get("documents") or [], assets, payload.get("visual_readings") or [])


class EntityEvidenceMixin:
    def load_artifact(self, task, name):
        """Read one named extraction artifact for this task, or None if it is missing or unreadable."""
        return load_artifact(self, task, name)

    def source_citation_index(self):
        """Published task first; a newer draft must not redirect old evidence."""
        return source_index(self)

    def source_passage(self, source_id, block_id, around=2):
        """The original text of one block and its neighbours in reading order, for a quote's context."""
        return read_source_passage(self, source_id, block_id, around)
