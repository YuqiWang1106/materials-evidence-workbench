"""Atomic all-table consent outbox; model execution remains in independent jobs."""

from __future__ import annotations

import json
import re
import time

from .core import StateConflict, atomic_json, fingerprint


class StandardModelBatchesMixin:
    def _standard_batches_root(self):
        return self.store.root / "standard_batches"

    def _standard_batch_path(self, batch_id):
        if not isinstance(batch_id, str) or not re.fullmatch(r"stb-[0-9a-f]{32}", batch_id):
            raise ValueError("无效的表格识别批次编号")
        path = self._standard_batches_root() / batch_id / "batch.json"
        if not path.is_file():
            raise KeyError("表格识别批次不存在")
        batch = json.loads(path.read_text())
        if batch.get("batch_id") != batch_id:
            raise StateConflict("批次文件与编号不匹配")
        return path, batch

    @staticmethod
    def _validate_standard_batch_request(payload):
        if not isinstance(payload, dict) or set(payload) != {
            "expected_revision",
            "expected_version",
            "confirmed",
            "items",
        }:
            raise ValueError("批次需要版本、明确确认与全部表格发送项")
        for field, minimum in (("expected_revision", 1), ("expected_version", 0)):
            if type(payload[field]) is not int or payload[field] < minimum:
                raise ValueError("批次版本必须为有效整数")
        if payload["confirmed"] is not True:
            raise StateConflict("请先逐表核对发送内容，再确认发送全部表格")
        if not isinstance(payload["items"], list) or not payload["items"]:
            raise ValueError("必须包含全部已确认表格")
        for item in payload["items"]:
            if not isinstance(item, dict) or set(item) - {
                "region_id",
                "payload_sha256",
                "sample_rows",
                "sample_range",
                "confirmed",
            }:
                raise ValueError("无效的表格发送项")
            if not isinstance(item.get("region_id"), str) or not item["region_id"] or len(item["region_id"]) > 100:
                raise ValueError("每个发送项必须提供有效表格编号")
            if item.get("confirmed") is not True:
                raise StateConflict("每张表都必须明确确认发送内容：" + item["region_id"])
            if not isinstance(item.get("payload_sha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", item["payload_sha256"]
            ):
                raise ValueError("每张表都必须提供有效发送内容摘要")
            if item.get("sample_rows") is None and item.get("sample_range") is None:
                raise ValueError("每张表都必须明确选择样例行或样例矩形")

    def enqueue_standard_model_batch(self, source_id, payload):
        from .standard_workflow import MODEL, PROMPT_VERSION

        self._validate_standard_batch_request(payload)
        if not self.table_options.get("api_key"):
            raise ValueError("尚未配置表格模型；可以手动确认同一份读法")
        # Existing standard-review paths take standard before source. Do not
        # hold either lock while scheduling or waiting for provider execution.
        with self.store.lock("standard:" + source_id), self.store.lock("source:" + source_id):
            draft, _ = self._standard_input(source_id, payload["expected_revision"])
            if self._standard_state(draft)["version"] != payload["expected_version"]:
                raise StateConflict("含义版本已变化，请刷新并重新确认全部发送内容")
            expected = {r["id"] for r in draft["recipe"]["region_review"]["regions"]}
            actual = [item["region_id"] for item in payload["items"]]
            if len(actual) != len(set(actual)) or set(actual) != expected:
                raise ValueError("批次必须恰好包含全部已确认表格；不能遗漏、重复或包含未知表格")
            items = []
            for item in sorted(payload["items"], key=lambda value: value["region_id"]):
                preview = self.standard_payload_preview(
                    source_id,
                    {
                        "expected_revision": payload["expected_revision"],
                        "expected_version": payload["expected_version"],
                        **item,
                    },
                )
                if preview["payload_sha256"] != item["payload_sha256"]:
                    raise StateConflict("发送内容与预览不一致，请重新核对表格：" + item["region_id"])
                entry = {
                    "region_id": item["region_id"],
                    "payload_sha256": preview["payload_sha256"],
                    "sample_rows": preview["included_rows"],
                    "confirmed": True,
                    "job_id": self._standard_job_id(preview, MODEL, PROMPT_VERSION),
                    "payload": preview["payload"],
                }
                if "sample_range" in preview["payload"]:
                    entry["sample_range"] = preview["payload"]["sample_range"]
                items.append(entry)
            batch = {
                "source_id": source_id,
                "revision": draft["revision"],
                "version": payload["expected_version"],
                "draft_sha256": draft["draft_sha256"],
                "asset_hashes": draft["asset_hashes"],
                "model": MODEL,
                "prompt_version": PROMPT_VERSION,
                "items": items,
                "confirmed": True,
            }
            batch_id = "stb-" + fingerprint(batch)[:32]
            path = self._standard_batches_root() / batch_id / "batch.json"
            if not path.exists():
                # This is the only acceptance/publication point. Every payload
                # and every fixed job identity is durable before any job exists.
                batch.update(batch_id=batch_id, created_at=time.time())
                atomic_json(path, batch)
        self._dispatch_standard_model_batch(batch_id)
        return self.standard_model_batch(source_id, batch_id)

    def _standard_batch_current_error(self, batch):
        from .standard_workflow import MODEL, PROMPT_VERSION

        if batch.get("stale_error"):
            return batch["stale_error"]
        try:
            draft, _ = self._standard_input(batch["source_id"], batch["revision"])
            if draft["draft_sha256"] != batch["draft_sha256"] or draft["asset_hashes"] != batch["asset_hashes"]:
                return "原件或已确认范围已变化；剩余表格不会继续发送"
            if self._standard_state(draft)["version"] != batch["version"]:
                return "含义版本已变化；剩余表格不会继续发送"
            if batch["model"] != MODEL or batch["prompt_version"] != PROMPT_VERSION:
                return "模型或提示词版本已变化；请重新预览并确认批次"
        except (ValueError, KeyError, OSError, StateConflict) as exc:
            return str(exc) or "批次来源已失效；剩余表格不会继续发送"
        return None

    @staticmethod
    def _batch_job_matches(batch, item, job):
        expected = {
            "job_id": item["job_id"],
            "source_id": batch["source_id"],
            "revision": batch["revision"],
            "draft_sha256": batch["draft_sha256"],
            "expected_version": batch["version"],
            "region_id": item["region_id"],
            "payload_sha256": item["payload_sha256"],
            "model": batch["model"],
            "prompt_version": batch["prompt_version"],
            "sample_rows": item["sample_rows"],
        }
        return all(job.get(key) == value for key, value in expected.items()) and job.get("sample_range") == item.get(
            "sample_range"
        )

    def _dispatch_standard_model_batch(self, batch_id):
        path, batch = self._standard_batch_path(batch_id)
        # A completed outbox has nothing left to publish. Existing queued jobs
        # are recovered by the job scheduler, without rebuilding every payload
        # of every historical batch on each two-second poll.
        if all((self._standard_jobs_root() / item["job_id"] / "job.json").is_file() for item in batch["items"]):
            return
        source_id = batch["source_id"]
        queued = []
        with self.store.lock("standard:" + source_id), self.store.lock("source:" + source_id):
            batch = json.loads(path.read_text())
            error = self._standard_batch_current_error(batch)
            if error:
                if not batch.get("stale_error"):
                    atomic_json(path, {**batch, "stale_error": error, "stale_at": time.time()})
                return
            # Validate every persisted item before creating any missing jobs.
            # Recovery never derives a new identity from current model settings.
            previews = []
            for item in batch["items"]:
                preview = self.standard_payload_preview(
                    source_id,
                    {
                        "expected_revision": batch["revision"],
                        "expected_version": batch["version"],
                        "region_id": item["region_id"],
                        "sample_rows": item["sample_rows"],
                        "sample_range": item.get("sample_range"),
                    },
                )
                if (
                    preview["payload"] != item["payload"]
                    or preview["payload_sha256"] != item["payload_sha256"]
                    or self._standard_job_id(preview, batch["model"], batch["prompt_version"]) != item["job_id"]
                ):
                    atomic_json(
                        path, {**batch, "stale_error": "批次发送内容已变化；请重新预览并确认", "stale_at": time.time()}
                    )
                    return
                previews.append(preview)
            for item, preview in zip(batch["items"], previews):
                job = self._persist_standard_job(preview, model=batch["model"], prompt_version=batch["prompt_version"])
                if self._batch_job_matches(batch, item, job) and job["status"] == "queued":
                    queued.append(item["job_id"])
        for job_id in queued:
            self.start_standard_model_job(source_id, job_id)

    def recover_standard_model_batches(self):
        for path in self._standard_batches_root().glob("stb-*/batch.json"):
            try:
                self._dispatch_standard_model_batch(path.parent.name)
            except (OSError, ValueError, KeyError, StateConflict):
                # One corrupt outbox cannot prevent other sources from recovery.
                continue

    def standard_model_batch(self, source_id, batch_id):
        _, batch = self._standard_batch_path(batch_id)
        if batch["source_id"] != source_id:
            raise StateConflict("批次不属于当前资料")
        error = self._standard_batch_current_error(batch)
        counts = dict.fromkeys(
            (
                "pending",
                "queued",
                "running",
                "ready_for_review",
                "failed",
                "outcome_unknown",
                "needs_researcher",
                "invalid",
            ),
            0,
        )
        items = []
        for saved in batch["items"]:
            item = {key: value for key, value in saved.items() if key not in ("payload", "confirmed")}
            item.update(status="pending", error=None, form=None, diagnostics=None)
            try:
                _, job = self._standard_job_path(saved["job_id"])
                if not self._batch_job_matches(batch, saved, job):
                    item.update(status="invalid", error="任务身份与批次已确认内容不匹配")
                else:
                    item.update(
                        status=job["status"],
                        error=job.get("error"),
                        form=job.get("form"),
                        diagnostics=job.get("diagnostics"),
                        attempt=job.get("attempt"),
                    )
                    if item["status"] not in counts:
                        item.update(status="invalid", error="任务状态无法识别")
                    elif item["status"] == "ready_for_review" and (
                        not isinstance(item["form"], dict) or not item["form"]
                    ):
                        item.update(status="needs_researcher", error="模型任务尚无可审核读法")
            except KeyError:
                pass
            except (OSError, ValueError, StateConflict):
                item.update(status="invalid", error="无法读取批次对应任务，请检查持久任务文件")
            counts[item["status"]] += 1
            items.append(item)
        counts["total"] = len(items)
        ready = bool(items) and counts["ready_for_review"] == len(items) and not error
        if error:
            status = "stale"
        elif ready:
            status = "ready_for_review"
        elif any(counts[k] for k in ("failed", "outcome_unknown", "needs_researcher", "invalid")):
            status = "needs_attention"
        elif counts["running"] or counts["ready_for_review"]:
            status = "running"
        else:
            status = "queued"
        return {
            key: batch[key]
            for key in (
                "batch_id",
                "source_id",
                "revision",
                "version",
                "draft_sha256",
                "created_at",
                "model",
                "prompt_version",
            )
        } | {"status": status, "ready_for_review": ready, "counts": counts, "items": items, "error": error}

    def standard_model_batches(self, source_id):
        self.store.get_table_draft(source_id)  # Historical/stale batches remain readable.
        batches = []
        for path in self._standard_batches_root().glob("stb-*/batch.json"):
            try:
                batch = json.loads(path.read_text())
                if batch.get("source_id") == source_id:
                    batches.append(self.standard_model_batch(source_id, batch["batch_id"]))
            except (OSError, ValueError, KeyError, StateConflict):
                continue
        batches.sort(key=lambda item: (item["created_at"], item["batch_id"]))
        return {"source_id": source_id, "batches": batches}
