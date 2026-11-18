"""Trusted, read-only materials query service using K02 published-record ports."""

from __future__ import annotations

import secrets
import time
from contextvars import ContextVar
from functools import wraps
from itertools import combinations

from .contract import FIELDS, PROFILES
from .core import canonical, fingerprint
from .knowledge_context import context_assertions
from .knowledge_contracts import (
    VERSION,
    AssertionRef,
    Capabilities,
    CatalogueEntry,
    CompareRequest,
    Completeness,
    ContextValidateRequest,
    ContextValidateResult,
    CorpusRequest,
    EvidenceData,
    EvidenceRef,
    Exclusion,
    FieldCapability,
    GetRequest,
    IndexBuildRequest,
    IndexBuildResult,
    IndexStatusRequest,
    IndexStatusResult,
    KnowledgeResult,
    LexicalCandidate,
    PassageBrowseRequest,
    ProfileRef,
    QueryRequest,
    RecordCatalogueRequest,
    RecordCatalogueResult,
    RecordRef,
    RecordResult,
    Retrieval,
    SampleTestContextRef,
    ScientificStatus,
    Scope,
    SearchRequest,
    SourcePassageRef,
    SourceSnapshotRef,
    TableAnalysisArtifactRef,
    TableSnapshotRef,
    ValueProjection,
    Versions,
    WikiRevisionRef,
    safe_error,
)
from .knowledge_domain import load_comparison_profiles, load_domain_profile, resolve_comparison_profile
from .knowledge_results import KnowledgeResultStore
from .knowledge_snapshot import KnowledgeIntegrityError
from .query import query_package_detailed

PROJECTION_VERSION = "knowledge-projection-3"
READ_CANCELED = ContextVar("knowledge_read_canceled", default=None)
READ_DEADLINE = ContextVar("knowledge_read_deadline", default=None)


def bounded_read(method):
    """Cooperative total deadline; external reads retain client I/O timeouts.

    Await actual work completion rather than abandoning a timed-out thread.
    A late read fails closed and no later stage executes after the deadline.
    """

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        deadline = time.monotonic() + self.timeout_seconds
        inherited = READ_DEADLINE.get()
        if inherited is not None:
            deadline = min(deadline, inherited)
        token = READ_DEADLINE.set(deadline)
        try:
            self._deadline(deadline)
            try:
                result = method(self, *args, **kwargs)
            except KnowledgeIntegrityError:
                self._deadline(deadline)
                raise
            self._deadline(deadline)
            return result
        finally:
            READ_DEADLINE.reset(token)

    return wrapped


OWNER = (
    "workspace_id",
    "corpus_snapshot_id",
    "task_id",
    "source_id",
    "source_revision_id",
    "record_revision_id",
    "record_snapshot_id",
    "package_sha256",
)


