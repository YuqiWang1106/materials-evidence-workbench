"""Synthetic authority + real SQLite FTS; never provider or user storage."""

import importlib
import threading
import time
from copy import deepcopy
from hashlib import sha256

import pytest
from sqlalchemy import text


def core():
    try:
        return importlib.import_module("kb_ingest.knowledge_index_store")
    except ModuleNotFoundError as exc:
        pytest.fail(f"Lexical index core not implemented: {exc.name}")


def contracts():
    return importlib.import_module("kb_ingest.knowledge_index_contracts")


def record(n="one", **changes):
    return dict(
        kind="assertion",
        workspace_id="workspace-a",
        corpus_snapshot_id="corpus-a",
        task_id="task-" + n,
        source_id="source-" + n,
        source_revision_id="source-rev-" + n,
        record_revision_id="record-" + n,
        record_snapshot_id="snapshot-" + n,
        package_sha256="a" * 64,
        assertion_id="assertion-" + n,
        **changes,
    )


def scope(**changes):
    value = dict(
        workspace_id="workspace-a",
        layer="record",
        collection_id="collection-a",
        use_policy="source_reviewed",
        corpus_snapshot_id="corpus-a",
    )
    value.update(changes)
    return contracts().IndexScope(**value)


def document(n="one", body="ABS 极限氧指数 24.5 %，试样厚度 3.2 mm", **changes):
    value = dict(ref=record(n), content_kind="record_assertion", text=body, eligible=True)
    value.update(changes)
    return contracts().IndexDocument(**value)


class Authority:
    """An explicitly fake source boundary; all ranking/storage is production code."""

    def __init__(self, docs):
        self.docs = {d.ref.model_dump_json(): d for d in docs}
        self.manifest = {
            "source_manifest_version": "fixture-v1",
            "source_revision": "first",
            "index_documents": {d.key: d.document_hash for d in docs},
        }
        self.resolve_count = 0
        self.before_resolve = None

    def replace_docs(self, docs):
        self.docs = {d.ref.model_dump_json(): d for d in docs}
        self.manifest["index_documents"] = {d.key: d.document_hash for d in docs}

    def current_manifest(self, scope, *, deadline):
        return deepcopy(self.manifest)

    def resolve(self, scope, ref, content_kind, *, deadline):
        self.resolve_count += 1
        if self.before_resolve:
            self.before_resolve()
        return self.docs[ref.model_dump_json()]


@pytest.fixture
def setup(tmp_path):
    module = core()
    from kb_ingest.store import Store
    from kb_ingest.knowledge_index_tokens import IndexTokenizer

    store = Store(tmp_path)
    # Synthetic aliases make this test independent of any deployed domain profile.
    tokenizer = IndexTokenizer(alias_groups=[["sample code 7", "SC7"]], alias_version="test-aliases")
    index = module.KnowledgeIndexStore(store, tokenizer=tokenizer)
    docs = [document()]
    authority = Authority(docs)
    return index, authority, docs, store


def build(index, authority, docs, *, selected=None, request="build-one"):
    c = contracts()
    snapshot = c.IndexSnapshot(scope=selected or scope(), source_manifest=authority.manifest, documents=docs)
    return index.rebuild(snapshot, request_id=request, authority=authority)


def test_chinese_english_aliases_decimal_units_and_query_syntax():
    core()
    t = importlib.import_module("kb_ingest.knowledge_index_tokens").IndexTokenizer(
        alias_groups=[["厚度", "thickness"]], alias_version="test-aliases"
    )
    terms = t.tokens("ABS 阻燃性能 LOI 24.5% 3.2mm")
    assert {"abs", "阻", "燃", "阻燃", "24.5", "%", "3.2", "mm"} <= set(terms)
    assert set(t.tokens("厚度")) & set(t.tokens("thickness"))
    expression = t.match_expression('" OR * NEAR(ABS, 2) - NOT :')
    assert expression and "NEAR(" not in expression and "*" not in expression
    # All user terms are encoded literal atoms; punctuation cannot become SQL/FTS.
    assert all(atom.startswith('"t') and atom.endswith('"') for atom in expression.split(" OR "))
    with pytest.raises(ValueError):
        t.match_expression("a" * 300)
    with pytest.raises(ValueError):
        t.match_expression("材料" * 3000)


