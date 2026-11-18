"""Independent source snapshots and deterministic analyses; no scientific gate bypass."""

from kb_ingest.core import StateConflict, clock
from kb_ingest.storage.file_time import utc_text
from kb_ingest.storage.time_source import sourced_epoch

from .raw_table_snapshot import build_raw_snapshot
from .raw_table_store import RawTableArtifactStore, analysis_version, newest_artifacts_first


def _request(payload, allowed, required):
    if not isinstance(payload, dict) or set(payload) - set(allowed) or not set(required) <= set(payload):
        raise ValueError("Invalid raw-table request")
    if payload.get("reviewed") is not True:
        raise ValueError("请明确确认当前表格或分析设置")


class RawTableWorkflowMixin:
    def _raw_artifacts(self):
        return RawTableArtifactStore(self.store)

    def save_raw_table(self, source_id, payload):
        _request(
            payload,
            {"expected_revision", "region_id", "reviewed", "operator"},
            {"expected_revision", "region_id", "reviewed"},
        )
        if type(payload["expected_revision"]) is not int or not isinstance(payload["region_id"], str):
            raise ValueError("Expected revision and region identity are required")
        operator = payload.get("operator", "本地使用者")
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 200:
            raise ValueError("Invalid operator")
        artifacts = self._raw_artifacts()
        with self.store.lock("source:" + source_id):
            draft = self.store.get_table_draft(source_id)
            if draft["revision"] != payload["expected_revision"]:
                raise StateConflict("范围已修改，请载入最新版本后保存")
            if draft["recipe"].get("entry_profile") != "table_regions_v1":
                raise ValueError("原表保存需要表格范围草稿")
            region = next(
                (r for r in draft["recipe"]["region_review"]["regions"] if r["id"] == payload["region_id"]), None
            )
            if region is None:
                raise ValueError("原表范围不存在")
            asset = self._table_assets([region["asset_id"]])[0]
            if asset["sha256"] != draft["asset_hashes"].get(asset["asset_id"]):
                raise StateConflict("原件版本已变化")
            table = self._original_region_sheet(draft, [asset], asset["asset_id"], region["sheet"])
            snapshot = build_raw_snapshot(draft, asset, region, table)
            generation = self.store.source_state(source_id).get("visibility_generation", 0)
            if generation:
                from .raw_table_snapshot import snapshot_version

                snapshot["reparse_generation"] = generation
                snapshot["table_version_id"] = snapshot_version(snapshot)
            identifier = snapshot["table_version_id"]
            with self.store.lock("table-artifact:" + identifier):
                artifacts.put(
                    "raw",
                    identifier,
                    snapshot,
                    {
                        "reviewed": True,
                        "operator": operator,
                        "draft_revision": draft["revision"],
                        "draft_sha256": draft["draft_sha256"],
                    },
                )
        self._sync_table_artifact("raw", identifier)
        return self.get_raw_table(identifier)

    def _sync_table_artifact(self, kind, identifier):
        envelope = self._raw_artifacts().load(kind, identifier)
        with self.store.lock("source:" + envelope["payload"]["source_id"]):
            return self._sync_source_artifact(kind, identifier)

    def _sync_source_artifact(self, kind, identifier):
        artifacts = self._raw_artifacts()
        with self.store.lock("table-artifact:" + identifier):
            envelope = artifacts.load(kind, identifier)
            current = artifacts.publication(kind, identifier)
            if self.archive is None or current["status"] == "archive_verified":
                return current
            pending = artifacts.path(kind, identifier) / "publication"
            state: dict[str, object]
            try:
                payload = envelope["payload"]
                registered = self.store.get_asset(payload["asset_id"])
                self.store.asset_bytes(payload["asset_id"])
                if registered["sha256"] != payload["asset_sha256"]:
                    raise StateConflict("原件身份与已保存原表不一致")
                previous = self.archive.publication_state(pending)
                if previous["outcome"] in {"accepted", "unknown", "verified"} or current["status"] == "write_unknown":
                    receipt = self.archive.reconcile_pending(envelope, pending)
                    if receipt is None:
                        state = {
                            "status": "sync_failed",
                            "receipt": None,
                            "error": "已确认旧写入未提交，可再次重试同步",
                        }
                    else:
                        state = {"status": "archive_verified", "receipt": receipt, "error": None}
                else:
                    receipt = self.archive.publish(
                        envelope, {payload["asset_id"]: self.store.asset_path(payload["asset_id"])}, pending
                    )
                    state = {"status": "archive_verified", "receipt": receipt, "error": None}
            except Exception as exc:
                try:
                    outcome = self.archive.publication_state(pending)["outcome"]
                except Exception:
                    outcome = "unknown"
                state = {
                    "status": "sync_failed" if outcome == "not_written" else "write_unknown",
                    "receipt": current.get("receipt"),
                    "error": "本地已保存；持久归档失败，可重试"
                    if outcome == "not_written"
                    else "本地已保存；归档写入状态待核对，重试将只核对原写入",
                    "error_type": type(exc).__name__,
                }
            state["updated_at"] = clock.now()
            artifacts.set_publication(kind, identifier, state)
            return state

    def _artifact_status(self, kind, identifier):
        publication = self._raw_artifacts().publication(kind, identifier)
        if "updated_at" in publication:
            publication["updated_at"] = sourced_epoch(publication["updated_at"])
        return {
            "status": publication["status"],
            "local_saved": True,
            "archive_verified": publication["status"] == "archive_verified",
            "publication": publication,
        }

    def _analysis_summaries(self, table_version_id):
        artifacts = self._raw_artifacts()
        result = []
        for identifier in artifacts.identifiers("analyses"):
            analysis = artifacts.load("analyses", identifier)["payload"]
            if analysis["table_version_id"] == table_version_id:
                result.append(
                    {
                        "analysis_id": identifier,
                        "config": analysis["config"],
                        "created_at": analysis["created_at"],
                        **self._artifact_status("analyses", identifier),
                    }
                )
        return newest_artifacts_first(result, "analysis_id")

    def get_raw_table(self, table_version_id):
        snapshot = self._raw_artifacts().load("raw", table_version_id)["payload"]
        from .raw_table_analysis import ENGINE_VERSION, recommend_analysis

        try:
            recommendations = recommend_analysis(snapshot)
        except ValueError as exc:
            # An analysis capability limit cannot revoke a complete raw snapshot.
            recommendations = {
                "engine_version": ENGINE_VERSION,
                "requires_confirmation": True,
                "status": "skipped",
                "reason": str(exc),
                "columns": [],
                "config": {
                    "y_columns": [],
                    "x_column": None,
                    "group_columns": [],
                    "units": {},
                    "header_row_count": snapshot["default_header_row_count"],
                    "line_plot": False,
                    "number_format": "source",
                },
            }
        return {
            "table_id": snapshot["table_id"],
            "table_version_id": table_version_id,
            "snapshot": snapshot,
            **self._artifact_status("raw", table_version_id),
            "recommendations": recommendations,
            "analyses": self._analysis_summaries(table_version_id),
        }

    def list_raw_tables(self, source_id):
        self.store.get_table_draft(source_id)
        artifacts = self._raw_artifacts()
        tables = []
        for identifier in artifacts.identifiers("raw"):
            snapshot = artifacts.load("raw", identifier)["payload"]
            if snapshot["source_id"] != source_id:
                continue
            tables.append(
                {
                    key: snapshot[key]
                    for key in (
                        "table_id",
                        "table_version_id",
                        "source_id",
                        "source_region_id",
                        "name",
                        "sheet",
                        "table_range",
                        "original_name",
                        "asset_sha256",
                        "row_count",
                        "column_count",
                    )
                }
            )
            tables[-1].update(
                **self._artifact_status("raw", identifier),
                created_at=artifacts.audit("raw", identifier)["created_at"],
                analysis_count=len(self._analysis_summaries(identifier)),
            )
        return {"tables": newest_artifacts_first(tables, "table_version_id")}

    def retry_raw_table_publication(self, table_version_id):
        self._sync_table_artifact("raw", table_version_id)
        return self.get_raw_table(table_version_id)

    def analyze_raw_table(self, table_version_id, payload):
        snapshot = self._raw_artifacts().load("raw", table_version_id)["payload"]
        with self.store.lock("source:" + snapshot["source_id"]):
            return self._analyze_source_table(table_version_id, payload)

    def _analyze_source_table(self, table_version_id, payload):
        from .raw_table_analysis import ENGINE_VERSION, analyze_table, normalize_analysis_config

        _request(payload, {"config", "reviewed"}, {"config", "reviewed"})
        artifacts = self._raw_artifacts()
        snapshot = artifacts.load("raw", table_version_id)["payload"]
        config = normalize_analysis_config(snapshot, payload["config"])
        identity = {
            "version": "table-analysis-v1",
            "table_version_id": table_version_id,
            "engine_version": ENGINE_VERSION,
            "config": config,
        }
        identifier = analysis_version(identity)
        with self.store.lock("table-artifact:" + identifier):
            try:
                artifacts.load("analyses", identifier)
            except KeyError:
                result = analyze_table(snapshot, config)
                analysis = {
                    **identity,
                    "analysis_id": identifier,
                    "table_id": snapshot["table_id"],
                    "source_id": snapshot["source_id"],
                    "asset_id": snapshot["asset_id"],
                    "asset_sha256": snapshot["asset_sha256"],
                    "original_name": snapshot["original_name"],
                    "created_at": utc_text(clock.now()),
                    "result": result,
                }
                artifacts.put("analyses", identifier, analysis, {"reviewed": True})
        self._sync_source_artifact("analyses", identifier)
        return self.get_raw_table_analysis(identifier)

    def get_raw_table_analysis(self, analysis_id):
        analysis = self._raw_artifacts().load("analyses", analysis_id)["payload"]
        return {
            "analysis_id": analysis_id,
            "table_id": analysis["table_id"],
            "table_version_id": analysis["table_version_id"],
            "analysis": {**analysis, "created_at": sourced_epoch(analysis["created_at"])},
            **self._artifact_status("analyses", analysis_id),
        }

    def retry_analysis_publication(self, analysis_id):
        self._sync_table_artifact("analyses", analysis_id)
        return self.get_raw_table_analysis(analysis_id)
