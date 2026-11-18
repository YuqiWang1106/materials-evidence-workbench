"""Human-confirmed table workflow. No model client or executable user rules."""

import uuid
from copy import deepcopy
from pathlib import Path

from kb_ingest.contract import add_coverage, add_evidence
from kb_ingest.core import StateConflict, canonical, fingerprint


def _draft_reader_options(recipe, assets, options):
    """Pin new/explicitly edited standard drafts; never reinterpret old artifacts."""
    from .row_measurements import PROFILE
    from .table_reading import _options_by_asset
    from .table_region_review import PROFILE as REGION_PROFILE

    options = _options_by_asset(assets, options)
    if recipe.get("entry_profile") in (PROFILE, REGION_PROFILE):
        for asset in assets:
            if Path(asset["original_name"]).suffix.lower() == ".xlsx":
                options.setdefault(asset["asset_id"], {}).setdefault("formula_mode", "stored_value")
    return options


class TableWorkflowMixin:
    def _table_assets(self, asset_ids):
        if not asset_ids or len(asset_ids) != len(set(asset_ids)) or len(asset_ids) > 50:
            raise ValueError("请选择 1–50 个不同的表格文件")
        assets = []
        for aid in asset_ids:
            asset = self.store.get_asset(aid)
            if Path(asset["original_name"]).suffix.lower() not in (".csv", ".xlsx"):
                raise ValueError("当前表格入口支持 CSV 和 XLSX")
            self.store.asset_bytes(aid)  # Checks immutable content, not just metadata.
            assets.append({**asset, "local_path": str(self.store.asset_path(aid))})
        return assets

    def table_inventory(self, asset_id, reader_options=None):
        from .table_reading import list_asset_tables

        return list_asset_tables(self._table_assets([asset_id])[0], reader_options)

    def table_preview(self, asset_id, **options):
        from .table_reading import preview_table

        return preview_table(self._table_assets([asset_id])[0], **options)

    def reading_preview(self, source_id, payload):
        """Read one version of a draft; never confirm, extract, save or call a model."""
        from openpyxl.utils import get_column_letter

        from .extractors.experimental import _cell, _column
        from .extractors.profile_regions import selected_ranges
        from .extractors.table_headers import apply_metric, bounded_header_context, resolve_headers
        from .extractors.table_orientation import oriented_table
        from .table_reading import _selected_tables
        from .table_recipe import validate_profile_list

        draft = self.store.get_table_draft(source_id)
        if not draft["recipe"].get("standard_review"):
            self.require_legacy_table_workflow()
        if draft["revision"] != payload.get("expected_revision"):
            raise StateConflict("草稿已变化，请重新查看读法")
        if draft["recipe"].get("version") == "2":
            from .plan_workflow import preview

            return preview(self, draft, payload)
        index, offset, limit = payload.get("profile_index"), payload.get("offset", 0), payload.get("limit", 30)
        if (
            type(index) is not int
            or not 0 <= index < len(draft["recipe"]["profiles"])
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("读法预览索引或分页范围无效")
        profile = draft["recipe"]["profiles"][index]
        asset = self._table_assets([profile["asset_id"]])[0]
        tables = _selected_tables(asset, draft["reader_options"].get(asset["asset_id"], {}))
        validate_profile_list([profile], tables, asset["asset_id"])
        table = oriented_table(next(t for t in tables if t["sheet"] == profile["sheet"]), profile)
        layout = profile["layout"]
        start = profile.get("data_start_row", 1 if layout == "key_value" else (profile.get("header_row", 1) or 0) + 1)
        ranges = selected_ranges(profile, table["nrows"]) or [[start, profile.get("data_end_row", table["nrows"])]]
        specs = []
        if layout in ("curves", "channels"):
            specs = (
                list(profile["series"])
                if layout == "curves"
                else [{"x_column": profile["x_column"], "y_column": c} for c in profile["y_columns"]]
            )
        elif layout == "key_value":
            for row in range(start, profile.get("data_end_row", table["nrows"]) + 1):
                key = _cell(table, row, _column(profile.get("key_column", 1)))
                field = profile.get("fields", {}).get(str(key["raw_value"]))
                if field is not None:
                    specs.append({**field, "column": profile.get("value_column", 2), "_row": row, "_key": key})
        else:
            specs = profile["fields"]

        def iter_specs():
            for spec in specs:
                if layout in ("records", "objects") and (
                    spec.get("unit_column") is not None or spec.get("qualifier_columns")
                ):
                    for lo, hi in ranges:
                        for row in range(lo, hi + 1):
                            yield {**spec, "_row": row}
                else:
                    yield spec

        row_count = sum(hi - lo + 1 for lo, hi in ranges)
        total = sum(
            row_count
            if layout in ("records", "objects") and (s.get("unit_column") is not None or s.get("qualifier_columns"))
            else 1
            for s in specs
        )
        items = []
        from itertools import islice

        for spec_index, spec in enumerate(islice(iter_specs(), offset, offset + limit), offset):
            if layout in ("curves", "channels"):
                contexts = [
                    resolve_headers(asset, table, profile, spec[axis + "_column"], axis=axis)["context"]
                    for axis in ("x", "y")
                ]
                item = {"kind": "curve", "index": spec_index}
            else:
                resolved = resolve_headers(
                    asset, table, profile, spec["column"], spec, row=spec.get("_row"), header_cell=spec.get("_key")
                )
                if spec["field_id"] == "observation.value":
                    from .extractors.table_semantics import resolve_qualifiers

                    qualifiers = (
                        resolve_qualifiers(table, spec["_row"], spec, set())[0]
                        if "_row" in spec
                        else deepcopy(spec.get("qualifiers", {}))
                    )
                    apply_metric(qualifiers, resolved)
                contexts = [resolved["context"]]
                item = {"kind": "field", "index": spec_index, "field_id": spec["field_id"]}
                if "_key" in spec:
                    item["source_key"] = str(spec["_key"]["raw_value"])[:512]
                if "_row" in spec:
                    item["source_row"] = spec["_row"]
                if spec.get("unit_column") is not None:
                    item["row_dependent_unit_column"] = get_column_letter(_column(spec["unit_column"]))
            item["columns"] = bounded_header_context(contexts, confirmed=False)
            items.append(item)
        selected_columns = sorted({_column(s[c]) for s in specs for c in ("x_column", "y_column", "column") if c in s})
        # Optimistic read check: a concurrent edit cannot receive a seemingly current preview.
        latest = self.store.get_table_draft(source_id)
        if latest["revision"] != draft["revision"] or latest["draft_sha256"] != draft["draft_sha256"]:
            raise StateConflict("草稿已变化，请重新查看读法")
        return {
            "source_id": source_id,
            "revision": draft["revision"],
            "draft_sha256": draft["draft_sha256"],
            "profile_index": index,
            "asset_id": asset["asset_id"],
            "sheet": profile["sheet"],
            "layout": layout,
            "orientation": profile.get("orientation", "rows"),
            "reading_confirmed": False,
            "selected_region": {
                "data_ranges": ranges,
                "columns": [get_column_letter(c) for c in selected_columns[:128]],
                "column_total": len(selected_columns),
                "columns_truncated": len(selected_columns) > 128,
            },
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(items) < total,
            "model_calls": 0,
        }

    def create_table_draft(self, payload):
        payload = deepcopy(payload)
        from .table_region_review import PROFILE as REGION_PROFILE

        if payload.get("entry_profile") != REGION_PROFILE:
            self.require_legacy_table_workflow()
        payload.setdefault("source_id", "table-" + uuid.uuid4().hex[:20])
        assets = self._table_assets(payload.get("asset_ids", []))
        payload.setdefault("title", assets[0]["original_name"])
        if payload.get("entry_profile") is not None:
            from .row_measurements import PROFILE
            from .table_region_review import PROFILE as REGION_PROFILE

            if payload["entry_profile"] == REGION_PROFILE:
                payload["recipe"] = {
                    "version": "1",
                    "entry_profile": REGION_PROFILE,
                    "region_review": {"version": "1", "regions": []},
                }
            elif payload["entry_profile"] == PROFILE:
                payload["recipe"] = {"version": "2", "entry_profile": PROFILE, "definition": {"version": "1"}}
            else:
                raise ValueError("未知表格入口 profile")
        payload.setdefault("recipe", {"profiles": [], "excluded_ranges": [], "supplements": []})
        payload.setdefault("reader_options", {})
        payload["reader_options"] = _draft_reader_options(payload["recipe"], assets, payload["reader_options"])
        from .table_reading import validate_recipe

        payload["recipe"] = validate_recipe(payload["recipe"], assets, payload["reader_options"])
        return self.store.create_table_draft(payload)

    def update_table_draft(self, source_id, payload):
        draft = self.store.get_table_draft(source_id)
        proposed = deepcopy({**draft, **payload})
        from .table_region_review import PROFILE as REGION_PROFILE

        if (
            draft["recipe"].get("entry_profile") != REGION_PROFILE
            or proposed["recipe"].get("entry_profile") != REGION_PROFILE
        ):
            self.require_legacy_table_workflow()
        if draft["recipe"].get("standard_review") is not None:
            raise StateConflict("请回到表格含义页修改；已绑定的执行草稿不能直接改写")
        from .table_region_review import PROFILE as REGION_PROFILE

        if (draft["recipe"].get("entry_profile") == REGION_PROFILE) != (
            proposed["recipe"].get("entry_profile") == REGION_PROFILE
        ):
            raise ValueError("范围核对与科学提取使用独立草稿，不能切换入口")
        assets = self._table_assets(proposed["asset_ids"])
        proposed["reader_options"] = _draft_reader_options(proposed["recipe"], assets, proposed["reader_options"])
        current_review = draft["recipe"].get("region_review", {})
        proposed_review = proposed["recipe"].get("region_review", {})
        if current_review.get("mode") == "sheet-by-sheet":
            if not isinstance(proposed_review, dict):
                raise ValueError("工作表范围配置必须是对象")
            if proposed_review.get("mode") != "sheet-by-sheet":
                raise StateConflict("逐工作表流程不能降级回旧范围确认方式")
            markers = proposed_review.get("initialized_sheets", [])
            if not isinstance(markers, list) or any(
                not isinstance(item, dict)
                or set(item) != {"asset_id", "sheet"}
                or not isinstance(item["asset_id"], str)
                or not (item["sheet"] is None or isinstance(item["sheet"], str))
                for item in markers
            ):
                raise ValueError("工作表初始化记录必须是原件和工作表标识列表")
            changed_readers = {
                aid
                for aid in proposed["asset_ids"]
                if draft["reader_options"].get(aid, {}) != proposed["reader_options"].get(aid, {})
            }
            if any(
                item["asset_id"] not in changed_readers and item not in markers
                for item in current_review.get("initialized_sheets", [])
            ):
                raise StateConflict("不能移除工作表初始化记录；已删除的表格不会重新添加")
            # New reader coordinates require a fresh visit. Preserve markers
            # for other originals and allow the UI to clear the changed file.
            if changed_readers:
                proposed_review["initialized_sheets"] = [
                    item for item in markers if item["asset_id"] not in changed_readers
                ]
        from .row_measurements import is_profile

        if is_profile(draft["recipe"]):
            if not is_profile(proposed["recipe"]):
                raise ValueError("不能移除已选行式入口 profile")
            # Definitions and reader settings both determine the generated plan.
            # Only the exact previous server plan may be discarded/recompiled.
            if (
                proposed["recipe"].get("definition") != draft["recipe"].get("definition")
                or proposed["reader_options"] != draft["reader_options"]
            ) and proposed["recipe"].get("reading_plan") == draft["recipe"].get("reading_plan"):
                proposed["recipe"].pop("reading_plan", None)
                if proposed["recipe"].get("excluded_ranges") == draft["recipe"].get("excluded_ranges"):
                    proposed["recipe"].pop("excluded_ranges", None)
        from .table_reading import validate_recipe

        normalized = validate_recipe(proposed["recipe"], assets, proposed["reader_options"])
        invalidated = []
        if normalized.get("version") == "2":
            from .plan_workflow import scope_hashes

            hashes = scope_hashes(
                normalized["reading_plan"],
                assets,
                self.pipeline_fingerprint,
                reader_options=proposed["reader_options"],
                annotations={k: normalized.get(k, []) for k in ("supplements", "excluded_ranges")},
            )
            reviews = normalized.get("reviewed_scopes", {})
            invalidated = [key for key, value in reviews.items() if hashes.get(key) != value]
            normalized["reviewed_scopes"] = {key: value for key, value in reviews.items() if hashes.get(key) == value}
        result = self.store.update_table_draft(
            source_id, {**payload, "recipe": normalized, "reader_options": proposed["reader_options"]}
        )
        if normalized.get("version") == "2":
            result["invalidated_scopes"] = invalidated
        return result

    def upgrade_table_draft(self, source_id, payload):
        """Editing a historical recipe creates a new, unconfirmed v2 draft."""
        self.require_legacy_table_workflow()
        from .table_plan import adapt_profiles
        from .table_reading import _selected_tables

        draft = self.store.get_table_draft(source_id)
        if draft["revision"] != payload["expected_revision"]:
            raise StateConflict("草稿已变化，请重新加载")
        if draft["recipe"].get("version") == "2":
            return draft
        assets = self._table_assets(draft["asset_ids"])
        tables = {a["asset_id"]: _selected_tables(a, draft["reader_options"].get(a["asset_id"], {})) for a in assets}
        plan = adapt_profiles(draft["recipe"].get("profiles", []), tables)
        recipe = {
            "version": "2",
            "reading_plan": plan,
            "profiles": [],
            "reviewed_scopes": {},
            "excluded_ranges": deepcopy(draft["recipe"].get("excluded_ranges", [])),
            "supplements": deepcopy(draft["recipe"].get("supplements", [])),
        }
        return self.update_table_draft(source_id, {"expected_revision": draft["revision"], "recipe": recipe})

    def confirm_table_draft(self, source_id, payload):
        draft = self.store.get_table_draft(source_id)
        if not draft["recipe"].get("standard_review"):
            self.require_legacy_table_workflow()
        from .table_region_review import PROFILE as REGION_PROFILE

        if draft["recipe"].get("entry_profile") == REGION_PROFILE:
            raise ValueError("这里只确认表格范围；请先完成独立的含义确认再提取")
        if draft["revision"] != payload["expected_revision"]:
            raise StateConflict("草稿已变化，请重新查看并确认")
        self._require_standard_confirmation(draft["recipe"], draft["source_id"], draft["draft_sha256"])
        from .table_reading import validate_recipe

        validate_recipe(draft["recipe"], self._table_assets(draft["asset_ids"]), draft["reader_options"])
        from .row_measurements import is_profile, require_ready

        if is_profile(draft["recipe"]):
            require_ready(self, draft)
        if draft["recipe"].get("version") == "2":
            from .plan_workflow import confirm_manifest

            manifest = confirm_manifest(self, draft, payload)
        else:
            manifest = None
        if not draft["recipe"].get("profiles") and manifest is None:
            raise ValueError("请至少定义一组要提取的内容")
        return self.store.confirm_table_draft(
            source_id, {**payload, "_execution_manifest": manifest, "_pipeline_fingerprint": self.pipeline_fingerprint}
        )

    def _require_table_confirmation(self, task):
        version_id = task.get("confirmation_version_id")
        if not version_id:
            raise StateConflict("该表格任务没有确认版本，请重新确认读法")
        version = self.store.get_confirmation(version_id)
        self._require_standard_confirmation(version["recipe"], task["source_id"], version["draft_sha256"])
        draft = self.store.get_table_draft(task["source_id"])
        if draft["revision"] != version["draft_revision"] or draft["draft_sha256"] != version["draft_sha256"]:
            raise StateConflict("草稿或读法已修改，请重新确认")
        from .row_measurements import is_profile, require_ready

        if is_profile(version["recipe"]):
            if not is_profile(draft["recipe"]):
                raise StateConflict("行式入口确认被改变")
            require_ready(self, draft)
        options = task["options"]
        if (
            task["asset_ids"] != version["asset_ids"]
            or options.get("table_mode") != "confirmed"
            or options.get("table_profiles") != version["recipe"]["profiles"]
            or options.get("reader_options") != version["reader_options"]
            or options.get("auto_detect") is not False
        ):
            raise StateConflict("提取配置与确认版本不一致")
        if version["recipe"].get("version") == "2":
            if (
                options.get("reading_plan") != version["recipe"]["reading_plan"]
                or options.get("_execution_manifest") != version["structure"]
                or version["structure"].get("pipeline_fingerprint") != self.pipeline_fingerprint
            ):
                raise StateConflict("执行计划与确认版本不一致，请重新核对")
        if self.store.source_state(task["source_id"])["active_task_id"] != task["task_id"]:
            raise StateConflict("资料版本已变化，请使用最新确认任务")
        for asset in self._table_assets(task["asset_ids"]):
            if version["asset_hashes"].get(asset["asset_id"]) != asset["sha256"]:
                raise StateConflict("原件与确认版本不一致")
        return version

    def create_table_recipe(self, payload):
        """Only a confirmed reading can become a reusable named recipe."""
        self.require_legacy_table_workflow()
        from .table_reading import structure_snapshot

        draft = self.store.get_table_draft(payload["source_id"])
        if draft["revision"] != payload["expected_revision"]:
            raise StateConflict("草稿已变化，请重新确认")
        source = self.store.source_state(draft["source_id"])
        if not source or not source.get("active_task_id"):
            raise StateConflict("请先确认一次读法")
        task = self.store.get_task(source["active_task_id"])
        version = self._require_table_confirmation(task)
        recipe = deepcopy(version["recipe"])
        if recipe.get("version") == "2":
            for view in recipe["reading_plan"]["views"]:
                view["asset_slot"] = draft["asset_ids"].index(view.pop("asset_id"))
            recipe["reviewed_scopes"] = {}
        # File selectors are locations in the input bundle, never old raw assets.
        names = {self.store.get_asset(a)["original_name"]: a for a in draft["asset_ids"]}
        for profile in recipe["profiles"]:
            aid = profile.pop("asset_id", None) or names.get(profile.pop("original_name", None))
            if aid not in draft["asset_ids"]:
                raise ValueError("方案需要明确每个区域属于哪个文件")
            profile["asset_slot"] = draft["asset_ids"].index(aid)
            profile.pop("run_key", None)
            profile.pop("candidate_scalar_scope", None)
        recipe["excluded_ranges"] = []
        recipe["supplements"] = []
        readers = {str(i): deepcopy(draft["reader_options"].get(a, {})) for i, a in enumerate(draft["asset_ids"])}
        return self.store.create_table_recipe(
            {
                "name": payload["name"],
                "recipe": recipe,
                "reader_options": readers,
                "operator": payload.get("operator", "本地使用者"),
                "structure": structure_snapshot(self._table_assets(draft["asset_ids"]), draft["reader_options"]),
            }
        )

    def reuse_table_recipe(self, source_id, payload):
        self.require_legacy_table_workflow()
        from .table_reading import compare_structures, structure_snapshot

        draft = self.store.get_table_draft(source_id)
        version = self.store.get_table_recipe(payload["recipe_version_id"])
        recipe = deepcopy(version["recipe"])
        if recipe.get("version") == "2":
            for view in recipe["reading_plan"]["views"]:
                slot = view.pop("asset_slot", None)
                if type(slot) is not int or not 0 <= slot < len(draft["asset_ids"]):
                    raise ValueError("文件数量与方案不同，请重新选择读法")
                view["asset_id"] = draft["asset_ids"][slot]
            recipe["reviewed_scopes"] = {}
        for profile in recipe["profiles"]:
            slot = profile.pop("asset_slot", None)
            if type(slot) is not int or not 0 <= slot < len(draft["asset_ids"]):
                raise ValueError("文件数量或顺序与方案不同，请重新选择读法")
            profile["asset_id"] = draft["asset_ids"][slot]
            if profile.get("layout") == "key_value":
                profile["run_key"] = (
                    "source-block-"
                    + fingerprint(
                        [slot, profile.get("sheet"), profile.get("data_start_row"), profile.get("data_end_row")]
                    )[:16]
                )
        readers = {
            a: deepcopy(version.get("reader_options", {}).get(str(i), {})) for i, a in enumerate(draft["asset_ids"])
        }
        current = structure_snapshot(self._table_assets(draft["asset_ids"]), readers)
        changes = compare_structures(version.get("structure", {}), current)
        # Structural changes are not silently accepted as a validated mapping.
        try:
            updated = self.update_table_draft(
                source_id,
                {"expected_revision": payload["expected_revision"], "recipe": recipe, "reader_options": readers},
            )
        except ValueError as exc:
            return {
                "draft": draft,
                "comparison": {**changes, "compatible": False},
                "applied": False,
                "message": str(exc),
            }
        return {"draft": updated, "comparison": changes, "applied": True}

    def _annotate_confirmed_package(self, task, package):
        from .table_reading_exclusions import validate_exclusions_against_package
        from .table_results import processing_summary

        confirmation = self.store.get_confirmation(task["confirmation_version_id"])
        validate_exclusions_against_package(confirmation["recipe"], package)
        package["sources"][0]["title"] = confirmation["title"]
        package["header"]["extraction_mode"] = "human_confirmed_table"
        if confirmation["recipe"].get("standard_review"):
            package["header"]["standard_table_review"] = {
                **confirmation["recipe"]["standard_review"],
                "scientific_expert_review": False,
                "human_meaning_confirmed": True,
            }
        package["header"]["reading_confirmation"] = {
            "version_id": task["confirmation_version_id"],
            "draft_revision": confirmation["draft_revision"],
            "recipe_sha256": confirmation["recipe_sha256"],
            "operator": confirmation["operator"],
            "confirmed_at": confirmation["created_at"],
            "scope": "reading_meaning_and_relationships",
        }
        package["header"]["human_expert_review"] = False
        if confirmation["recipe"].get("version") == "2":
            package["header"]["execution_manifest"] = deepcopy(confirmation["structure"])
        package["header"]["selected_scope"] = deepcopy(confirmation["recipe"])
        for index, note in enumerate(confirmation["recipe"].get("supplements", [])):
            statement = {
                **note,
                "confirmed_at": confirmation["created_at"],
                "confirmation_version_id": task["confirmation_version_id"],
            }
            asset = self.store.register_bytes(canonical(statement).encode("utf-8"), f"supplement-{index + 1}.json")
            package["assets"].append({**asset, "role": "supplemental_note"})
            package["sources"][0]["asset_refs"].append(asset["asset_id"])
            add_evidence(
                package,
                asset["asset_id"],
                {"statement": 1, "scope": note["scope"]},
                statement,
                "human_supplied_statement",
                scientific_use="context_only",
            )
        for scope in confirmation["recipe"].get("excluded_ranges", []):
            add_coverage(
                package,
                scope,
                "not_applicable",
                "研究人员明确选择不整理此范围",
                reading_confirmation_ref=task["confirmation_version_id"],
            )
        # Explicit exclusions resolve only those unmapped cells, never an actual
        # extraction failure or a scientific identity/relationship question.
        from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

        exclusions = confirmation["recipe"].get("excluded_ranges", [])
        for item in package["coverage"]:
            scope = item.get("scope")
            if not isinstance(scope, dict) or scope.get("scope") != "unmapped_cells":
                continue
            rectangles = [
                range_boundaries(e["range"])
                for e in exclusions
                if e["asset_id"] == scope.get("asset_ref") and e["sheet"] == scope.get("sheet")
            ]
            excluded, remaining = [], []
            for address in item.get("cell_addresses", []):
                row, col = coordinate_to_tuple(address)
                (
                    excluded if any(c0 <= col <= c1 and r0 <= row <= r1 for c0, r0, c1, r1 in rectangles) else remaining
                ).append(address)
            if excluded:
                item["cell_addresses"] = remaining
                item["excluded_cell_addresses"] = excluded
                item["handling"] = "not_checked" if remaining else "checked"
                item["reason"] = "剩余未整理单元格已明确记录；排除范围单独留痕"
        if confirmation["recipe"].get("version") == "2":
            from .table_reading import apply_plan_exclusions

            apply_plan_exclusions(package, exclusions, task["confirmation_version_id"])
        from .row_measurements import annotate, is_profile, require_ready

        if is_profile(confirmation["recipe"]):
            _, completion = require_ready(self, self.store.get_table_draft(task["source_id"]))
            annotate(package, confirmation["recipe"], completion)
        package["header"]["processing_summary"] = processing_summary(package)

    def result_view(self, task_id, *, offset=0, limit=100, subject_ref=None):
        from .table_results import result_view

        task = self.store.get_task(task_id)
        package = self.records(task_id)
        return result_view(package, task, offset=offset, limit=limit, subject_ref=subject_ref)