def test_fts_returns_exact_refs_non_exhaustive_top_k_and_unicode(setup):
    index, authority, docs, _ = setup
    docs += [document(str(i), body="ABS 阻燃性能 24.5% 3.2mm") for i in range(15)]
    authority.replace_docs(docs)
    generation = build(index, authority, docs)
    authority.resolve_count = 0
    result = index.search(scope(), "LOI 24.5% 3.2mm", top_k=3, authority=authority)
    assert result["readiness"] == "ready"
    assert result["generation"] == generation["generation_id"]
    assert result["mode"] == "lexical" and result["exhaustive_within_scope"] is False
    assert result["total_matches"] is None and len(result["results"]) == 3
    assert authority.resolve_count == 3
    for hit in result["results"]:
        assert hit["ref"] in [d.ref.model_dump(mode="json") for d in docs]
        assert hit["scope"] == scope().model_dump(mode="json")
        assert (
            hit["snippet"]
            in authority.docs[
                contracts()
                .IndexDocument.model_validate(
                    dict(ref=hit["ref"], content_kind=hit["content_kind"], text=hit["snippet"], eligible=True)
                )
                .ref.model_dump_json()
            ].text
        )
        assert hit["content_hash"] == sha256(hit["snippet"].encode()).hexdigest()


def test_not_ready_empty_and_query_miss_are_distinct(setup):
    index, a, docs, _ = setup
    assert index.search(scope(), "ABS", authority=a)["reason"] == "index_not_ready"
    a.replace_docs([])
    build(index, a, [])
    assert index.search(scope(), "ABS", authority=a)["reason"] == "scope_empty"
    a.replace_docs(docs)
    build(index, a, docs, request="next")
    assert index.search(scope(), "unobtainium", authority=a)["reason"] == "no_lexical_match"
    assert index.search(scope(), '"() * :', authority=a)["reason"] == "no_query_terms"


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "workspace-b"},
        {"collection_id": "collection-b"},
        {"use_policy": "saved_extractions"},
        {"corpus_snapshot_id": "corpus-b"},
        {"layer": "wiki"},
    ],
)
def test_scope_isolation_never_reuses_other_pointer(setup, change):
    index, a, docs, _ = setup
    build(index, a, docs)
    assert index.search(scope(**change), "ABS", authority=a)["reason"] == "index_not_ready"


@pytest.mark.parametrize("change", [{"workspace_id": "workspace-b"}, {"corpus_snapshot_id": "corpus-b"}])
def test_forged_document_owner_rejected_before_publication(setup, change):
    index, a, docs, _ = setup
    ref = record()
    ref.update(change)
    with pytest.raises(ValueError):
        build(index, a, [document(ref=ref)])
    assert index.status(scope())["readiness"] == "not_ready"


@pytest.mark.parametrize("mutation", ["body", "missing", "manifest", "ineligible", "different_ref"])
def test_fresh_authority_is_required_even_with_unchanged_index_flag(setup, mutation):
    index, a, docs, _ = setup
    build(index, a, docs)
    key = docs[0].ref.model_dump_json()
    if mutation == "body":
        a.docs[key] = document(body="ABS 99 %")
    if mutation == "missing":
        a.docs.clear()
    if mutation == "manifest":
        a.manifest["source_revision"] = "withdrawn"
    if mutation == "ineligible":

        def denied():
            raise PermissionError("revoked")

        a.before_resolve = denied
    if mutation == "different_ref":
        a.docs[key] = document("different")
    with pytest.raises(core().IndexFailure, match="INDEX_STALE"):
        index.search(scope(), "ABS", authority=a)


