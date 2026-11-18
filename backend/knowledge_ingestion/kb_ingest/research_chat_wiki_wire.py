"""Turn-local model aliases. Canonical evidence and persisted answers stay intact."""

from copy import deepcopy

from .research_chat_answer_wire import WireAnswerDraft, bounded_wire
from .research_chat_contracts import ResearchError


class WikiModelWire:
    def __init__(self, registry):
        self.registry = registry
        self.aliases = {}
        self.canonical_ids = {}

    def _issue(self, key):
        self.registry.item(key)
        if key not in self.aliases:
            alias = "r" + str(len(self.aliases) + 1)
            self.aliases[key] = alias
            self.canonical_ids[alias] = key
        return self.aliases[key]

    def _resolve(self, alias):
        # Raw canonical IDs and similar-looking claim hashes are not aliases.
        if not isinstance(alias, str):
            raise ResearchError("invalid_request")
        key = self.canonical_ids.get(alias)
        if key is None:
            raise ResearchError("invalid_request")
        self.registry.item(key)
        return key

    def _arguments(self, name, args, transform):
        value = deepcopy(args)
        if name == "wiki_read" and "hit_id" in value:
            value["hit_id"] = transform(value["hit_id"])
        elif name == "wiki_evidence":
            if "handle" in value:
                value["handle"] = transform(value["handle"])
            selector = value.get("selector")
            if (
                isinstance(selector, dict)
                and selector.get("operation") == "compare_tables"
                and isinstance(selector.get("tables"), list)
            ):
                for table in selector.get("tables", []):
                    if isinstance(table, dict) and "handle" in table:
                        table["handle"] = transform(table["handle"])
        elif name == "research_validate" and isinstance(value.get("ids"), list):
            value["ids"] = [transform(key) for key in value["ids"]]
        return value

    def arguments(self, name, args):
        # Existing typed tools still check kind, selector, authority and freshness.
        # In particular a saved analysis continuation may use wiki_evidence ID.
        return self._arguments(name, args, self._resolve)

    def project(self, value):
        """Map explicit model-reference positions only, never scientific payloads."""
        value = deepcopy(value)
        for name in ("items", "prior_items"):
            for item in value.get(name, []):
                if not item.get("kind", "").startswith("wiki_"):
                    continue
                entry = self.registry.item(item["id"], item["kind"])
                alias = self._issue(entry["id"])
                item["id"] = alias
                for key in ("hit_id", "handle"):
                    if key in item:
                        item[key] = self._issue(item[key])
                for key in ("page_id", "revision_id", "claim_id", "support_id"):
                    item.pop(key, None)
        for support in value.get("supports", []):
            if "handle" in support:
                support["handle"] = self._issue(support["handle"])
            support.pop("support_id", None)
        for next_read in value.get("next_read", []):
            if isinstance(next_read.get("arguments"), dict):
                next_read["arguments"] = self._arguments(next_read.get("name"), next_read["arguments"], self._issue)
        for name in ("ids", "focus_ids"):
            if name in value:
                value[name] = [self._issue(key) for key in value[name]]
        return value

    def _answer_aliases(self):
        return [
            alias
            for key, alias in self.aliases.items()
            if key in self.registry.entries and "answer" in self.registry.operations(self.registry.entries[key])
        ]

    def _answer_id(self, alias, kind=None):
        key = self._resolve(alias)
        entry = self.registry.item(key, kind)
        if "answer" not in self.registry.operations(entry):
            raise ResearchError("answer_validation_failed")
        return key

    def answer(self, raw):
        draft = WireAnswerDraft.model_validate(raw)
        bounded_wire(draft)
        value = draft.model_dump(mode="json")
        for block in value["blocks"]:
            if "id" in block:
                block["id"] = self._answer_id(block["id"], block["kind"])
            for segment in block.get("segments", []):
                if "id" in segment:
                    segment["id"] = self._answer_id(segment["id"], "fact")
        value["focus_ids"] = [self._answer_id(alias) for alias in value["focus_ids"]]
        return value

    def schema(self):
        schema = WireAnswerDraft.model_json_schema()
        aliases = self._answer_aliases()
        # Wiki prose uses ordinary text; scientific selections are entire typed
        # wiki_content/wiki_evidence blocks, not fact-segment interpolation.
        schema["$defs"]["WireEditorialBlock"]["properties"]["segments"]["items"] = {"$ref": "#/$defs/TextSegment"}
        schema["$defs"].pop("ReferenceSegment")
        if aliases:
            selected = schema["$defs"]["SelectedClaim"]["properties"]
            selected["id"]["enum"] = aliases
            kinds = sorted({self.registry.entries[self.canonical_ids[a]]["kind"] for a in aliases})
            selected["kind"]["enum"] = kinds
            mapping = schema["properties"]["blocks"]["items"]["discriminator"]["mapping"]
            for kind in list(mapping):
                if mapping[kind] == "#/$defs/SelectedClaim" and kind not in kinds:
                    del mapping[kind]
            schema["properties"]["focus_ids"]["items"]["enum"] = aliases
        else:
            schema["$defs"].pop("SelectedClaim")
            schema["properties"]["blocks"]["items"] = {"$ref": "#/$defs/WireEditorialBlock"}
            schema["properties"]["focus_ids"]["maxItems"] = 0
        return schema
