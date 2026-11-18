"""Read drafts and recipes from a workspace-bound ingestion store."""

from dataclasses import dataclass

from ...store import Store
from .table_region_review import PROFILE


@dataclass(frozen=True, slots=True)
class DraftReads:
    store: Store

    def drafts(self, workflow_mode: str) -> list[dict[str, object]]:
        values = self.store.list_table_drafts()
        if workflow_mode == "latest-only":
            return [value for value in values if value["recipe"].get("entry_profile") == PROFILE]
        return values

    def draft(self, source_id: str) -> dict[str, object]:
        return self.store.get_table_draft(source_id)

    def recipes(self) -> list[dict[str, object]]:
        return self.store.list_table_recipes()