def test_rebuild_failure_never_publishes_partial_generation_and_can_retry(setup):
    index, a, docs, store = setup
    old = build(index, a, docs)
    a.docs.clear()
    with pytest.raises(core().IndexFailure):
        build(index, a, docs, request="failed-next")
    assert index.status(scope())["generation"] == old["generation_id"]
    assert index.build_status(scope(), "failed-next")["state"] == "failed"
    a.replace_docs(docs)
    new = build(index, a, docs, request="failed-next")
    assert new["generation_id"] != old["generation_id"]
    assert build(index, a, docs, request="failed-next")["generation_id"] == new["generation_id"]
    assert index.search(scope(), "ABS", authority=a)["generation"] == new["generation_id"]
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM knowledge_index_current")).scalar() == 1


def test_source_change_during_build_or_search_fails_closed(setup):
    index, a, docs, _ = setup
    old = build(index, a, docs)
    a.before_resolve = lambda: a.manifest.update(source_revision="second")
    with pytest.raises(core().IndexFailure, match="INDEX_STALE"):
        build(index, a, docs, request="changed")
    assert index.status(scope())["generation"] == old["generation_id"]
    a.manifest["source_revision"] = "first"
    with pytest.raises(core().IndexFailure, match="INDEX_STALE"):
        index.search(scope(), "ABS", authority=a)


def test_readers_keep_previous_complete_generation_during_rebuild(setup):
    index, a, docs, _ = setup
    old = build(index, a, docs)
    entered = threading.Event()
    release = threading.Event()
    errors = []
    second = Authority(docs)

    def pause():
        entered.set()
        assert release.wait(4)

    second.before_resolve = pause

    def worker():
        try:
            build(index, second, docs, request="concurrent")
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(4)
        assert index.search(scope(), "ABS", authority=a)["generation"] == old["generation_id"]
    finally:
        release.set()
        thread.join(4)
    assert not errors and not thread.is_alive()
    assert index.search(scope(), "ABS", authority=a)["generation"] != old["generation_id"]


def test_entry_integrity_and_tokenizer_alias_version_fence(setup):
    index, a, docs, store = setup
    build(index, a, docs)
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE knowledge_index_entries SET text='forged ABS'"))
    with pytest.raises(core().IndexFailure, match="INDEX_STALE"):
        index.search(scope(), "ABS", authority=a)
    build(index, a, docs, request="repair")
    from kb_ingest.knowledge_index_tokens import IndexTokenizer

    other = core().KnowledgeIndexStore(
        store, tokenizer=IndexTokenizer(alias_groups=[["polystyrene", "PS"]], alias_version="test-v2")
    )
    with pytest.raises(core().IndexFailure, match="INDEX_STALE"):
        other.search(scope(), "ABS", authority=a)


def test_resource_bounds_and_callback_deadline(setup):
    index, a, docs, _ = setup
    build(index, a, docs)
    for top in (0, 101, True):
        with pytest.raises(ValueError):
            index.search(scope(), "ABS", top_k=top, authority=a)
    with pytest.raises(ValueError):
        document(body="x" * 40000)

    def slow():
        time.sleep(0.02)

    a.before_resolve = slow
    with pytest.raises(core().IndexFailure, match="INDEX_TIMEOUT"):
        index.search(scope(), "ABS", authority=a, timeout=0.005)


def test_forged_content_kind_and_ineligible_never_indexed():
    core()
    with pytest.raises(ValueError):
        document(content_kind="wiki_revision")
    with pytest.raises(ValueError):
        document(eligible=False)


def test_request_identity_cannot_be_reused_for_different_build(setup):
    index, a, docs, _ = setup
    build(index, a, docs)
    changed = [document(body="another")]
    a.replace_docs(changed)
    with pytest.raises(core().IndexFailure, match="INDEX_REQUEST_CONFLICT"):
        build(index, a, changed)


