"""Source-bound per-sheet range decisions; no semantic inference or model calls."""

import json
from copy import deepcopy
from itertools import combinations

from openpyxl.utils.cell import get_column_letter, range_boundaries

from kb_ingest.core import StateConflict, atomic_json, clock, fingerprint
from kb_ingest.storage.file_time import utc_text

from .table_region_review import inspect_review

SHEET_REVIEW_VERSION = "sheet-region-review-1"


def _sheet_regions(draft, asset_id, sheet):
    return [r for r in draft["recipe"]["region_review"]["regions"] if (r["asset_id"], r["sheet"]) == (asset_id, sheet)]


def _overlaps(regions):
    from .table_region_review import _intersection

    result = []
    for left, right in combinations(regions, 2):
        box = _intersection(range_boundaries(left["table_range"]), range_boundaries(right["table_range"]))
        if box is None:
            continue
        c0, r0, c1, r1 = box

        def describe(region):
            return {"id": region["id"], "name": region.get("name", region["id"]), "range": region["table_range"]}

        result.append(
            {
                "asset_id": left["asset_id"],
                "sheet": left["sheet"],
                "left": describe(left),
                "right": describe(right),
                "intersection_range": f"{get_column_letter(c0)}{r0}:{get_column_letter(c1)}{r1}",
            }
        )
    return result