class KnowledgeService:
    def __init__(
        self,
        workspace_id,
        registry,
        adapters,
        snapshot_store,
        *,
        source_resolver=None,
        table_artifact_adapter=None,
        wiki_adapter=None,
        clock=time.time,
        timeout_seconds=30,
    ):
        self.workspace_id = workspace_id
        self.registry = registry
        self.adapters = dict(adapters)
        self.snapshots = snapshot_store
        self.source_resolver = source_resolver
        self.table_artifact_adapter = table_artifact_adapter
        self.wiki_adapter = wiki_adapter
        self.documents = None
        self.lexical_index = None
        self._index_bundles = {}
        self.clock = clock
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Bounded positive read timeout required")
        self.timeout_seconds = timeout_seconds
        self.results = KnowledgeResultStore(snapshot_store, clock=clock)
        self.resources = {}
        for collection, adapter in self.adapters.items():
            registry.resolve(workspace_id, collection)
            if adapter.workspace_id != workspace_id or adapter.collection_id != collection:
                raise ValueError("Trusted adapter scope mismatch")

    def _adapter(self, collection):
        self.registry.resolve(self.workspace_id, collection)
        if collection not in self.adapters:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        return self.adapters[collection]

    def _ref_adapter(self, ref):
        manifest = self._ref_manifest(ref)
        return (self._adapter(manifest["collection_id"]), manifest)

    def _ref_manifest(self, ref):
        if ref.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        manifest = self.snapshots.read_corpus(ref.corpus_snapshot_id)
        if self.documents is not None:
            self.documents.refresh()
        self.registry.check_manifest(manifest)
        if manifest["workspace_id"] != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        return manifest

    def _domain(self, domain):
        if domain != load_domain_profile().id:
            raise KnowledgeIntegrityError("DOMAIN_FILTER_UNSUPPORTED")

    def _deadline(self, deadline):
        canceled = READ_CANCELED.get()
        if canceled is not None and canceled.is_set():
            raise KnowledgeIntegrityError("REQUEST_CANCELED")
        inherited = READ_DEADLINE.get()
        if inherited is not None:
            deadline = min(deadline, inherited)
        if time.monotonic() >= deadline:
            raise KnowledgeIntegrityError("QUERY_TIMEOUT")

    @bounded_read
    def capabilities(self):
        if self.documents is not None:
            self.documents.refresh()
        domain = load_domain_profile()
        profiles = load_comparison_profiles()
        source_vocabulary = []
        for adapter in self.adapters.values():
            for (task, digest), stamp in list(adapter._verified.items()):
                if 0 <= adapter.clock() - stamp <= adapter.cache_ttl_seconds:
                    try:
                        package = adapter.read_saved_package(task, digest, mode="verified_cache")
                        source_vocabulary.extend(
                            {"field_id": a["field_id"], "qualifiers": a.get("qualifiers", {})}
                            for a in package["assertions"]
                        )
                    except KnowledgeIntegrityError:
                        continue
        unique = {fingerprint(x): x for x in source_vocabulary}
        return Capabilities(
            schema_version=VERSION,
            workspace_id=self.workspace_id,
            readiness="ready",
            fields=[
                FieldCapability(
                    field_id=f,
                    qualifier_keys=sorted(
                        {k for p in PROFILES.values() if p["field_id"] == f for k in p["qualifiers"]}
                    ),
                    ops=["eq", "contains", "exists", "gte", "gt", "lte", "lt"],
                    value_types=["number", "text", "category", "date", "time", "datetime", "duration"],
                )
                for f in sorted(FIELDS)
            ],
            domain_profiles=[domain.id],
            comparison_profiles=[ProfileRef(id=p.id, version=p.version, sha256=p.sha256) for p in profiles],
            collections=sorted(
                set(self.adapters) | ({self.wiki_adapter.collection_id} if self.wiki_adapter else set())
            ),
            policies=["source_reviewed", "saved_extractions", "history"],
            index_status="not_ready",
            unsupported=["semantic_search", "numeric_delta", "category_compare", "rank_observed"]
            + ([] if self.wiki_adapter else ["wiki"]),
            exact_query=True,
            lexical_index=self.lexical_index is not None,
            lexical_index_layers=["record", "source_passage", "wiki"] if self.lexical_index is not None else [],
            source_passage=self.source_resolver is not None,
            wiki=self.wiki_adapter is not None,
            wiki_search="lexical_current_topics" if self.wiki_adapter else None,
            comparison_operations=["side_by_side", "condition_diff"],
            catalogue_vocabulary={"version": domain.catalogue_version, "identity": "catalogue_declared"},
            source_vocabulary={
                "identity": "observed_source_qualifiers",
                "entries": [unique[k] for k in sorted(unique)],
            },
            mappings=[m.model_dump(mode="json") for m in domain.mappings],
        )

    @bounded_read
    def source_navigation(self, source_ids):
        """Bounded labels for explicit selected IDs; never a global inventory."""
        if len(source_ids) > 100 or len(set(source_ids)) != len(source_ids):
            raise KnowledgeIntegrityError("INVALID_QUERY")
        if self.documents is None:
            return {"items": [], "clipped": False}
        result = {"items": [], "clipped": False}
        for sid in source_ids:
            self._deadline(READ_DEADLINE.get())
            _, task = self.documents.member(sid)
            label = ", ".join(self.documents.service.store.get_asset(a)["original_name"] for a in task["asset_ids"])[
                :512
            ]
            item = {"source_id": sid, "label": label or sid}
            if len(canonical({**result, "items": result["items"] + [item]}).encode()) <= 8000:
                result["items"].append(item)
            else:
                result["clipped"] = True
        return result

    def _evidence(self, ref, ids, package):
        evidence = {e["evidence_id"]: e for e in package["evidence"]}
        assets = {a["asset_id"]: a for a in package["assets"]}
        result = []
        for eid in sorted(set(ids)):
            if eid not in evidence or evidence[eid]["asset_ref"] not in assets:
                raise KnowledgeIntegrityError()
            result.append(
                EvidenceRef(
                    kind="evidence",
                    **{k: getattr(ref, k) for k in OWNER},
                    evidence_id=eid,
                    asset_sha256=assets[evidence[eid]["asset_ref"]]["sha256"],
                )
            )
        return result

    def _values(self, assertions, record=None, sample_ref=None):
        from .process_context import process_index, process_paths

        objects = {o["object_id"]: o for o in record.projected_package["objects"]} if record else {}
        paths = {}
        samples = [sample_ref] if sample_ref else [o for o in objects if objects[o]["kind"] == "sample"]
        process_lookup = process_index(record.projected_package, objects=objects) if record else None
        for sample in samples:
            for subject, entries in process_paths(record.projected_package, sample, index=process_lookup).items():
                paths.setdefault(subject, []).extend(entries)
        result = []
        for a in assertions:
            v = a["value"]
            typed = v.get("typed_value")
            kind = typed.get("type") if isinstance(typed, dict) else None
            vt = kind if kind in {"number", "text", "category", "date", "time", "datetime", "duration"} else None
            provenance = []
            for path in paths.get(a["subject_ref"], []) if a["field_id"].startswith("process.") else []:
                relations = [
                    {k: r[k] for k in ("relation_id", "predicate", "from_ref", "to_ref", "basis_kind", "applicability")}
                    | {"evidence_refs": self._evidence(record.ref, r["evidence_refs"], record.projected_package)}
                    for r in path["relations"]
                ]
                provenance.append(
                    {**path, "relation_ids": [r["relation_id"] for r in relations], "relations": relations}
                )
            obj = objects.get(a["subject_ref"], {})
            result.append(
                ValueProjection(
                    assertion_id=a["assertion_id"],
                    field_id=a["field_id"],
                    qualifiers=a.get("qualifiers", {}),
                    raw_value=v.get("raw_value"),
                    unit=v.get("raw_unit"),
                    basis=v.get("raw_basis"),
                    typed_value_version=v.get("typed_value_version"),
                    value_type=vt,
                    typed_value=typed,
                    typed_values=v.get("typed_values"),
                    statement_kind=a.get("statement_kind", "unknown")
                    if a.get("statement_kind")
                    in {
                        "source_report",
                        "measured",
                        "source_reported_measurement",
                        "declared_range",
                        "scope_claim",
                        "source_claim",
                        "author_interpretation",
                        "source_reported_calculation",
                    }
                    else "unknown",
                    subject_ref=a["subject_ref"],
                    subject_kind=obj.get("kind"),
                    subject_label=obj.get("label"),
                    evidence_refs=self._evidence(record.ref, a["evidence_refs"], record.projected_package)
                    if record
                    else [],
                    context_paths=provenance,
                )
            )
        return result

    def _row(self, record, row, assertions=None):
        base = record.ref.model_dump()
        sample = row.get("sample_ref")
        representable = sample and (not row.get("run_ref")) and (not row.get("series_ref"))
        if representable:
            ref = SampleTestContextRef.model_validate(
                {**base, "kind": "sample_test_context", "sample_ref": sample, "test_ref": row.get("test_ref")}
            )
        else:
            ref = record.ref
        selected = (
            [a for a in record.projected_package["assertions"] if a["assertion_id"] in row.get("assertion_refs", [])]
            if assertions is None
            else assertions
        )
        identity = {
            **base,
            **{k: row.get(k) for k in ("record_ref", "sample_ref", "test_ref", "run_ref", "series_ref")},
        }
        return RecordResult(
            kind=ref.kind,
            result_id=fingerprint(identity),
            ref=ref,
            label=row.get("sample_label") or row.get("record_label"),
            status=ScientificStatus(source_review=record.source_review, currently_eligible=record.currently_eligible),
            storage=record.storage,
            integrity=record.integrity,
            values=self._values(selected, record, sample),
            evidence_refs=self._evidence(
                ref, [e for a in selected for e in a["evidence_refs"]], record.projected_package
            ),
            record_subject_ref=row.get("record_ref"),
            run_ref=row.get("run_ref"),
            series_ref=row.get("series_ref"),
        )

    def _response(
        self,
        request,
        operation,
        corpus,
        manifest,
        rows=(),
        comparisons=(),
        exclusions=(),
        *,
        resultset=None,
        next_cursor=None,
        total=None,
    ):
        failed = sorted({f["source_id"] for f in manifest.get("failures", [])})
        read = set(manifest["sources"]) - set(failed)
        partial = bool(failed)
        return KnowledgeResult(
            schema_version=VERSION,
            request_id=request.request_id,
            status="partial" if partial else "ok",
            operation=operation,
            workspace_id=self.workspace_id,
            domain_profile=load_domain_profile().id,
            versions=Versions(
                query_policy="knowledge-query-2",
                domain_vocabulary=load_domain_profile().catalogue_version,
                projection=PROJECTION_VERSION,
                corpus_snapshot_id=corpus,
                corpus_manifest_sha256=corpus,
            ),
            scope=Scope(
                collection_id=manifest["collection_id"],
                source_selection="history" if manifest["use_policy"] == "history" else "pinned",
                use_policy=request.use_policy,
                source_count=len(set(manifest["sources"])),
                sources_read=len(read),
                sources_failed=len(failed),
                failed_source_ids=failed,
                version_freshness="pinned",
                safety_policy_checked_at=str(self.clock()),
            ),
            completeness=Completeness(
                matching_complete=not partial,
                exhaustive_within_scope=not partial,
                total_matches=None if partial else total,
                returned=len(rows) + len(comparisons),
                has_more=next_cursor is not None,
                next_cursor=next_cursor,
                result_set_id=resultset,
            ),
            results=list(rows),
            comparisons=list(comparisons),
            exclusions=list(exclusions),
            errors=[
                safe_error(
                    "ARCHIVE_UNAVAILABLE",
                    request.request_id,
                    retryable=True,
                    remedy="retry_later",
                    affected_refs=[
                        RecordRef(
                            kind="record_revision",
                            workspace_id=self.workspace_id,
                            corpus_snapshot_id=corpus,
                            **{k: f[k] for k in OWNER if k not in {"workspace_id", "corpus_snapshot_id"}},
                        )
                        for f in manifest.get("failures", [])
                        if f["source_id"] == sid
                    ],
                )
                for sid in failed
            ],
        )

    @bounded_read
    def query(self, request):
        request = QueryRequest.model_validate(request.model_dump())
        self._domain(request.domain_profile)
        if not request.filters or (
            request.selection is not None and (not request.selection or len(request.selection) > 200)
        ):
            raise KnowledgeIntegrityError("INVALID_QUERY")
        adapter = self._adapter(request.collection_id)
        deadline = time.monotonic() + self.timeout_seconds
        semantic = request.model_dump(mode="json", exclude={"request_id", "corpus_snapshot_id", "page"})
        semantic["page_size"] = request.page.size
        semantic["query_policy"] = "knowledge-query-2"
        semantic["projection"] = PROJECTION_VERSION
        digest = fingerprint(semantic)
        if request.page.cursor:
            (resultset, offset, payload) = self.results.read(request.page.cursor, digest)
            corpus = payload["corpus"]
            if request.corpus_snapshot_id and request.corpus_snapshot_id != corpus:
                raise KnowledgeIntegrityError("CURSOR_MISMATCH")
            adapter.read_corpus(corpus, use_policy=request.use_policy, mode="live_readback")
            self._deadline(deadline)
        else:
            corpus = request.corpus_snapshot_id or adapter.build_corpus(
                use_policy=request.use_policy,
                include_history=request.include_history,
                allow_partial=request.allow_partial,
            )
            self._deadline(deadline)
            manifest = adapter.corpus_manifest(corpus)
            if manifest.get("failures") and (not request.allow_partial):
                raise KnowledgeIntegrityError("ARCHIVE_UNAVAILABLE")
            # Explicit pinned queries may resume after TTL or process restart.
            # Refresh only that immutable corpus; never select a newer head.
            read_mode = "live_readback" if request.corpus_snapshot_id else "verified_cache"
            records = adapter.read_corpus(corpus, use_policy=request.use_policy, mode=read_mode)
            self._deadline(deadline)
            if request.selection:
                for ref in request.selection:
                    adapter.resolve(ref, use_policy=request.use_policy, mode="verified_cache")
            rows = {}
            exclusions = []
            filters = [f.model_dump(exclude_none=True) for f in request.filters]
            for record in records:
                self._deadline(deadline)
                detail = query_package_detailed(record.projected_package, filters)
                for raw in detail["results"]:
                    row = self._row(record, raw)
                    if request.selection and row.ref not in request.selection:
                        continue
                    rows[row.result_id] = row.model_dump(mode="json")
                for exclusion in detail["exclusions"]:
                    ref = AssertionRef.model_validate(
                        {**record.ref.model_dump(), "kind": "assertion", "assertion_id": exclusion["assertion_id"]}
                    )
                    exclusions.append(
                        Exclusion(
                            ref=ref, filter_index=exclusion["filter_index"], reason=exclusion["reason"]
                        ).model_dump(mode="json")
                    )
            self._deadline(deadline)
            payload = {
                "fingerprint": digest,
                "corpus": corpus,
                "rows": [rows[k] for k in sorted(rows)],
                "exclusions": exclusions,
            }
            resultset = self.results.put(payload)
            offset = 0
        adapter.read_corpus(corpus, use_policy=request.use_policy, mode="live_readback")
        self._deadline(deadline)
        manifest = adapter.corpus_manifest(corpus)
        rows = payload["rows"]
        end = min(offset + request.page.size, len(rows))
        self._deadline(deadline)
        cursor = self.results.cursor(resultset, end) if end < len(rows) else None
        return self._response(
            request,
            "query",
            corpus,
            manifest,
            [RecordResult.model_validate(r) for r in rows[offset:end]],
            exclusions=[Exclusion.model_validate(e) for e in payload["exclusions"]],
            resultset=resultset,
            next_cursor=cursor,
            total=len(rows),
        )

    def _sanitize_package(self, package):
        from copy import deepcopy

        result = {k: deepcopy(package[k]) for k in ("objects", "relations", "assertions", "evidence")}
        result["header"] = {k: package["header"][k] for k in ("source_id", "source_revision_id", "record_revision_id")}
        result["assets"] = [
            {k: a[k] for k in ("asset_id", "sha256", "media_type", "size_bytes") if k in a} for a in package["assets"]
        ]
        result["sources"] = [
            {k: s[k] for k in ("source_id", "source_type", "title") if k in s} for s in package.get("sources", [])
        ]

        def safe(value):
            if isinstance(value, dict):
                return {
                    k: safe(v)
                    for (k, v) in value.items()
                    if not any(
                        term in k.casefold()
                        for term in (
                            "path",
                            "token",
                            "secret",
                            "password",
                            "credential",
                            "receipt",
                            "url",
                            "api_key",
                        )
                    )
                    and k not in {"reader_options", "runtime_config"}
                }
            if isinstance(value, list):
                return [safe(v) for v in value]
            return value

        return safe(result)

    @bounded_read
    def document_sources(self):
        if self.documents is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        return self.documents.inventory()

    @bounded_read
    def freeze_corpus(self, request):
        request = CorpusRequest.model_validate(request.model_dump())
        if self.documents is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        return self.documents.freeze(request)

    @bounded_read
    def catalogue(self, request):
        request = RecordCatalogueRequest.model_validate(request.model_dump())
        adapter, manifest = self._ref_adapter(request.target)
        resolved = adapter.resolve(request.target, use_policy=request.use_policy)
        record = (
            resolved
            if isinstance(request.target, RecordRef) and request.target.kind == "record_revision"
            else resolved["record"]
        )
        semantic = request.model_dump(mode="json", exclude={"request_id", "page"})
        semantic.update(page_size=request.page.size, projection=PROJECTION_VERSION)
        digest = fingerprint(semantic)
        if request.page.cursor:
            resultset, offset, payload = self.results.read(request.page.cursor, digest)
        else:
            package = record.projected_package
            objects = {o["object_id"]: o for o in package["objects"]}
            base = record.ref.model_dump(mode="json")
            entries = []
            assertions = (
                context_assertions(record, request.target)
                if isinstance(request.target, SampleTestContextRef)
                else package["assertions"]
            )
            if request.kind == "contexts":
                from .query import query_context_candidates

                for grain in query_context_candidates(package):
                    if not grain["sample_ref"] or grain["run_ref"] or grain["series_ref"]:
                        continue
                    ref = SampleTestContextRef.model_validate(
                        {
                            **base,
                            "kind": "sample_test_context",
                            "sample_ref": grain["sample_ref"],
                            "test_ref": grain["test_ref"],
                        }
                    )
                    if isinstance(request.target, SampleTestContextRef) and ref != request.target:
                        continue
                    entries.append(
                        CatalogueEntry(
                            ref=ref,
                            label=objects[grain["sample_ref"]].get("label"),
                            subject_ref=grain["test_ref"] or grain["sample_ref"],
                            subject_kind=objects[grain["test_ref"] or grain["sample_ref"]]["kind"],
                        )
                    )
            elif request.kind == "assertions":
                for assertion in assertions:
                    subject = objects[assertion["subject_ref"]]
                    entries.append(
                        CatalogueEntry(
                            ref=AssertionRef.model_validate(
                                {**base, "kind": "assertion", "assertion_id": assertion["assertion_id"]}
                            ),
                            label=subject.get("label"),
                            subject_ref=assertion["subject_ref"],
                            subject_kind=subject["kind"],
                            field_id=assertion["field_id"],
                            qualifiers=assertion.get("qualifiers", {}),
                        )
                    )
            else:
                for ref in self._evidence(record.ref, [e for a in assertions for e in a["evidence_refs"]], package):
                    entries.append(CatalogueEntry(ref=ref))
            payload = {
                "fingerprint": digest,
                "entries": [e.model_dump(mode="json") for e in sorted(entries, key=lambda e: e.ref.model_dump_json())],
            }
            resultset = self.results.put(payload)
            offset = 0
        end = min(offset + request.page.size, len(payload["entries"]))
        entries = [CatalogueEntry.model_validate(e) for e in payload["entries"][offset:end]]
        adapter.resolve(request.target, use_policy=request.use_policy)
        cursor = self.results.cursor(resultset, end) if end < len(payload["entries"]) else None
        envelope = self._response(
            request,
            "get",
            request.target.corpus_snapshot_id,
            manifest,
            resultset=resultset,
            next_cursor=cursor,
            total=len(payload["entries"]),
        )
        result = RecordCatalogueResult(
            schema_version=VERSION,
            request_id=request.request_id,
            workspace_id=self.workspace_id,
            corpus_snapshot_id=request.target.corpus_snapshot_id,
            kind=request.kind,
            scope=envelope.scope,
            completeness=envelope.completeness.model_copy(update={"returned": len(entries)}),
            entries=entries,
        )
        if len(result.model_dump_json().encode()) > 262144:
            raise KnowledgeIntegrityError("INVALID_QUERY")
        return result

    @bounded_read
    def passages(self, request):
        request = PassageBrowseRequest.model_validate(request.model_dump())
        if self.source_resolver is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        manifest = self.snapshots.read_corpus(request.corpus_snapshot_id)
        if self.documents is not None:
            self.documents.refresh()
        self.registry.check_manifest(manifest)
        if manifest["workspace_id"] != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        if manifest["collection_id"] != self.source_resolver.collection_id:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        if manifest["use_policy"] != request.use_policy or "source_snapshots" not in manifest:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        if request.source_id not in manifest["sources"]:
            raise KnowledgeIntegrityError("SOURCE_USE_POLICY_DENIED")
        sources = [SourceSnapshotRef.model_validate(s) for s in manifest["source_snapshots"]]
        for source in sources:
            self.source_resolver._frozen(source, request.use_policy)
            self._deadline(float("inf"))
        semantic = request.model_dump(mode="json", exclude={"request_id", "page"})
        semantic.update(page_size=request.page.size, source_read_policy="website-source-read-1")
        digest = fingerprint(semantic)
        if request.page.cursor:
            resultset, offset, payload = self.results.read(request.page.cursor, digest)
        else:
            rows = []
            for source in sources:
                if (
                    source.source_id != request.source_id
                    or request.asset_sha256
                    and source.source_asset_sha256 != request.asset_sha256
                ):
                    continue
                _, index, _ = self.source_resolver._frozen(source, request.use_policy)
                for segment in index["segments"]:
                    if request.physical_page is not None and segment["physical_page"] != request.physical_page:
                        continue
                    if request.segment_id is not None and segment["segment_id"] != request.segment_id:
                        continue
                    from hashlib import sha256

                    ref = SourcePassageRef(
                        kind="source_passage",
                        workspace_id=self.workspace_id,
                        corpus_snapshot_id=request.corpus_snapshot_id,
                        source_snapshot_ref=source,
                        segment_id=segment["segment_id"],
                        quote_sha256=sha256(segment["quote"].encode()).hexdigest(),
                        physical_page=segment["physical_page"],
                        location_status=segment["location_status"],
                        bbox=segment.get("bbox"),
                    )
                    rows.append(ref.model_dump(mode="json"))
                    self._deadline(float("inf"))
            payload = {"fingerprint": digest, "rows": rows}
            resultset = self.results.put(payload)
            offset = 0
        end = min(offset + request.page.size, len(payload["rows"]))
        rows = [
            self.source_resolver.resolve(SourcePassageRef.model_validate(r), use_policy=request.use_policy)
            for r in payload["rows"][offset:end]
        ]
        for source in sources:
            self.source_resolver._frozen(source, request.use_policy)
            self._deadline(float("inf"))
        cursor = self.results.cursor(resultset, end) if end < len(payload["rows"]) else None
        result = self._response(
            request,
            "passages",
            request.corpus_snapshot_id,
            manifest,
            rows,
            resultset=resultset,
            next_cursor=cursor,
            total=len(payload["rows"]),
        )
        if len(result.model_dump_json().encode()) > 262144:
            raise KnowledgeIntegrityError("INVALID_QUERY")
        return result

    @bounded_read
    def get(self, request):
        request = GetRequest.model_validate(request.model_dump())
        ref = request.target
        if isinstance(ref, WikiRevisionRef):
            if self.wiki_adapter is None:
                raise KnowledgeIntegrityError("WIKI_NOT_READY")
            row, manifest = self.wiki_adapter.resolve(ref, use_policy=request.use_policy)
            return self._response(request, "get", ref.corpus_snapshot_id, manifest, [row], total=1)
        if isinstance(ref, TableSnapshotRef):
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        manifest = self._ref_manifest(ref)
        if isinstance(ref, SourcePassageRef):
            if self.source_resolver is None:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            row = self.source_resolver.resolve(ref, use_policy=request.use_policy)
            handle = secrets.token_urlsafe(32)
            self._deadline(float("inf"))
            self.resources[handle] = (ref, request.use_policy, self.clock() + 300)
            row.resource_ref = handle
        elif isinstance(ref, TableAnalysisArtifactRef):
            adapter = self._adapter(manifest["collection_id"])
            if self.table_artifact_adapter is None:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            adapter.read_corpus(ref.corpus_snapshot_id, use_policy=manifest["use_policy"], mode="live_readback")
            row = self.table_artifact_adapter.resolve(ref, use_policy=request.use_policy)
        else:
            adapter = self._adapter(manifest["collection_id"])
            resolved = adapter.resolve(ref, use_policy=request.use_policy)
            record = resolved if ref.kind == "record_revision" else resolved.get("record")
            if record is None:
                record = adapter.resolve(
                    RecordRef(kind="record_revision", **{k: getattr(ref, k) for k in OWNER}),
                    use_policy=request.use_policy,
                    mode="verified_cache",
                )
            if isinstance(ref, SampleTestContextRef):
                assertions = context_assertions(record, ref)
            elif isinstance(ref, AssertionRef):
                assertions = [resolved["assertion"]]
            elif isinstance(ref, EvidenceRef):
                assertions = [
                    a for a in record.projected_package["assertions"] if ref.evidence_id in a["evidence_refs"]
                ]
            else:
                assertions = record.projected_package["assertions"]
            row = RecordResult(
                kind=ref.kind,
                result_id=fingerprint(ref.model_dump(mode="json")),
                ref=ref,
                label=resolved["sample"].get("label") if isinstance(ref, SampleTestContextRef) else None,
                status=ScientificStatus(
                    source_review=record.source_review, currently_eligible=record.currently_eligible
                ),
                storage=record.storage,
                integrity=record.integrity,
                values=self._values(assertions, record, getattr(ref, "sample_ref", None)),
                evidence_refs=self._evidence(
                    ref, [e for a in assertions for e in a["evidence_refs"]], record.projected_package
                ),
            )
            if request.view == "full":
                row.scientific_package = self._sanitize_package(record.projected_package)
            if isinstance(ref, EvidenceRef):
                evidence = resolved["evidence"]
                provenance = self.documents.evidence_provenance(record, evidence) if self.documents is not None else {}
                handle = secrets.token_urlsafe(32)
                row.evidence_data = EvidenceData(
                    anchor=evidence.get("anchor"),
                    content_kind=evidence.get("content_kind"),
                    quote=evidence.get("content"),
                    resource_ref=handle,
                    reader_settings_status="unknown",
                    **provenance,
                )
                self._deadline(float("inf"))
                self.resources[handle] = (ref, request.use_policy, self.clock() + 300)
        return self._response(request, "get", ref.corpus_snapshot_id, manifest, [row], total=1)

    @bounded_read
    def resource(self, handle):
        binding = self.resources.get(handle)
        if binding is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        (ref, policy, expires) = binding
        if self.clock() >= expires:
            raise KnowledgeIntegrityError("RESULT_SET_EXPIRED")
        manifest = self._ref_manifest(ref)
        if isinstance(ref, SourcePassageRef):
            self.source_resolver.resolve(ref, use_policy=policy)
            digest = ref.bbox.page_image_sha256 if ref.bbox else ref.source_snapshot_ref.source_asset_sha256
            return self.snapshots.get_blob(digest)
        resolved = self._adapter(manifest["collection_id"]).resolve(ref, use_policy=policy)
        return resolved["asset_bytes"]

    @bounded_read
    def preview(self, handle, page=None):
        """Render an existing opaque resource; never mint another resource."""
        from .knowledge_preview import render_passage_image, render_pdf, validate_bytes

        binding = self.resources.get(handle)
        if binding is None:
            raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
        ref, policy, expires = binding
        if self.clock() >= expires:
            raise KnowledgeIntegrityError("RESULT_SET_EXPIRED")
        manifest = self._ref_manifest(ref)
        self._deadline(float("inf"))
        if isinstance(ref, SourcePassageRef):
            if self.source_resolver is None:
                raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
            passage = self.source_resolver.resolve(ref, use_policy=policy)
            original = self.snapshots.get_blob(ref.source_snapshot_ref.source_asset_sha256)
            status = ScientificStatus(source_review=passage.source_review, currently_eligible=True)
            self._deadline(float("inf"))
            if ref.bbox is not None:
                pinned = self.snapshots.get_blob(ref.bbox.page_image_sha256)
                result = render_passage_image(ref, original, pinned, page, passage.quote, status)
            else:
                if ref.physical_page is None:
                    raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
                anchor = {"kind": "pdf", "page_numbers": [ref.physical_page], "location_status": ref.location_status}
                result = render_pdf(ref, original, anchor, page, passage.quote, status)
            self._deadline(float("inf"))
            self.source_resolver.resolve(ref, use_policy=policy)
            validate_bytes(
                self.snapshots.get_blob(ref.source_snapshot_ref.source_asset_sha256),
                ref.source_snapshot_ref.source_asset_sha256,
            )
            if ref.bbox is not None:
                validate_bytes(self.snapshots.get_blob(ref.bbox.page_image_sha256), ref.bbox.page_image_sha256)
        elif isinstance(ref, EvidenceRef):
            adapter = self._adapter(manifest["collection_id"])
            resolved = adapter.resolve(ref, use_policy=policy)
            record = adapter.resolve(
                RecordRef(kind="record_revision", **{k: getattr(ref, k) for k in OWNER}),
                use_policy=policy,
                mode="verified_cache",
            )
            evidence = resolved["evidence"]
            status = ScientificStatus(source_review=record.source_review, currently_eligible=record.currently_eligible)
            self._deadline(float("inf"))
            result = render_pdf(
                ref, resolved["asset_bytes"], evidence.get("anchor"), page, evidence.get("content"), status
            )
            self._deadline(float("inf"))
            # A revoked corpus member or altered original invalidates the render.
            adapter.resolve(ref, use_policy=policy)
        else:
            raise KnowledgeIntegrityError("PREVIEW_UNSUPPORTED")
        if self.clock() >= expires:
            raise KnowledgeIntegrityError("RESULT_SET_EXPIRED")
        self._deadline(float("inf"))
        return result

    @bounded_read
    def compare(self, request):
        request = CompareRequest.model_validate(request.model_dump())
        try:
            profile = resolve_comparison_profile(
                request.comparison_profile.id, request.comparison_profile.version, request.comparison_profile.sha256
            )
        except ValueError as exc:
            raise KnowledgeIntegrityError(str(exc)) from None
        (adapter, manifest) = self._ref_adapter(request.items[0])
        resolved = []
        deadline = time.monotonic() + self.timeout_seconds
        for ref in request.items:
            self._deadline(deadline)
            item = adapter.resolve(ref, use_policy=request.use_policy)
            resolved.append((ref, item["record"], context_assertions(item["record"], ref)))
        from .knowledge_comparability import compare_pair

        pairs = []
        for (left, lr, la), (right, rr, ra) in combinations(resolved, 2):
            self._deadline(deadline)

            def evidence(owner, ids):
                return self._evidence(owner, ids, lr.projected_package if owner == left else rr.projected_package)

            pairs.append(compare_pair(left, right, la, ra, profile, request.requested_operations, evidence))
        adapter.read_corpus(request.corpus_snapshot_id, use_policy=request.use_policy, mode="live_readback")
        self._deadline(deadline)
        return self._response(
            request, "compare", request.corpus_snapshot_id, manifest, comparisons=pairs, total=len(pairs)
        )

    def _index_authority(self):
        from .knowledge_index_authority import WebsiteIndexAuthority

        if self.lexical_index is None:
            raise KnowledgeIntegrityError("INDEX_NOT_READY")
        return WebsiteIndexAuthority(self, self.lexical_index)

    @bounded_read
    def index_rebuild(self, request):
        from .knowledge_index_contracts import IndexScope
        from .knowledge_index_store import IndexFailure

        request = IndexBuildRequest.model_validate(request.model_dump())
        scope = IndexScope.model_validate(request.scope.model_dump())
        if scope.workspace_id != self.workspace_id:
            raise KnowledgeIntegrityError("WORKSPACE_MISMATCH")
        deadline = min(READ_DEADLINE.get(), time.monotonic() + request.timeout_seconds)
        authority = self._index_authority()
        try:
            with authority.budget(deadline):
                snapshot = authority.prepare(scope, deadline=deadline)
                self._deadline(deadline)
                built = self.lexical_index.rebuild(
                    snapshot,
                    request_id=request.request_id,
                    authority=authority,
                    timeout=max(0.000001, deadline - time.monotonic()),
                )
            return IndexBuildResult(schema_version=VERSION, request_id=request.request_id, **built)
        except TimeoutError:
            raise KnowledgeIntegrityError("QUERY_TIMEOUT") from None
        except IndexFailure as exc:
            raise KnowledgeIntegrityError(exc.code) from None

    @bounded_read
    def index_status(self, request):
        from .knowledge_index_contracts import IndexScope
        from .knowledge_index_store import IndexFailure

        request = IndexStatusRequest.model_validate(request.model_dump())
        scope = IndexScope.model_validate(request.scope.model_dump())
        authority = self._index_authority()
        try:
            with authority.budget(READ_DEADLINE.get()):
                authority._corpus(scope)
                state = self.lexical_index.status(scope, deadline=READ_DEADLINE.get())
                if state["readiness"] == "ready":
                    try:
                        authority.current_manifest(scope, deadline=READ_DEADLINE.get())
                    except Exception:
                        state.update(readiness="stale", reason="source_validation_required")
            return IndexStatusResult(schema_version=VERSION, request_id=request.request_id, **state)
        except IndexFailure as exc:
            raise KnowledgeIntegrityError(exc.code) from None

    def _lexical_search(self, request):
        from .knowledge_index_contracts import IndexScope
        from .knowledge_index_store import IndexFailure

        if len(request.source_kinds) != 1 or not request.corpus_snapshot_id or request.allow_stale_index:
            raise KnowledgeIntegrityError("INVALID_QUERY")
        scope = IndexScope(
            workspace_id=self.workspace_id,
            layer=request.source_kinds[0],
            collection_id=request.collection_id,
            use_policy=request.use_policy,
            corpus_snapshot_id=request.corpus_snapshot_id,
        )
        authority = self._index_authority()
        deadline = min(READ_DEADLINE.get(), time.monotonic() + 10)
        try:
            with authority.budget(deadline):
                manifest = authority._corpus(scope)
                result = self.lexical_index.search(
                    scope,
                    request.query,
                    authority=authority,
                    top_k=request.top_k,
                    timeout=max(0.000001, deadline - time.monotonic()),
                )
                rows = []
                for hit in result["results"]:
                    display = authority.display(scope, hit, deadline)
                    rows.append(
                        LexicalCandidate(
                            result_id=fingerprint(hit["ref"]),
                            **{
                                k: v
                                for k, v in hit.items()
                                if k not in {"source_identity", "snippet", "snippet_truncated"}
                            },
                            **display,
                        )
                    )
                response = self._response(request, "get", scope.corpus_snapshot_id, manifest, rows)
                response.operation = "search"
                response.completeness.matching_complete = False
                response.completeness.exhaustive_within_scope = False
                response.completeness.total_matches = None
                response.retrieval = Retrieval(
                    mode="lexical",
                    candidate_count=len(rows),
                    index_generation=result["generation"] or "not-ready",
                    corpus_snapshot_id=scope.corpus_snapshot_id,
                    readiness=result["readiness"],
                    reason=result["reason"],
                )
                return KnowledgeResult.model_validate(response.model_dump())
        except TimeoutError:
            raise KnowledgeIntegrityError("QUERY_TIMEOUT") from None
        except IndexFailure as exc:
            raise KnowledgeIntegrityError(exc.code) from None

    @bounded_read
    def search(self, request):
        request = SearchRequest.model_validate(request.model_dump())
        self._domain(request.domain_profile)
        if self.lexical_index is not None and request.corpus_snapshot_id:
            return self._lexical_search(request)
        if self.wiki_adapter is not None and request.source_kinds == ["wiki"]:
            rows, corpus, manifest, candidates = self.wiki_adapter.search(request)
            result = self._response(request, "get", corpus, manifest, rows)
            result.operation = "search"
            result.completeness.exhaustive_within_scope = False
            result.completeness.total_matches = None
            result.retrieval = Retrieval(
                mode="lexical", candidate_count=candidates, index_generation=corpus, corpus_snapshot_id=corpus
            )
            return KnowledgeResult.model_validate(result.model_dump())
        self._adapter(request.collection_id)
        raise KnowledgeIntegrityError("INDEX_NOT_READY")

    @bounded_read
    def validate_context(self, request):
        request = ContextValidateRequest.model_validate(request.model_dump())
        errors = []
        for ref in request.refs:
            try:
                self.get(
                    GetRequest(
                        schema_version=VERSION, request_id=request.request_id, use_policy=request.use_policy, target=ref
                    )
                )
            except KnowledgeIntegrityError as exc:
                errors.append(safe_error(exc.code, request.request_id, remedy="refresh_context", affected_refs=[ref]))
        return ContextValidateResult(
            schema_version=VERSION, request_id=request.request_id, valid=not errors, errors=errors
        )