def test_abbreviation_variant_uses_explicit_synthetic_alias_mapping(setup):
    index, a, _, _ = setup
    docs = [document(body="sample code 7")]
    a.replace_docs(docs)
    build(index, a, docs)
    assert len(index.search(scope(), "SC7", authority=a)["results"]) == 1


def test_passage_and_published_wiki_candidates_keep_typed_owner_and_need_leaf_read(setup):
    index, _, _, _ = setup
    C = contracts()
    for kind, policy in [("source_passage", "explore"), ("wiki_revision", "source_reviewed")]:
        selected = scope(layer="source_passage" if kind == "source_passage" else "wiki", use_policy=policy)
        base = dict(kind=kind, workspace_id="workspace-a", corpus_snapshot_id="corpus-a")
        if kind == "source_passage":
            base.update(
                source_snapshot_ref=dict(
                    kind="source_snapshot",
                    workspace_id="workspace-a",
                    source_snapshot_id="source-snapshot",
                    source_id="paper",
                    source_revision_id="source-revision",
                    source_asset_sha256="b" * 64,
                    source_index_version="mineru/v1",
                    source_index_sha256="c" * 64,
                    manifest_sha256="d" * 64,
                ),
                segment_id="p1",
                quote_sha256=sha256(b"ABS 24.5 %").hexdigest(),
                physical_page=None,
                location_status="unresolved",
            )
        else:
            base.update(
                page_id="published-page",
                revision_id="published-revision",
                content_hash="b" * 64,
                dependency_manifest_hash="c" * 64,
            )
        docs = [C.IndexDocument(ref=base, content_kind=kind, text="ABS 24.5 %", eligible=True)]
        authority = Authority(docs)
        build(index, authority, docs, selected=selected, request="build-" + kind)
        hit = index.search(selected, "ABS", authority=authority)["results"][0]
        assert hit["ref"] == base | ({"bbox": None} if kind == "source_passage" else {})
        assert hit["content_kind"] == kind and hit["requires_authoritative_read"] is True
        assert "scientific_validation" not in hit
        authority.docs.clear()  # current publication withdrawn / source no longer eligible
        with pytest.raises(core().IndexFailure, match="INDEX_STALE"):
            index.search(selected, "ABS", authority=authority)


def test_same_text_different_source_remains_two_candidates(setup):
    index, a, _, _ = setup
    docs = [document("paper"), document("patent")]
    a.replace_docs(docs)
    build(index, a, docs)
    hits = index.search(scope(), "ABS", authority=a)["results"]
    assert {h["ref"]["source_id"] for h in hits} == {"source-paper", "source-patent"}


def test_shared_sqlite_same_text_workspace_indexes_stay_isolated(setup):
    index, _, _, store = setup
    body = "shared zirconium token unique to this fixture"
    docs_a = [document("a", body=body)]
    ref_b = record("b")
    ref_b["workspace_id"] = "workspace-b"
    docs_b = [document("b", body=body, ref=ref_b)]
    scope_a = scope()
    scope_b = scope(workspace_id="workspace-b")
    auth_a = Authority(docs_a)
    auth_b = Authority(docs_b)
    gen_a = build(index, auth_a, docs_a, selected=scope_a, request="build-a")
    gen_b = build(index, auth_b, docs_b, selected=scope_b, request="build-b")
    assert gen_a["generation_id"] != gen_b["generation_id"]
    hits_a = index.search(scope_a, "zirconium", authority=auth_a)
    hits_b = index.search(scope_b, "zirconium", authority=auth_b)
    assert hits_a["readiness"] == "ready" and hits_b["readiness"] == "ready"
    assert {row["ref"]["workspace_id"] for row in hits_a["results"]} == {"workspace-a"}
    assert {row["ref"]["task_id"] for row in hits_a["results"]} == {"task-a"}
    assert {row["ref"]["workspace_id"] for row in hits_b["results"]} == {"workspace-b"}
    assert {row["ref"]["task_id"] for row in hits_b["results"]} == {"task-b"}
    before_gen, before_hits = gen_b["generation_id"], hits_b["results"]
    rebuilt = build(index, auth_a, docs_a, selected=scope_a, request="rebuild-a")
    assert rebuilt["generation_id"] != gen_a["generation_id"]
    after_rebuild = index.search(scope_b, "zirconium", authority=auth_b)
    assert after_rebuild["generation"] == before_gen and after_rebuild["results"] == before_hits
    auth_a.replace_docs([])
    build(index, auth_a, [], selected=scope_a, request="delete-a")
    assert index.search(scope_a, "zirconium", authority=auth_a)["reason"] == "scope_empty"
    after_delete = index.search(scope_b, "zirconium", authority=auth_b)
    assert after_delete["generation"] == before_gen and after_delete["results"] == before_hits
    with store.engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT COUNT(*) FROM knowledge_index_fts WHERE generation_id = :g"),
            {"g": before_gen},
        ).scalar()
    assert remaining == len(before_hits)