class SheetRegionWorkflowMixin:
    def _sheet_review_identity(self, draft, asset_id, sheet):
        return {
            "review_version": SHEET_REVIEW_VERSION,
            "source_id": draft["source_id"],
            "asset_id": asset_id,
            "asset_sha256": draft["asset_hashes"][asset_id],
            "sheet": sheet,
            "reader_options": deepcopy(draft["reader_options"].get(asset_id, {})),
            "regions": deepcopy(_sheet_regions(draft, asset_id, sheet)),
        }

    def _sheet_review_path(self, identity):
        return self.store.root / "region_sheet_reviews" / (fingerprint(identity) + ".json")

    def _sheet_is_confirmed(self, identity):
        path = self._sheet_review_path(identity)
        if not path.exists():
            return False
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        return receipt.get("identity") == identity and receipt.get("confirmed") is True

    def _region_sheet_progress(self, draft, sheet_inventory):
        review = draft["recipe"]["region_review"]
        initialized = review.get("initialized_sheets", [])
        sheets = []
        for source in sheet_inventory:
            key = {"asset_id": source["asset_id"], "sheet": source["sheet"]}
            identity = self._sheet_review_identity(draft, **key)
            prepared = review.get("mode") == "sheet-by-sheet" and key in initialized
            sheets.append(
                {
                    **source,
                    "initialized": prepared,
                    "confirmed": prepared and self._sheet_is_confirmed(identity),
                    "retained_count": len(identity["regions"]),
                    "sheet_sha256": fingerprint(identity),
                }
            )
        next_sheet = next(
            ({"asset_id": s["asset_id"], "sheet": s["sheet"]} for s in sheets if not s["confirmed"]), None
        )
        return {
            "sheets": sheets,
            "all_sheets_confirmed": bool(sheets) and all(s["confirmed"] for s in sheets),
            "next_sheet": next_sheet,
        }

    def _original_region_sheet(self, draft, assets, asset_id, sheet):
        from .table_reading import _selected_tables

        asset = next((a for a in assets if a["asset_id"] == asset_id), None)
        if asset is None or not (sheet is None or isinstance(sheet, str)):
            raise ValueError("请选择本次资料中的原始工作表")
        tables = _selected_tables(asset, draft["reader_options"].get(asset_id, {}))
        table = next((t for t in tables if t["sheet"] == sheet), None)
        if table is None:
            raise ValueError("原件中不存在所选工作表")
        return table

    def prepare_region_sheet(self, source_id, payload):
        from .table_discovery import discover_regions

        draft, assets = self._region_draft(source_id, payload["expected_revision"])
        key = {"asset_id": payload["asset_id"], "sheet": payload["sheet"]}
        table = self._original_region_sheet(draft, assets, **key)
        recipe = deepcopy(draft["recipe"])
        review = recipe["region_review"]
        if key in review.get("initialized_sheets", []):
            self._region_draft(source_id, draft["revision"])
            return draft
        discovered = discover_regions(table, key["asset_id"])
        if discovered["regions_truncated"]:
            raise ValueError("当前工作表识别出的表格超过容量上限，不能只初始化截断的候选范围")
        if not _sheet_regions(draft, **key):
            candidates = [r for r in discovered["regions"] if r["region_kind"] != "annotation_candidate"]
            for index, candidate in enumerate(candidates, 1):
                region_id = "region-" + fingerprint({**key, "range": candidate["range"]})[:24]
                review["regions"].append(
                    {
                        "id": region_id,
                        "name": f"表格 {index}",
                        **key,
                        "table_range": candidate["range"],
                        "header_ranges": candidate["suggested_header_ranges"],
                        "data_ranges": candidate["suggested_data_ranges"],
                        "context_ranges": [],
                    }
                )
        review["mode"] = "sheet-by-sheet"
        review.setdefault("initialized_sheets", []).append(key)
        # update_table_draft owns the source lock and performs revision CAS. Do
        # not hold that non-reentrant lock around this call.
        return self.update_table_draft(source_id, {"expected_revision": draft["revision"], "recipe": recipe})

    def confirm_region_sheet(self, source_id, payload):
        from kb_ingest.standard_table_meaning import _geometry

        with self.store.lock("source:" + source_id):
            draft, assets = self._region_draft(source_id, payload["expected_revision"])
            key = {"asset_id": payload["asset_id"], "sheet": payload["sheet"]}
            self._original_region_sheet(draft, assets, **key)
            review = draft["recipe"]["region_review"]
            if review.get("mode") != "sheet-by-sheet" or key not in review.get("initialized_sheets", []):
                raise StateConflict("请先载入并准备当前工作表")
            selected = _sheet_regions(draft, **key)
            # Inspect only this sheet's retained regions. Other worksheets may
            # still be unfinished; their coverage remains in the inventory.
            recipe = deepcopy(draft["recipe"])
            recipe["region_review"]["regions"] = deepcopy(selected)
            inspected = inspect_review(recipe, assets, draft["reader_options"])
            issues = [i for i in inspected["issues"] if i["code"] != "regions_required"]
            for region in selected:
                try:
                    _geometry(region)
                except ValueError as exc:
                    issues.append(
                        {
                            "code": "standard_geometry_required",
                            "message": str(exc),
                            "severity": "error",
                            "region_id": region["id"],
                            "name": region.get("name", region["id"]),
                            **key,
                        }
                    )
            overlaps = _overlaps(selected)
            # Inventory and receipt status refer to the full current draft.
            full = inspect_review(draft["recipe"], assets, draft["reader_options"])
            progress = self._region_sheet_progress(draft, full["sheets"])
            if issues:
                return {
                    "confirmed": False,
                    "issues": issues,
                    "overlaps": overlaps,
                    **progress,
                    "next_sheet": None,
                    "all_sheets_confirmed": False,
                }
            identity = self._sheet_review_identity(draft, **key)
            if not self._sheet_is_confirmed(identity):
                atomic_json(
                    self._sheet_review_path(identity),
                    {
                        "identity": identity,
                        "confirmed": True,
                        "draft_revision": draft["revision"],
                        "operator": payload.get("operator", "本地使用者"),
                        "confirmed_at": utc_text(clock.now()),
                        "model_calls": 0,
                        "meaning_confirmed": False,
                    },
                )
            return {
                "confirmed": True,
                "issues": [],
                "overlaps": [],
                **self._region_sheet_progress(draft, full["sheets"]),
            }
