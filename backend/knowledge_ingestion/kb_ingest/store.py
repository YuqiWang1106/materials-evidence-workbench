"""Durable coordination with atomic identities, attempt fencing and original bytes."""

from __future__ import annotations

import fcntl
import json
import re
import time
import uuid
from contextlib import contextmanager, nullcontext
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.orm import Session

from .core import errors, fingerprint
from .core.clock import IO_DEADLINE, remaining
from .core.errors import raise_state_conflict as conflict
from .core.serialization import canonical
from .core.workspace import data_root_path
from .document_progress import extracted_message
from .storage import asset_repository as assets
from .storage import reading_repository as reading
from .storage import revision_check as _revisions
from .storage import source_repository as sources
from .storage import task_files
from .storage import task_repository as tasks
from .storage.asset_uploads import Uploads
from .storage.engine import coordination_engine
from .storage.migrations import initialize_workspace
from .storage.models import ReusableRecipe, SourceConfirmation
from .storage.transactions import begin_write


class Store:
    def __init__(self, root: Path, *, workspace_id: str | None = None):
        self.data_root = data_root_path(Path(root))
        with _revisions.locked_database(self.data_root, write=True):
            self._initialize(workspace_id)
        self._uploads().recover()

    def _initialize(self, workspace_id: str | None) -> None:
        (self.data_root / "locks").mkdir(exist_ok=True, mode=0o700)
        self.engine = coordination_engine(self.data_root)
        with self.lock("migration"), self.engine.connect() as connection:
            self.workspace_id, self.root = initialize_workspace(self.data_root, workspace_id, connection)

    @contextmanager
    def lock(self, name: str):
        root = self.data_root if name in {"migration", "knowledge-ledger"} else self.root
        path = root / "locks" / (sha256(name.encode()).hexdigest() + ".lock")
        with path.open("a+b") as stream:
            if IO_DEADLINE.get() is None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            else:
                while True:
                    remaining()
                    try:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(remaining(0.02))
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def try_lock(self, name):
        root = self.data_root if name in {"migration", "knowledge-ledger"} else self.root
        path = root / "locks" / (sha256(name.encode()).hexdigest() + ".lock")
        with path.open("a+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def require_source(self, source_id, *, generation=None, session=None):
        from .source_lifecycle import require_source

        require_source(self, source_id, generation=generation, session=session)

    @contextmanager
    def _write(self):
        with begin_write(self.engine) as session:
            yield session

    @staticmethod
    def _row(row) -> dict:
        result = {column.name: getattr(row, column.name) for column in row.__table__.columns}
        if row.__tablename__ in {
            "assets",
            "sources",
            "tasks",
            "reading_versions",
            "table_drafts",
            "source_operations",
            "package_revisions",
        }:
            result.pop("workspace_id", None)
        if isinstance(row, SourceConfirmation):
            result["confirmation_version_id"] = row.version_id
        elif isinstance(row, ReusableRecipe):
            result["recipe_version_id"] = row.version_id
        return result

    def _uploads(self) -> Uploads:
        return Uploads(self.data_root, self.root, self.workspace_id, self.engine, self._write)

    def register_stream(self, stream: BinaryIO, original_name: str) -> dict:
        return self._uploads().register_stream(stream, original_name)

    def register_bytes(self, content: bytes, original_name: str) -> dict:
        return self.register_stream(BytesIO(content), original_name)

    def get_asset(self, asset_id: str) -> dict:
        with Session(self.engine) as session:
            row = assets.get_asset(session, self.workspace_id, asset_id)
            if row is None:
                raise KeyError("Asset not found")
            return self._row(row)

    def require_asset_access(self, asset_id: str) -> None:
        with Session(self.engine) as session:
            assets.require_asset_access(session, self.workspace_id, asset_id)

    def asset_path(self, asset_id: str) -> Path:
        row = self.get_asset(asset_id)
        return self._original_path(row["storage_name"])

    def _original_path(self, storage_name: str) -> Path:
        path = self.root / "originals" / storage_name
        if path.parent.resolve() != self.root / "originals" or path.is_symlink():
            conflict("Invalid registered asset path")
        return path

    def asset_bytes(self, asset_id: str) -> bytes:
        row = self.get_asset(asset_id)
        return self._original_bytes(row["storage_name"], row["sha256"])

    def _original_bytes(self, storage_name: str, digest: str) -> bytes:
        try:
            content = self._original_path(storage_name).read_bytes()
        except OSError as exc:
            message = "Registered original asset is unavailable"
            raise errors.StateConflict(message) from exc
        if sha256(content).hexdigest() != digest:
            conflict("Original asset integrity check failed")
        return content

    def reserve_revision(self, package: dict):
        """Same scientific revision can be retried but never mean different bytes."""
        source_id = package["header"]["source_id"]
        revision = package["header"]["record_revision_id"]
        digest = fingerprint(package)
        with self._write() as session:
            sources.reserve_revision(
                session,
                self.workspace_id,
                (source_id, revision),
                digest,
                "Scientific revision already names different content; create a new revision",
            )

    def source_state(self, source_id: str) -> dict | None:
        with Session(self.engine) as session:
            row = sources.get_source(session, self.workspace_id, source_id)
            return self._row(row) if row else None

    def submit(
        self,
        source_id: str,
        source_type: str,
        asset_ids: list[str],
        options: dict,
        expected_revision: int | None = None,
    ) -> dict:
        if options.get("table_mode") == "confirmed" or "confirmation_version_id" in options:
            conflict("Confirmed tasks must be created by confirming a current draft")
        if not re.fullmatch(r"[\w.-]{1,100}", source_id, flags=re.UNICODE):
            raise ValueError("Source ID must contain letters, numbers, dot, dash or underscore")
        if source_type not in ("paper", "patent", "experimental", "prepared"):
            raise ValueError("Unsupported source type")
        if not asset_ids or len(set(asset_ids)) != len(asset_ids):
            raise ValueError("Non-empty unique assets required")
        key = fingerprint([source_id, source_type, sorted(asset_ids), options, expected_revision])
        with self.lock("source:" + source_id), self._write() as session:
            return self._submit_in_session(session, source_id, source_type, asset_ids, options, key, expected_revision)

    def _submit_in_session(
        self,
        session,
        source_id,
        source_type,
        asset_ids,
        options,
        key,
        expected_revision=None,
        confirmation_version_id=None,
    ):
        """Shared transaction body; caller owns the source lock and write session."""
        self.require_source(source_id, session=session)
        submission = tasks.Submission(
            source_id, source_type, asset_ids, options, key, expected_revision, confirmation_version_id
        )
        return self._row(tasks.submit(session, self.workspace_id, submission))

    @staticmethod
    def _reading_data(value, label):
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return json.loads(canonical(value))

    def create_document_review_revision(self, parent_id, key, prepare):
        """Caller holds source lock; files must be ready before a review task is visible.

        A failed copy rolls back the complete DB transaction. Any unregistered
        files are inert: there is never a queued task for the extractor to claim.
        """
        with self._write() as session:
            existing = tasks.by_key(session, self.workspace_id, key)
            if existing:
                return self._row(existing)
            task = tasks.review_revision(session, self.workspace_id, parent_id, key)
            package = prepare(self._row(task))
            digest = fingerprint(package)
            tasks.review_candidate(session, self.workspace_id, task, package["header"]["record_revision_id"], digest)
            return self._row(task)

    @staticmethod
    def _required_text(value, label, limit=500):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{label} must be non-empty text of at most {limit} characters")
        return value.strip()

    def _asset_hashes(self, session, asset_ids):
        if (
            not isinstance(asset_ids, list)
            or not asset_ids
            or not all(isinstance(a, str) for a in asset_ids)
            or len(set(asset_ids)) != len(asset_ids)
        ):
            raise ValueError("Non-empty unique assets required")
        rows = assets.require_assets(session, self.workspace_id, asset_ids)
        for row in rows.values():
            self._original_bytes(row.storage_name, row.sha256)
        return {aid: row.sha256 for aid, row in rows.items()}

    def _refresh_draft_hash(self, row):
        reading.refresh_draft_hash(self.workspace_id, row, fingerprint)

    def _draft_result(self, session, row):
        result = self._row(row)
        result["expected_generation"] = sources.require_registered(session, self.workspace_id, row.source_id).generation
        return result

    def create_table_draft(self, payload: dict) -> dict:
        payload = self._reading_data(payload, "Draft")
        source_id = payload.get("source_id") or "source-" + uuid.uuid4().hex
        if not isinstance(source_id, str) or not re.fullmatch(r"[\w.-]{1,100}", source_id):
            raise ValueError("Invalid source ID")
        title = self._required_text(payload.get("title"), "Title")
        recipe = self._reading_data(payload.get("recipe", {}), "Recipe")
        options = self._reading_data(payload.get("reader_options", {}), "Reader options")
        with self.lock("source:" + source_id), self._write() as session:
            self.require_source(source_id, session=session)
            if reading.get_draft(session, self.workspace_id, source_id):
                conflict("Draft already exists; update with its expected revision")
            ids = payload.get("asset_ids", [])
            hashes = self._asset_hashes(session, ids)
            row = reading.new_draft(
                session,
                self.workspace_id,
                source_id,
                reading.DraftData(title, ids, hashes, recipe, options),
                fingerprint,
            )
            return self._draft_result(session, row)

    def get_table_draft(self, source_id: str) -> dict:
        self.require_source(source_id)
        with Session(self.engine) as session:
            row = reading.get_draft(session, self.workspace_id, source_id)
            if row is None:
                raise KeyError("Table draft not found")
            return self._draft_result(session, row)

    def list_table_drafts(self) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {**self._row(row), "expected_generation": generation}
                for row, generation in reading.list_drafts(session, self.workspace_id)
            ]

    def _update_draft_in_session(self, session, source_id, payload, knowledge=None):
        self.require_source(source_id, session=session)
        row = reading.get_draft(session, self.workspace_id, source_id)
        if row is None:
            raise KeyError("Table draft not found")
        expected = payload.get("expected_revision")
        if type(expected) is not int or expected != row.revision:
            conflict("Draft revision changed; reload before updating")
        changes = reading.DraftChanges()
        if "title" in payload:
            changes.title = self._required_text(payload["title"], "Title")
        if "asset_ids" in payload:
            changes.originals = (payload["asset_ids"], self._asset_hashes(session, payload["asset_ids"]))
        if "recipe" in payload:
            changes.recipe = self._reading_data(payload["recipe"], "Recipe")
        if "reader_options" in payload:
            changes.options = self._reading_data(payload["reader_options"], "Reader options")
        reading.update_draft(session, self.workspace_id, row, changes, fingerprint)
        if knowledge is not None:
            knowledge.invalidate_table_in_session(session, row)
        return self._draft_result(session, row)

    def update_table_draft(self, source_id: str, payload: dict) -> dict:
        payload = self._reading_data(payload, "Draft update")
        from .knowledge_store import KnowledgeStore

        knowledge = KnowledgeStore(self, self.workspace_id, initialize=False)
        with self.lock("source:" + source_id), knowledge._write(require_ledger=False) as session:
            return self._update_draft_in_session(session, source_id, payload, knowledge)

    def confirm_table_draft(self, source_id: str, payload: dict) -> dict:
        payload = self._reading_data(payload, "Confirmation")
        operator = self._required_text(payload.get("operator"), "Operator", 200)
        pipeline = self._required_text(payload.get("_pipeline_fingerprint"), "Pipeline fingerprint")
        with self.lock("source:" + source_id), self._write() as session:
            draft = reading.get_draft(session, self.workspace_id, source_id)
            if draft is None:
                raise KeyError("Table draft not found")
            expected = payload.get("expected_revision")
            if type(expected) is not int or expected != draft.revision:
                conflict("Draft revision changed; reload before confirming")
            prior = reading.draft_confirmation(session, self.workspace_id, source_id, expected)
            if prior:
                task = tasks.by_confirmation(session, self.workspace_id, prior.confirmation_version_id)
                if task is None:
                    conflict("Confirmation has no task; restore the ledger from backup")
                if draft.recipe.get("version") == "2" and (
                    task.options.get("_pipeline_fingerprint") != pipeline
                    or prior.structure != payload.get("_execution_manifest")
                ):
                    conflict("处理规则已更新，请保存新版草稿后重新核对 / new draft revision required")
                return self._row(task)
            generation = payload.get("expected_generation")
            source = sources.require_registered(session, self.workspace_id, source_id)
            if type(generation) is not int or generation != source.generation:
                conflict("Source generation changed; reload before confirming")
            hashes = self._asset_hashes(session, draft.asset_ids)
            if hashes != draft.asset_hashes:
                conflict("Draft original asset hashes changed; reload and review")
            plan_version = draft.recipe.get("version") == "2"
            manifest = payload.get("_execution_manifest")
            if plan_version and (
                not isinstance(manifest, dict)
                or not manifest.get("plan_hash")
                or not draft.recipe.get("reading_plan", {}).get("fields")
            ):
                raise ValueError("A versioned plan requires a reviewed execution manifest")
            if not plan_version and (
                not isinstance(draft.recipe.get("profiles"), list) or not draft.recipe["profiles"]
            ):
                raise ValueError("A non-empty reading recipe must be reviewed before confirming")
            confirmation = reading.Confirmation(
                operator, generation, manifest if isinstance(manifest, dict) and plan_version else {}
            )
            version_id = reading.confirm(session, self.workspace_id, draft, confirmation)
            options = {
                "title": draft.title,
                "table_mode": "confirmed",
                "table_profiles": draft.recipe["profiles"],
                "reader_options": draft.reader_options,
                "auto_detect": False,
                "_pipeline_fingerprint": pipeline,
                "confirmation_version_id": version_id,
                "asset_hashes": hashes,
                "draft_revision": draft.revision,
                "draft_sha256": draft.draft_sha256,
                "recipe_sha256": draft.recipe_sha256,
            }
            if plan_version:
                options.update(reading_plan=draft.recipe["reading_plan"], _execution_manifest=manifest)
            key = fingerprint(["confirmed-table", source_id, draft.revision])
            return self._submit_in_session(
                session, source_id, "experimental", draft.asset_ids, options, key, generation, version_id
            )

    def get_confirmation(self, confirmation_version_id: str) -> dict:
        with Session(self.engine) as session:
            row = reading.get_confirmation(session, self.workspace_id, confirmation_version_id)
            if row is None:
                raise KeyError("Reading confirmation not found")
            return self._row(row)

    @staticmethod
    def _portable_reading(value):
        """Remove source-specific identities; structural selectors remain data."""
        if isinstance(value, list):
            return [Store._portable_reading(v) for v in value]
        if isinstance(value, dict):
            return {
                k: Store._portable_reading(v)
                for k, v in value.items()
                if k
                not in {
                    "asset_id",
                    "asset_ids",
                    "asset_hashes",
                    "source_id",
                    "confirmation_version_id",
                    "package_sha256",
                }
                and not k.startswith("asset-")
            }
        return value

    @staticmethod
    def _reject_recipe_facts(value):
        """Reusable mappings cannot contain source measurements or assertions.

        Layout and coordinate validation belongs to the reading service. This
        guard also protects the internal recipe-write boundary from fact blobs.
        """
        if isinstance(value, list):
            for item in value:
                Store._reject_recipe_facts(item)
        elif isinstance(value, dict):
            if {"value", "values", "raw_value", "assertions", "measurements", "scientific_values"} & value.keys():
                raise ValueError("Reusable recipes cannot contain scientific values or assertions")
            for item in value.values():
                Store._reject_recipe_facts(item)

    def create_table_recipe(self, payload: dict) -> dict:
        payload = self._reading_data(payload, "Reusable recipe")
        name = self._required_text(payload.get("name"), "Recipe name", 200)
        operator = self._required_text(payload.get("operator", "unspecified"), "Operator", 200)
        recipe = self._portable_reading(self._reading_data(payload.get("recipe", {}), "Recipe"))
        self._reject_recipe_facts(recipe)
        options = self._portable_reading(self._reading_data(payload.get("reader_options", {}), "Reader options"))
        structure = self._portable_reading(self._reading_data(payload.get("structure", {}), "Structure"))
        with self._write() as session:
            data = reading.RecipeData(name, operator, recipe, options, structure, fingerprint(recipe))
            row = reading.create_recipe(session, self.workspace_id, data)
            return self._row(row)

    def get_table_recipe(self, recipe_version_id: str) -> dict:
        with Session(self.engine) as session:
            row = reading.get_recipe(session, self.workspace_id, recipe_version_id)
            if row is None:
                raise KeyError("Reusable recipe not found")
            return self._row(row)

    def list_table_recipes(self) -> list[dict]:
        with Session(self.engine) as session:
            return [self._row(row) for row in reading.list_recipes(session, self.workspace_id)]

    def reuse_table_recipe(self, source_id: str, payload: dict) -> dict:
        payload = self._reading_data(payload, "Recipe reuse")
        from .knowledge_store import KnowledgeStore

        knowledge = KnowledgeStore(self, self.workspace_id, initialize=False)
        with self.lock("source:" + source_id), knowledge._write(require_ledger=False) as session:
            named = reading.get_recipe(session, self.workspace_id, payload.get("recipe_version_id", ""))
            if named is None:
                raise KeyError("Reusable recipe not found")
            return self._update_draft_in_session(
                session,
                source_id,
                {
                    "expected_revision": payload.get("expected_revision"),
                    "recipe": named.recipe,
                    "reader_options": named.reader_options,
                },
                knowledge,
            )

    def get_task(self, task_id: str, *, include_unavailable=False) -> dict:
        with Session(self.engine) as session:
            row = tasks.get_task(session, self.workspace_id, task_id)
            if row is None:
                raise KeyError("Task not found")
            if not include_unavailable:
                self.require_source(row.source_id, generation=row.generation, session=session)
            return self._row(row)

    def list_tasks(self) -> list[dict]:
        with Session(self.engine) as session:
            return [self._row(row) for row in tasks.list_tasks(session, self.workspace_id, published=False)]

    def list_published_tasks(self) -> list[dict]:
        """Read citation targets in one query, without a source lookup for every document."""
        with Session(self.engine) as session:
            return [self._row(row) for row in tasks.list_tasks(session, self.workspace_id, published=True)]

    def claim(self, task_id: str, lease_seconds: int = 900) -> dict | None:
        with self._write() as session:
            row = tasks.claim(session, self.workspace_id, task_id, lease_seconds)
            return self._row(row) if row else None

    def transition(self, task_id: str, attempt_token: str, state: str, progress: str = "", error=None):
        with self._write() as session:
            tasks.transition(
                session, self.workspace_id, task_id, tasks.Transition(attempt_token, state, progress, error)
            )

    def heartbeat(self, task_id: str, attempt_token: str):
        with self._write() as session:
            tasks.heartbeat(session, self.workspace_id, task_id, attempt_token)

    def cancel(self, task_id: str):
        with self._write() as session:
            tasks.cancel(session, self.workspace_id, task_id)

    def retry(self, task_id: str):
        with self._write() as session:
            tasks.retry(session, self.workspace_id, task_id)

    def pending_path(self, task_id: str, *parts: str | Path, include_unavailable: bool = False) -> Path:
        self.get_task(task_id, include_unavailable=include_unavailable)
        return task_files.checked_path(self.data_root, self.root, task_id, parts)

    def pending_dir(self, task_id: str, *, include_unavailable=False) -> Path:
        path = self.pending_path(task_id, include_unavailable=include_unavailable)
        with task_files.directory(self.data_root, path):
            return path

    def save_candidate(self, task_id: str, attempt_token: str, package: dict, *, failed=False):
        with self._write() as session:
            row = tasks.working_attempt(
                session, self.workspace_id, task_id, attempt_token, "Late attempt cannot replace a candidate"
            )
            if package["header"]["source_id"] != row.source_id:
                conflict("Candidate source differs from task source")
            identity = (row.source_id, package["header"]["record_revision_id"])
            digest = fingerprint(package)
            sources.reserve_revision(
                session, self.workspace_id, identity, digest, "Scientific revision cannot be overwritten"
            )
            attempt = self.pending_path(task_id, "attempts", attempt_token, "candidate.json")
            current = self.pending_path(task_id, "candidate.json")
            data = (canonical(package) + "\n").encode("utf-8")
            task_files.atomic_write(self.data_root, attempt, data)
            task_files.atomic_write(self.data_root, current, data)
            graph = package["header"].get("entity_graph")
            if graph:
                message = extracted_message(graph)
            else:
                message = (
                    "记录与证据已生成，等待核验入库"
                    if package["assertions"]
                    else "原文证据已读取，科学内容抽取尚未完成"
                )
            if failed:
                message = "自动处理未完成，原件与问题已保留"
            tasks.finish_candidate(self.workspace_id, row, digest, failed, message)

    def record_study_unit_sync(self, task_id, attempt_token, *, error_code):
        with self._write() as session:
            tasks.record_study_unit_sync(session, self.workspace_id, task_id, attempt_token, error_code)

    def begin_publish(self, task_id: str) -> dict:
        with self._write() as session:
            return self._row(tasks.begin_publish(session, self.workspace_id, task_id))

    def finish_publish(
        self, task_id: str, receipt: dict, *, make_current: bool = True, progress: str | None = None, session=None
    ):
        """Finish publication; an injected write session owns the complete graph/receipt transaction."""
        with self._write() if session is None else nullcontext(session) as session:
            tasks.finish_publish(
                session, self.workspace_id, task_id, tasks.Publication(receipt, make_current, progress)
            )

    def publication_failed(self, task_id: str, error: str, unknown: bool = True):
        with self._write() as session:
            tasks.publication_failed(session, self.workspace_id, task_id, error, unknown)
