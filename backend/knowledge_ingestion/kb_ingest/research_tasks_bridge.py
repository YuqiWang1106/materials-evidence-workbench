"""Bridge registered read IDs to bounded immutable deterministic task artifacts.

Model arguments never supply evidence, user messages, coverage, or program output.
The existing snapshot store holds full inputs; the chat registry owns their IDs.
"""

from copy import deepcopy

from .core.serialization import canonical, fingerprint
from .knowledge_contracts import VERSION, CompareRequest, GetRequest, KnowledgeResult, QueryRequest
from .knowledge_service import READ_DEADLINE
from .knowledge_snapshot import KnowledgeIntegrityError
from .research_chat_contracts import ResearchError
from .research_chat_projection import identity
from .research_tasks import bind_user_target, evaluate_constraints, performance_reference, research_plan
from .research_tasks_contracts import POLICY_VERSION, UserMessage, UserTarget
from .research_tasks_render import render_target, render_task

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
KINDS = {"user_target", "task_result", "query_receipt", "comparison_receipt"}


class ResearchTasksBridge:
    def __init__(self, tools, *, user_message=None, message_reader=None):
        self.tools = tools
        self.registry = tools.registry
        self.knowledge = tools.knowledge
        self.user_message = UserMessage.model_validate(user_message) if user_message is not None else None
        self.message_reader = message_reader

    def check(self):
        deadline = READ_DEADLINE.get()
        if deadline is not None:
            self.knowledge._deadline(deadline)

    def scope(self):
        return {
            k: self.tools.scope("record")[k]
            for k in ("workspace_id", "collection_id", "corpus_snapshot_id", "use_policy")
        }

    def write(self, payload):
        self.check()
        if len(canonical(payload).encode()) > MAX_ARTIFACT_BYTES:
            raise ResearchError("task_record_limit")
        digest = self.knowledge.snapshots.put_json(payload)
        self.check()
        return digest

    def read(self, key):
        self.check()
        entry = self.registry.item(key)
        if entry["kind"] not in KINDS:
            raise ResearchError("invalid_request")
        try:
            store = self.knowledge.snapshots
            path = store._path("objects", entry["artifact_sha256"])
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ResearchError("task_record_limit")
            obj = store.get_json(entry["artifact_sha256"])
            if (obj["version"], obj["kind"], obj["scope"], obj["dependencies"]) != (
                POLICY_VERSION,
                entry["kind"],
                entry["scope"],
                entry["dependency_ids"],
            ):
                raise ResearchError("stale_context")
            self.check()
            return obj["data"]
        except (KeyError, OSError, KnowledgeIntegrityError):
            raise ResearchError("task_record_unavailable") from None

    def register(self, kind, data, *, dependencies=(), text=None, **metadata):
        dependencies = list(dict.fromkeys(dependencies))
        scope = self.scope()
        for key in dependencies:
            self.registry.item(key)
        payload = {"version": POLICY_VERSION, "kind": kind, "scope": scope, "dependencies": dependencies, "data": data}
        entry = {
            "kind": kind,
            "layer": "record",
            "scope": scope,
            "artifact_sha256": self.write(payload),
            "dependency_ids": dependencies,
            **metadata,
        }
        if text is not None:
            entry["text"] = text
        entry["id"] = self.registry._id(entry)
        if len(canonical(entry).encode()) > 16000:
            raise ResearchError("task_record_limit")
        self.check()
        self.registry.entries[entry["id"]] = deepcopy(entry)
        return deepcopy(entry)

    def receipt(self, kind, request, response, *, comparison_ids=()):
        data = {"request": request.model_dump(mode="json"), "response": response.model_dump(mode="json")}
        return self.register(kind, data, comparison_ids=list(comparison_ids))

    @staticmethod
    def _query_identity(request):
        return request.model_dump(mode="json", exclude={"request_id", "page"}) | {"page_size": request.page.size}

    def _query_page(self, key):
        entry = self.registry.item(key)
        if entry["kind"] != "query_receipt" or entry["layer"] != "record":
            raise ResearchError("query_receipt_invalid")
        self.registry._scope("record", entry["scope"])
        data = self.read(key)
        try:
            if set(data) != {"request", "response"}:
                raise ValueError()
            request = QueryRequest.model_validate(data["request"])
            result = KnowledgeResult.model_validate(data["response"])
        except (ValueError, TypeError):
            raise ResearchError("query_receipt_invalid") from None
        scope = self.tools.scope("record")
        if any(getattr(request, k) != scope[k] for k in ("collection_id", "corpus_snapshot_id", "use_policy")):
            raise ResearchError("stale_context")
        self.registry._scope(
            "record",
            {
                "workspace_id": result.workspace_id,
                "collection_id": result.scope.collection_id,
                "corpus_snapshot_id": result.versions.corpus_snapshot_id,
                "use_policy": result.scope.use_policy,
            },
        )
        if result.operation != "query":
            raise ResearchError("query_receipt_invalid")
        for ref in request.selection or []:
            self.registry._owner(ref.model_dump(mode="json"), scope)
        for row in result.results:
            if row.kind != "sample_test_context":
                raise ResearchError("query_receipt_invalid")
            self.registry._owner(row.ref.model_dump(mode="json"), scope)
        return data, request, result

    def query_navigation(self, key):
        # Derived from a hash-verified artifact, never added to historical IDs.
        _, request, result = self._query_page(key)
        definition = self._query_identity(request)
        c = result.completeness
        query = {k: definition[k] for k in ("filters", "include_history", "allow_partial", "page_size")}
        query["selection"] = [identity(r.model_dump(mode="json")) for r in request.selection or []]
        return {
            "scientific_evidence": False,
            "population": "query_matches",
            "query_group": fingerprint(definition),
            "query": query,
            "collection_id": request.collection_id,
            "use_policy": request.use_policy,
            "returned": c.returned,
            "total_matches": c.total_matches,
            "first_page": request.page.cursor is None,
            "last_page": c.next_cursor is None,
            "has_more": c.next_cursor is not None,
            "matching_complete": c.matching_complete,
            "exhaustive_within_scope": c.exhaustive_within_scope,
        }

    def _query_pages(self, ids, *, complete=False):
        if len(set(ids)) != len(ids):
            raise ResearchError("query_receipt_duplicate")
        parsed = [self._query_page(key) for key in ids]
        definitions = [self._query_identity(q) for _, q, _ in parsed]
        if definitions and any(d != definitions[0] for d in definitions[1:]):
            raise ResearchError("query_receipt_mixed")
        cursor = None
        seen = set()
        cursors = set()
        refs = []
        resultset = None
        total = None
        all_complete = True
        for _, request, result in parsed:
            c = result.completeness
            if request.page.cursor in cursors:
                raise ResearchError("query_receipt_duplicate")
            cursors.add(request.page.cursor)
            if request.page.cursor != cursor:
                raise ResearchError("query_receipt_incomplete")
            if resultset is None:
                resultset = c.result_set_id
                total = c.total_matches
            elif (c.result_set_id, c.total_matches) != (resultset, total):
                raise ResearchError("query_receipt_mixed")
            if not c.result_set_id or c.returned != len(result.results):
                raise ResearchError("query_receipt_invalid")
            all_complete = all_complete and result.status == "ok" and c.matching_complete and c.exhaustive_within_scope
            for row in result.results:
                ref = row.ref.model_dump(mode="json")
                key = fingerprint(ref)
                if key in seen:
                    raise ResearchError("query_receipt_duplicate")
                seen.add(key)
                refs.append(ref)
            cursor = c.next_cursor
        if complete:
            if not parsed:
                raise ResearchError("query_selection_required")
            if not all_complete or cursor is not None or total != len(refs):
                raise ResearchError("query_receipt_incomplete")
            if not refs:
                raise ResearchError("query_receipt_empty")
            if len(refs) > 20:
                raise ResearchError("query_receipt_limit")
        return [raw for raw, _, _ in parsed], refs

    def _selected_contexts(self, receipt_ids):
        _, refs = self._query_pages(receipt_ids, complete=True)
        contexts = {}
        for key, entry in self.registry.entries.items():
            if entry["kind"] != "context" or entry["layer"] != "record":
                continue
            ref = entry.get("ref", {})
            if ref.get("kind") == "sample_test_context":
                contexts.setdefault(canonical(ref), key)
        try:
            return [contexts[canonical(ref)] for ref in refs]
        except KeyError:
            raise ResearchError("query_receipt_unread") from None

    def _validate_selection(self, data):
        selection = data.get("selection_request")
        if selection is None:
            return  # Historical explicit tasks keep their wire.
        args = data["arguments"]
        if data["name"] != "research_constraints" or selection != {
            "context_ids": [],
            "query_receipt_ids": args["query_receipt_ids"],
        }:
            raise ResearchError("stale_context")
        _, refs = self._query_pages(selection["query_receipt_ids"], complete=True)
        actual = [self.tools._context(key).model_dump(mode="json") for key in args["context_ids"]]
        if actual != refs:
            raise ResearchError("stale_context")

    def target(self, selections):
        if self.user_message is None:
            raise ResearchError("not_ready")
        try:
            target = bind_user_target(self.user_message, selections).model_dump(mode="json")
        except ValueError:
            raise ResearchError("target_clarification") from None
        return self.register(
            "user_target", target, text=render_target(target), message_id=target["message"]["message_id"]
        )

    def target_quote(self, selections):
        from .research_target_wire import quote_selections

        if self.user_message is None:
            raise ResearchError("not_ready")
        offsets = quote_selections(self.user_message, selections)
        try:
            return self.target(offsets)
        except ResearchError as error:
            if error.code == "target_clarification":
                raise ResearchError("target_clause_unsupported") from None
            raise

    def _target(self, key):
        self.registry.item(key, "user_target")
        target = UserTarget.model_validate(self.read(key))
        mid = target.message.message_id
        actual = (
            self.user_message
            if self.user_message is not None and self.user_message.message_id == mid
            else self.message_reader(mid)
            if self.message_reader
            else None
        )
        if actual is None or UserMessage.model_validate(actual) != target.message:
            raise ResearchError("stale_context")
        if render_target(target.model_dump(mode="json")) != self.registry.item(key)["text"]:
            raise ResearchError("stale_context")
        return target

    def _rows(self, ids):
        rows = []
        seen = set()
        for key in ids:
            self.check()
            ref = self.tools._context(key)
            identity = fingerprint(ref.model_dump())
            if identity in seen:
                raise ResearchError("invalid_request")
            seen.add(identity)
            self.registry._owner(ref.model_dump(mode="json"), self.tools.scope("record"))
            result = self.knowledge.get(
                GetRequest(
                    schema_version=VERSION,
                    request_id="research-task-context",
                    target=ref,
                    use_policy=self.scope()["use_policy"],
                    view="location",
                )
            )
            actual = {
                "workspace_id": result.workspace_id,
                "collection_id": result.scope.collection_id,
                "corpus_snapshot_id": result.versions.corpus_snapshot_id,
                "use_policy": result.scope.use_policy,
            }
            self.registry._scope("record", actual)
            if result.status != "ok" or len(result.results) != 1 or result.results[0].ref != ref:
                raise ResearchError("stale_context")
            rows.append(result.results[0])
        return rows

    def _comparisons(self, ids, fresh):
        receipts = []
        dependencies = []
        seen = set()
        for key in ids:
            _entry = self.registry.item(key, "comparison")
            receipt = next(
                (
                    r
                    for r in self.registry.entries.values()
                    if r["kind"] == "comparison_receipt" and key in r["comparison_ids"]
                ),
                None,
            )
            if receipt is None:
                raise ResearchError("stale_context")
            if receipt["id"] in seen:
                continue
            seen.add(receipt["id"])
            data = self.read(receipt["id"])
            if fresh:
                result = self.knowledge.compare(CompareRequest.model_validate(data["request"]))
                if (
                    result.status != "ok"
                    or [p.model_dump(mode="json") for p in result.comparisons] != data["response"]["comparisons"]
                ):
                    raise ResearchError("stale_context")
            receipts.append(data["response"])
            dependencies.extend([key, receipt["id"]])
        return receipts, dependencies

    def _compute(self, name, args, *, fresh):
        rows = self._rows(args["context_ids"])
        deps = list(args["context_ids"])
        scope = self.scope()
        if name == "research_reference":
            comparisons, extra = self._comparisons(args["comparison_ids"], fresh)
            deps += extra
            out = performance_reference(rows, scope=scope, comparison_results=comparisons)
        else:
            target = self._target(args["target_id"])
            deps.append(args["target_id"])
            if name == "research_plan":
                out = research_plan(target, rows, scope=scope)
            elif name == "research_constraints":
                pages, _ = self._query_pages(args["query_receipt_ids"])
                deps.extend(args["query_receipt_ids"])
                out = evaluate_constraints(target, rows, scope=scope, query_pages=pages)
                if fresh and out.coverage.exhaustive:
                    self._fresh_coverage(pages, rows)
            else:
                raise ResearchError("invalid_request")
        return out.model_dump(mode="json"), list(dict.fromkeys(deps))

    def _fresh_coverage(self, pages, rows):
        # Cursors expire. Re-run the same immutable query definition from its
        # first page, never treating a stored cursor as current coverage proof.
        query = QueryRequest.model_validate(pages[0]["request"])
        seen = set()
        cursor = None
        total = None
        for _ in range(20):
            self.check()
            query = query.model_copy(update={"page": query.page.model_copy(update={"cursor": cursor})})
            result = self.knowledge.query(query)
            c = result.completeness
            self.registry._scope(
                "record",
                {
                    "workspace_id": result.workspace_id,
                    "collection_id": result.scope.collection_id,
                    "corpus_snapshot_id": result.versions.corpus_snapshot_id,
                    "use_policy": result.scope.use_policy,
                },
            )
            if (
                result.status != "ok"
                or not c.matching_complete
                or not c.exhaustive_within_scope
                or c.returned != len(result.results)
            ):
                raise ResearchError("stale_context")
            if total is None:
                total = c.total_matches
            elif total != c.total_matches:
                raise ResearchError("stale_context")
            for row in result.results:
                key = fingerprint(row.ref.model_dump())
                if key in seen:
                    raise ResearchError("stale_context")
                seen.add(key)
            cursor = c.next_cursor
            if cursor is None:
                if total != len(seen) or seen != {fingerprint(r.ref.model_dump()) for r in rows}:
                    raise ResearchError("stale_context")
                return
        raise ResearchError("context_limit")

    def execute(self, name, args):
        self.check()
        if name == "research_target":
            entry = self.target(args["selections"])
        else:
            data = {}
            if name == "research_constraints" and not args["context_ids"]:
                data["selection_request"] = {"context_ids": [], "query_receipt_ids": list(args["query_receipt_ids"])}
                args = {**args, "context_ids": self._selected_contexts(args["query_receipt_ids"])}
            output, deps = self._compute(name, args, fresh=True)
            if name == "research_plan":
                data["presentation_version"] = "plan-v2"
            entry = self.register(
                "task_result",
                {"name": name, "arguments": args, "output": output, **data},
                dependencies=deps,
                text=render_task(output, self.registry, presentation_version=data.get("presentation_version")),
                task_kind=output["kind"],
            )
        return {"items": [entry], "clipped": False}

    def validate(self, ids):
        for key in self.registry.closure(ids):
            entry = self.registry.item(key)
            if entry["kind"] == "user_target":
                self._target(key)
            elif entry["kind"] == "task_result":
                data = self.read(key)
                self._validate_selection(data)
                version = data.get("presentation_version")
                if "presentation_version" in data and (data["name"] != "research_plan" or version != "plan-v2"):
                    raise ResearchError("stale_context")
                out, deps = self._compute(data["name"], data["arguments"], fresh=True)
                if (out, deps, render_task(out, self.registry, presentation_version=version)) != (
                    data["output"],
                    entry["dependency_ids"],
                    entry["text"],
                ):
                    raise ResearchError("stale_context")
            elif entry["kind"] in {"query_receipt", "comparison_receipt"}:
                self.read(key)
        self.check()
