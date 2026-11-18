"""Request-local access to saved entity publications and their scoped graph contributions."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TypedDict, TypeGuard

from ..core import StateConflict, fingerprint
from ..core.contract_entity import normalize_name
from ..mcp_contract.protocol import AgentKnowledgeError
from ..mcp_contract.requests import source_list
from ..storage import task_files
from ..storage.entity_store import EntityStore
from ..store import Store

type Record = dict[str, object]


class Graph(TypedDict):
    entities: list[Record]
    attributes: list[Record]
    related: list[Record]


class PublicationPart(TypedDict):
    task: Record
    package: Record
    run: str
    graph: Graph
    blocks: Record
    graph_hash: str
    blocks_hash: str
    assets: dict[str, Record]


class Publication(PublicationPart):
    publications: list[PublicationPart]


type PublicationCache = dict[str, Publication]


class Source(TypedDict):
    source_id: str
    title: str


class _CatalogSource(Source):
    names: list[str]


class Scope(TypedDict):
    source_ids: list[str] | None
    sources: list[Source]


class _Entity(TypedDict):
    entity_id: str
    type: str
    name: str
    aliases: list[str]
    source_id: str
    source_ids: list[str]


def _record(value: object, message: str = "Invalid published artifact structure.") -> Record:
    if not isinstance(value, dict):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", message)
    return value


def _is_records(value: object) -> TypeGuard[list[Record]]:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def records(value: object) -> list[Record]:
    if not _is_records(value):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid published artifact structure.")
    return value


def text_field(row: Record, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid published artifact structure.")
    return value


def string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid published artifact structure.")
    return value


def _is_graph(value: Record) -> TypeGuard[Graph]:
    return all(_is_records(value.get(key)) for key in ("entities", "attributes", "related"))


def _graph(value: Record) -> Graph:
    if not _is_graph(value):
        raise AgentKnowledgeError("INTEGRITY_FAILURE", "The published entity graph is incomplete.")
    return value


@dataclass(frozen=True, slots=True)
class _PublicationFiles:
    """Resolve each artifact through the task repository's workspace and symlink guards."""

    store: Store
    task_id: str

    def path(self, relative: Path) -> Path:
        if Path(self.task_id).name != self.task_id or self.task_id in {"", ".", ".."}:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid published task identity.")
        try:
            return task_files.checked_path(self.store.data_root, self.store.root, self.task_id, (relative,))
        except StateConflict as exc:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid published artifact location.") from exc

    def read(self, relative: Path) -> tuple[Record, str]:
        try:
            raw = self.path(relative).read_bytes()
        except OSError as exc:
            raise AgentKnowledgeError("SOURCE_NOT_READY", "A published artifact is unavailable.") from exc
        try:
            value: object = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "A published artifact is not valid JSON.") from exc
        return _record(value), sha256(raw).hexdigest()

    def candidate(self, task: Record) -> Record:
        package, _ = self.read(Path("candidate.json"))
        if fingerprint(package) != task.get("package_sha256"):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The published candidate has changed.")
        return package

    def original(self, task: Record) -> PublicationPart:
        package = self.candidate(task)
        header = _record(package.get("header") or {})
        run = f"{self.task_id}:attempt{task['attempt']}"
        if header.get("source_id") != task["source_id"] or header.get("processing_run_id") != run:
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "The candidate does not belong to this task attempt.")
        relative = Path("document-v2")
        layout = header.get("document_artifact_layout")
        if layout == "attempt-v1":
            relative = relative / "artifacts" / fingerprint(run)
        elif layout is not None:
            raise AgentKnowledgeError("SOURCE_NOT_READY", "The published artifact layout is unsupported.")
        graph, graph_hash = self.read(relative / "entity-graph-resolved.json")
        blocks, blocks_hash = self.read(relative / "entity-blocks.json")
        return PublicationPart(
            task=task,
            package=package,
            run=run,
            graph=_graph(graph),
            graph_hash=graph_hash,
            blocks=blocks,
            blocks_hash=blocks_hash,
            assets={},
        )

    def supplements(self, base: PublicationPart) -> Iterable[PublicationPart]:
        repairs = _record(
            _receipt(base["task"]).get("entity_supplements") or {}, "Invalid supplement publication receipt."
        )
        directory = Path("entity-supplements")
        run_ids = {marker.parent.name for marker in self.path(directory).glob("*/run.json")} | set(repairs)
        for run_id in sorted(run_ids):
            if not re.fullmatch("[0-9a-f]{32}", run_id):
                raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid supplement publication identity.")
            relative = directory / run_id
            repair = repairs[run_id] if run_id in repairs else self.read(relative / "run.json")[0]
            row = _record(repair, "Invalid supplement publication receipt.")
            if row.get("state") == "applied":
                yield from self._parts(base, relative, _record(row.get("publication") or {}))

    def _parts(self, base: PublicationPart, relative: Path, receipt: Record) -> Iterable[PublicationPart]:
        if (
            receipt.get("base_graph_sha256") != base["graph_hash"]
            or receipt.get("base_blocks_sha256") != base["blocks_hash"]
            or receipt.get("package_sha256") != base["task"]["package_sha256"]
        ):
            raise AgentKnowledgeError("INTEGRITY_FAILURE", "Supplement source binding changed.")
        for part in records(receipt.get("parts", [])):
            unit_id = text_field(part, "unit_id")
            if not unit_id or Path(unit_id).name != unit_id or unit_id in {".", ".."}:
                raise AgentKnowledgeError("INTEGRITY_FAILURE", "Invalid published artifact location.")
            extra, digest = self.read(relative / unit_id / "candidate.json")
            if fingerprint(extra) != part["candidate_sha256"]:
                raise AgentKnowledgeError("INTEGRITY_FAILURE", "Supplement candidate changed.")
            yield {**base, "graph": _graph(extra), "graph_hash": digest}


