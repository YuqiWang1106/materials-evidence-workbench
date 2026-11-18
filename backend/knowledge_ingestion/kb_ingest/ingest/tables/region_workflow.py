"""Revision-bound range review. Never interprets meaning or submits extraction."""

import json

from kb_ingest.core import StateConflict, atomic_json, clock, fingerprint
from kb_ingest.storage.file_time import utc_text
from kb_ingest.storage.time_source import sourced_epoch

from .sheet_region_workflow import SheetRegionWorkflowMixin


def _receipt_view(receipt: dict[str, object]) -> dict[str, object]:
    return {**receipt, "confirmed_at": sourced_epoch(receipt["confirmed_at"])}


class RegionWorkflowMixin(SheetRegionWorkflowMixin):
    def _region_draft(self, source_id, expected_revision=None):
        from .table_region_review import PROFILE

        draft = self.store.get_table_draft(source_id)
        if draft["recipe"].get("entry_profile") != PROFILE:
            raise ValueError("请使用表格范围核对草稿")
        if expected_revision is not None and draft["revision"] != expected_revision:
            raise StateConflict("范围已修改，请载入最新版本后重新核对")
        assets = self._table_assets(draft["asset_ids"])
        if {a["asset_id"]: a["sha256"] for a in assets} != draft["asset_hashes"]:
            raise StateConflict("原件版本已变化，请重新核对")
        return draft, assets

    def _region_receipt_path(self, draft):
        identity = fingerprint(
            {"source_id": draft["source_id"], "revision": draft["revision"], "draft_sha256": draft["draft_sha256"]}
        )
        return self.store.root / "region_reviews" / (identity + ".json")

    def _region_receipt(self, draft):
        path = self._region_receipt_path(draft)
        if not path.exists():
            return None
        receipt = json.loads(path.read_text())
        if (
            receipt.get("review_version") != "1"
            or receipt.get("draft_sha256") != draft["draft_sha256"]
            or receipt.get("revision") != draft["revision"]
        ):
            raise StateConflict("范围确认版本不匹配，请重新核对")
        return _receipt_view(receipt)

    def region_review(self, source_id):
        from .table_region_review import inspect_review

        draft, assets = self._region_draft(source_id)
        result = inspect_review(draft["recipe"], assets, draft["reader_options"])
        progress = self._region_sheet_progress(draft, result["sheets"])
        self._region_draft(source_id, draft["revision"])
        return {
            **result,
            **progress,
            "revision": draft["revision"],
            "draft_sha256": draft["draft_sha256"],
            "range_confirmed": bool(self._region_receipt(draft)),
        }

    def confirm_regions(self, source_id, payload):
        from .table_region_review import inspect_review

        with self.store.lock("source:" + source_id):
            draft, assets = self._region_draft(source_id, payload["expected_revision"])
            result = inspect_review(draft["recipe"], assets, draft["reader_options"])
            if draft["recipe"]["region_review"].get("mode") == "sheet-by-sheet":
                if not self._region_sheet_progress(draft, result["sheets"])["all_sheets_confirmed"]:
                    raise StateConflict("请先逐一确认所有工作表，再确认全部范围")
            if not result["ready"]:
                raise ValueError("请先解决范围与角色问题，再确认范围")
            old = self._region_receipt(draft)
            if old:
                return old
            receipt = {
                "review_version": "1",
                "source_id": source_id,
                "revision": draft["revision"],
                "draft_sha256": draft["draft_sha256"],
                "asset_hashes": draft["asset_hashes"],
                "reader_options": draft["reader_options"],
                "region_review": draft["recipe"]["region_review"],
                "range_confirmed": True,
                "meaning_confirmed": False,
                "model_calls": 0,
                "operator": payload.get("operator", "本地使用者"),
                "confirmed_at": utc_text(clock.now()),
                "coverage": result["sheets"],
            }
            atomic_json(self._region_receipt_path(draft), receipt)
            return _receipt_view(receipt)

    def region_snapshot(self, source_id):
        draft, _ = self._region_draft(source_id)
        receipt = self._region_receipt(draft)
        if not receipt:
            raise StateConflict("当前范围尚未确认，不能作为下游输入")
        self._region_draft(source_id, draft["revision"])
        return receipt

    def region_context(self, source_id, payload):
        from .table_region_review import context_page

        draft, assets = self._region_draft(source_id, payload["expected_revision"])
        if not self._region_receipt(draft):
            raise StateConflict("当前范围尚未确认，请先核对范围")
        result = context_page(
            draft["recipe"],
            assets,
            draft["reader_options"],
            region_id=payload["region_id"],
            offset=payload.get("offset", 0),
            limit=payload.get("limit", 100),
        )
        self._region_draft(source_id, draft["revision"])
        return {
            **result,
            "revision": draft["revision"],
            "draft_sha256": draft["draft_sha256"],
            "asset_hashes": draft["asset_hashes"],
            "range_confirmed": True,
            "meaning_confirmed": False,
        }