def test_interrupted_build_is_explicitly_resumable_without_publishing_staged_rows(setup):
    index, a, docs, store = setup
    old = build(index, a, docs)
    a.docs.clear()
    with pytest.raises(core().IndexFailure):
        build(index, a, docs, request="interrupted")
    with store.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE knowledge_index_generations SET state='building',error_code=NULL WHERE request_id='interrupted'"
            )
        )
    assert index.build_status(scope(), "interrupted")["state"] == "building"
    assert index.status(scope())["generation"] == old["generation_id"]
    a.replace_docs(docs)
    resumed = build(index, a, docs, request="interrupted")
    assert resumed["state"] == "ready"
    assert index.search(scope(), "ABS", authority=a)["generation"] == resumed["generation_id"]


@pytest.mark.parametrize(
    "layer,policy", [("record", "explore"), ("source_passage", "history"), ("wiki", "saved_extractions")]
)
def test_layer_policy_contract_cannot_be_widened(layer, policy):
    with pytest.raises(ValueError):
        scope(layer=layer, use_policy=policy)


@pytest.mark.parametrize(
    "query", ['ABS OR "*" NOT (UL94)', 'ABS"; DROP TABLE knowledge_index_entries; --', "NEAR(ABS,3) : *"]
)
def test_user_fts_syntax_is_literal_in_real_database(setup, query):
    index, a, docs, store = setup
    build(index, a, docs)
    result = index.search(scope(), query, authority=a)
    assert len(result["results"]) == 1 and result["results"][0]["ref"] == docs[0].ref.model_dump(mode="json")
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM knowledge_index_entries")).scalar() == 1


@pytest.mark.parametrize("lock_kind", ["file", "sqlite"])
def test_rebuild_deadline_includes_waiting_for_other_writers(setup, lock_kind):
    index, a, docs, store = setup
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def holder():
        try:
            lock = store.lock("knowledge-index-" + scope().key) if lock_kind == "file" else store._write()
            with lock:
                entered.set()
                assert release.wait(2)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=holder)
    thread.start()
    timer = threading.Timer(0.35, release.set)
    try:
        assert entered.wait(2)
        timer.start()
        started = time.monotonic()
        snapshot = contracts().IndexSnapshot(scope=scope(), source_manifest=a.manifest, documents=docs)
        with pytest.raises(core().IndexFailure, match="INDEX_TIMEOUT"):
            index.rebuild(snapshot, request_id="bounded-writer", authority=a, timeout=0.03)
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        thread.join(2)
        timer.cancel()
    assert not errors


def test_incomplete_snapshot_cannot_publish_scope_empty(setup):
    index, a, docs, _ = setup
    a.manifest["index_documents"] = {d.key: d.document_hash for d in docs}
    with pytest.raises(ValueError, match="inventory"):
        build(index, a, [])
    assert index.status(scope())["readiness"] == "not_ready"