def _receipt(task: Record) -> Record:
    return _record(task.get("receipt") or {})


def _merged(parts: list[PublicationPart]) -> Graph:
    def unique(rows: Iterable[Record]) -> list[Record]:
        return list({fingerprint(row): row for row in rows}.values())

    return Graph(
        entities=unique(row for part in parts for row in part["graph"]["entities"]),
        attributes=unique(row for part in parts for row in part["graph"]["attributes"]),
        related=unique(row for part in parts for row in part["graph"]["related"]),
    )


def _catalog_source(source_id: str, package: Record) -> _CatalogSource:
    source = next((row for row in records(package.get("sources", [])) if row.get("source_id") == source_id), {})
    filenames = [
        text_field(row, "original_name") for row in records(package.get("assets", [])) if row.get("original_name")
    ]
    title = text_field(source, "title") if source.get("title") else (filenames[0] if filenames else source_id)
    return _CatalogSource(source_id=source_id, title=title, names=[source_id, title, *filenames])


def _source_name_key(value: str) -> str:
    # Uploaded filenames commonly use underscores where paper titles use spaces.
    return normalize_name(value).replace("_", "")


def _resolve_name(catalog: dict[str, _CatalogSource], name: str) -> str:
    key = _source_name_key(name)
    if not key:
        raise AgentKnowledgeError("INVALID_ARGUMENT", "source_names must contain a non-separator name.")
    exact = [row for row in catalog.values() if key in map(_source_name_key, row["names"])]
    matches = exact or [
        row for row in catalog.values() if any(key in _source_name_key(value) for value in row["names"])
    ]
    if not matches:
        raise AgentKnowledgeError("REFERENCE_NOT_FOUND", f"No published source matches {name}.")
    if len(matches) > 1:
        candidates = "; ".join(f"{row['title']} [{row['source_id']}]" for row in matches)
        raise AgentKnowledgeError("SOURCE_AMBIGUOUS", f"Ambiguous source {name}: {candidates}")
    return matches[0]["source_id"]


class AgentPublications:
    def __init__(self, store: Store, entity_store: EntityStore, workspace_id: str) -> None:
        self.store = store
        self.entity_store = entity_store
        self.workspace_id = workspace_id

    def catalog(self) -> dict[str, _CatalogSource]:
        result = {}
        for task in self.store.list_tasks():
            source_id = text_field(task, "source_id")
            state = self.store.source_state(source_id) or {}
            if (
                task["task_id"] == state.get("published_task_id")
                and task["state"] == "saved"
                and _receipt(task).get("entity_graph")
            ):
                files = _PublicationFiles(self.store, text_field(task, "task_id"))
                result[source_id] = _catalog_source(source_id, files.candidate(task))
        return result

    def scope(self, source_ids: object, cache: PublicationCache, source_names: object = None) -> Scope:
        ids = source_list(source_ids, "source_ids")
        names = source_list(source_names, "source_names")
        if ids is not None and names is not None:
            raise AgentKnowledgeError("INVALID_ARGUMENT", "source_names and source_ids cannot both be supplied.")
        if ids is None and names is None:
            return Scope(source_ids=None, sources=[])
        catalog = self.catalog()
        selected = (
            list(dict.fromkeys(_resolve_name(catalog, name) for name in names)) if names is not None else ids or []
        )
        sources: list[Source] = []
        for source_id in selected:
            if self.store.source_state(source_id) is None:
                raise AgentKnowledgeError("REFERENCE_NOT_FOUND", f"Source {source_id} was not found.")
            self.published(source_id, cache)
            if source_id not in catalog:
                raise AgentKnowledgeError("SOURCE_NOT_READY", "The source has no saved entity publication.")
            sources.append(Source(source_id=source_id, title=catalog[source_id]["title"]))
        return Scope(source_ids=selected, sources=sources)

    def contributions(self, scope: Scope, cache: PublicationCache) -> ContributionView | None:
        ids = scope["source_ids"]
        if ids is None:
            return None
        return ContributionView(self, ids, cache)

    def published(self, source_id: str, cache: PublicationCache, task_id: str | None = None) -> Publication:
        state: Record = self.store.source_state(source_id) or {}
        published_id = state.get("published_task_id")
        if task_id is not None and task_id != published_id:
            raise AgentKnowledgeError("SOURCE_CHANGED", "This source has changed; read the current entity again.")
        if not isinstance(published_id, str) or not published_id:
            raise AgentKnowledgeError("SOURCE_NOT_READY", "The source has no published entity task.")
        # All projections in this request must refer to one generation of each source.
        if source_id in cache:
            return cache[source_id]
        try:
            task = self.store.get_task(published_id)
        except KeyError as exc:
            raise AgentKnowledgeError("REFERENCE_NOT_FOUND", "The published task is unavailable.") from exc
        if task["source_id"] != source_id or task["state"] != "saved" or not _receipt(task).get("entity_graph"):
            raise AgentKnowledgeError("SOURCE_NOT_READY", "The source is not a saved entity publication.")
        files = _PublicationFiles(self.store, published_id)
        base = files.original(task)
        parts = [base, *files.supplements(base)]
        result: Publication = {**base, "graph": _merged(parts), "publications": parts}
        cache[source_id] = result
        return result

    def optional(self, source_id: object, cache: PublicationCache, warnings: list[str]) -> Publication | None:
        if not isinstance(source_id, str) or not source_id:
            return None
        try:
            return self.published(source_id, cache)
        except AgentKnowledgeError as exc:
            warnings.append(f"{exc.code}: Some source content is unavailable or not published.")
            return None


def _places(places: Iterable[Record], source_id: str, selected: set[str]) -> list[Record]:
    result: list[Record] = []
    for place in places:
        owner = place.get("source_id") or source_id
        if isinstance(owner, str) and owner in selected:
            result.append({**place, "source_id": owner})
    return result


class ContributionView:
    """Read-only store facade built solely from the selected published graph contributions."""

    def __init__(self, service: AgentPublications, source_ids: list[str], cache: PublicationCache) -> None:
        self.rows: dict[str, _Entity] = {}
        self.attributes: list[Record] = []
        self.links: list[Record] = []
        self.workspace_id = service.workspace_id
        selected = set(source_ids)
        for source_id in source_ids:
            graph = service.published(source_id, cache)["graph"]
            for head in graph["entities"]:
                if service.entity_store.entity(self.workspace_id, head["entity_id"]) is not None:
                    self._head(head, source_id)
            for attribute in graph["attributes"]:
                self._attribute(attribute, source_id, selected)
            for link in graph["related"]:
                places = _places(records(link.get("locations", [])), source_id, selected)
                if places:
                    self.links.append({**link, "locations": places})
        self.attributes = [row for row in self.attributes if row["entity_id"] in self.rows]
        for row in self.attributes:
            row["refs"] = [ref for ref in string_list(row.get("refs", [])) if ref in self.rows]
        self.links = [
            row for row in self.links if row.get("entity_a") in self.rows and row.get("entity_b") in self.rows
        ]
        for entity in self.rows.values():
            entity["source_ids"].sort()

    def _head(self, head: Record, source_id: str) -> None:
        entity_id = text_field(head, "entity_id")
        name = text_field(head, "name")
        row = self.rows.setdefault(
            entity_id,
            _Entity(
                entity_id=entity_id,
                type=text_field(head, "type"),
                name=name,
                aliases=[],
                source_id=source_id,
                source_ids=[],
            ),
        )
        if source_id not in row["source_ids"]:
            row["source_ids"].append(source_id)
        for alias in [name, *string_list(head.get("add_aliases", []))]:
            if alias != row["name"] and alias not in row["aliases"]:
                row["aliases"].append(alias)

    def _attribute(self, attribute: Record, source_id: str, selected: set[str]) -> None:
        places = _places([attribute, *records(attribute.get("citations", []))], source_id, selected)
        if places:
            first, *citations = places
            fields = ("asset_ref", "page", "block_id", "block_ids", "quote", "highlight_regions", "source_id")
            self.attributes.append(
                {
                    **attribute,
                    **{key: first.get(key) for key in fields},
                    "attribute_id": fingerprint([source_id, attribute]),
                    "citations": citations,
                }
            )

    def attributes_of(self, workspace_id: str, entity_id: object) -> list[Record]:
        return [row for row in self.attributes if workspace_id == self.workspace_id and row["entity_id"] == entity_id]

    def related_to(self, workspace_id: str, entity_id: object, entity_type: str | None = None) -> list[Record]:
        neighbors: dict[str, list[Record]] = {}
        for link in self.links:
            a, b = text_field(link, "entity_a"), text_field(link, "entity_b")
            if entity_id not in (a, b):
                continue
            other = b if a == entity_id else a
            if not entity_type or self.rows[other]["type"] == entity_type:
                neighbors.setdefault(other, []).extend(records(link["locations"]))
        return [{**self.rows[other], "locations": locations} for other, locations in neighbors.items()]

    def read_entity(self, workspace_id: str, entity_id: object) -> Record | None:
        row = self.rows.get(entity_id) if isinstance(entity_id, str) else None
        if row is None:
            return None
        return {
            **row,
            "attributes": self.attributes_of(workspace_id, entity_id),
            "related": self.related_to(workspace_id, entity_id),
        }
