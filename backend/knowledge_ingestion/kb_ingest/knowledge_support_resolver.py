"""Pinned source/artifact provenance and precise live source availability checks."""

from copy import deepcopy

from .core import StateConflict, fingerprint
from .knowledge_contracts import TableAnalysisArtifactRef, TableSnapshotRef
from .knowledge_table_artifact import read_table_artifact

REGION_FIELDS = ("id", "name", "asset_id", "sheet", "table_range", "header_ranges", "data_ranges", "context_ranges")


def region_from_raw(raw):
    return {
        "id": raw["source_region_id"],
        "name": raw["name"],
        "asset_id": raw["asset_id"],
        "sheet": raw["sheet"],
        **{
            key: deepcopy(raw.get(key, [])) for key in ("table_range", "header_ranges", "data_ranges", "context_ranges")
        },
    }


def turn_binding(turn):
    return {
        key: deepcopy(turn[key])
        for key in ("chat_id", "turn_id", "input_sha256", "status", "assistant_text", "table_version_ids", "tool_calls")
    }


class KnowledgeSupportResolver:
    def __init__(self, wiki):
        self.wiki = wiki
        self.service = wiki.ingestion_service

    def completed_turn(self, chat_id, turn_id):
        files = self.service._table_chats()
        with self.service.store.lock("table-chat:" + chat_id):
            chat = files.read_chat(chat_id)
            if turn_id not in chat["turn_ids"]:
                raise StateConflict("TURN_NOT_COMMITTED")
            turn, snapshot = files.read_turn(chat_id, turn_id)
            if turn["status"] != "completed" or not turn["assistant_text"].strip():
                raise StateConflict("TURN_NOT_COMPLETED")
            raws = []
            artifacts = []
            for table_id in snapshot["table_version_ids"]:
                raw = self.service._raw_artifacts().load("raw", table_id)["payload"]
                if fingerprint(raw) != snapshot["table_hashes"][table_id]:
                    raise StateConflict("TABLE_SNAPSHOT_CHANGED")
                self.service.store.asset_bytes(raw["asset_id"])
                if self.service.store.get_asset(raw["asset_id"])["sha256"] != raw["asset_sha256"]:
                    raise StateConflict("SOURCE_UNAVAILABLE")
                raws.append(raw)
            for call in turn["tool_calls"]:
                aid = call.get("artifact_id")
                if aid:
                    payload = files.read_artifact(chat_id, turn_id, aid)
                    if payload["call_id"] != call["call_id"] or call["status"] != "completed":
                        raise StateConflict("ARTIFACT_CALL_MISMATCH")
                    artifact = payload["artifact"]
                    used = (
                        [artifact["table_version_id"]]
                        if "table_version_id" in artifact
                        else [r["table_version_id"] for r in artifact.get("tables", [])]
                    )
                    if not used or not set(used) <= set(snapshot["table_version_ids"]):
                        raise StateConflict("ARTIFACT_SCOPE_MISMATCH")
                    arguments = call["arguments"]
                    authorized = (
                        [arguments["table_version_id"]]
                        if "table_version_id" in arguments
                        else [r["table_version_id"] for r in arguments.get("tables", [])]
                    )
                    if set(used) != set(authorized):
                        raise StateConflict("ARTIFACT_CALL_MISMATCH")
                    artifacts.append(payload)
            return turn, snapshot, raws, artifacts

    def supports(self, turn, raws, artifacts, corpus_id):
        supports = []
        deps = []
        usable = []
        for raw in raws:
            ref = TableSnapshotRef(
                kind="table_snapshot",
                workspace_id=self.wiki.workspace_id,
                corpus_snapshot_id=corpus_id,
                **{
                    key: raw[key]
                    for key in (
                        "source_id",
                        "asset_id",
                        "asset_sha256",
                        "table_id",
                        "table_version_id",
                        "source_region_id",
                        "table_range",
                    )
                },
                snapshot_sha256=fingerprint(raw),
            ).model_dump(mode="json")
            sid = "support-" + fingerprint([ref, turn["chat_id"], turn["turn_id"]])
            supports.append({"support_id": sid, "ref": ref, "support_type": "source_description"})
            source = self.service.store.source_state(raw["source_id"]) or {}
            deps.append(
                {
                    "dependency_id": "raw-" + raw["table_version_id"],
                    "kind": "raw_table",
                    "ref": ref,
                    "region": region_from_raw(raw),
                    "reader_options": deepcopy(raw["reader_options"]),
                    "published_task_id": source.get("published_task_id"),
                }
            )
            for call in turn["tool_calls"]:
                if (
                    call["status"] == "completed"
                    and isinstance(call.get("output"), dict)
                    and call["output"].get("ok") is True
                    and call["name"] in {"inspect_table", "read_table_rows", "analyze_table", "compare_tables"}
                ):
                    args = call["arguments"]
                    ids = (
                        [args["table_version_id"]]
                        if "table_version_id" in args
                        else [a["table_version_id"] for a in args.get("tables", [])]
                    )
                    if raw["table_version_id"] in ids:
                        usable.append(sid)
                        break
        for payload in artifacts:
            ref = TableAnalysisArtifactRef(
                kind="table_analysis_artifact",
                workspace_id=self.wiki.workspace_id,
                corpus_snapshot_id=corpus_id,
                chat_id=turn["chat_id"],
                turn_id=turn["turn_id"],
                artifact_id=payload["artifact_id"],
                table_version_ids=turn["table_version_ids"],
                artifact_sha256=fingerprint(payload),
            ).model_dump(mode="json")
            sid = "support-" + fingerprint(ref)
            supports.append({"support_id": sid, "ref": ref, "support_type": "calculation"})
            usable.append(sid)
            deps.append({"dependency_id": payload["artifact_id"], "kind": "table_analysis_artifact", "ref": ref})
        deps.append(
            {
                "dependency_id": turn["turn_id"],
                "kind": "chat_turn",
                "chat_id": turn["chat_id"],
                "turn_id": turn["turn_id"],
                "binding_sha256": fingerprint(turn_binding(turn)),
                "support_ids": [s["support_id"] for s in supports],
            }
        )
        return supports, deps, sorted(set(usable))

    def validate(self, revision):
        gaps = []

        def gap(code, dep=None):
            entry = {"code": code}
            if dep:
                entry["dependency_id"] = dep["dependency_id"]
            if entry not in gaps:
                gaps.append(entry)

        manifest = revision["dependency_manifest"]
        try:
            self.wiki.registry.check_manifest(self.wiki.snapshot_store.read_corpus(manifest["corpus_snapshot_id"]))
        except Exception:
            gap("SOURCE_SCOPE_CHANGED")
        if manifest.get("research_summary"):
            try:
                from .research_summary_supports import validate_content, validate_frozen

                validate_frozen(self.wiki.research_service, self.wiki, {"manifest": manifest})
                validate_content(manifest, revision["content"])
            except Exception as exc:
                # A bounded read failing is not evidence that immutable sources changed.
                if getattr(exc, "code", None) in {
                    "timeout",
                    "cancelled",
                    "QUERY_TIMEOUT",
                    "REQUEST_CANCELED",
                    "ARCHIVE_UNAVAILABLE",
                }:
                    raise
                gap("RESEARCH_SUPPORT_INVALID")
            return gaps
        gaps.extend(self.validate_dependencies(manifest))
        from .study_wiki_evidence import StudySupportContext

        study_context = StudySupportContext(self.wiki)
        supports = {s["support_id"]: s for s in revision["supports"]}
        if not any(s["claims"] for s in revision["content"]["sections"]):
            gap("EMPTY_KNOWLEDGE")
        for section in revision["content"]["sections"]:
            for claim in section["claims"]:
                ids = claim["support_ids"]
                if not ids:
                    from .knowledge_table_wiki_policy import is_table_summary
                    from .study_wiki_policy import is_study_summary

                    if not (
                        (is_table_summary(revision) or is_study_summary(revision))
                        and claim["claim_type"] == "editorial_synthesis"
                    ):
                        gap("CLAIM_SUPPORT_MISSING")
                elif any(sid not in supports for sid in ids):
                    gap("CLAIM_SUPPORT_INVALID")
                if claim["claim_type"] == "reported_measurement":
                    from .study_wiki_policy import is_study_summary

                    allowed = False
                    if is_study_summary(revision):
                        from .study_wiki_evidence import resolve_support

                        for sid in ids:
                            try:
                                fixed = resolve_support(self.wiki, supports[sid], manifest, context=study_context).get(
                                    "fact", {}
                                )
                                if fixed.get("fact_type") == "measurement" and fixed.get("attribution") == "authors":
                                    allowed = True
                            except (ValueError, KeyError, StateConflict):
                                pass
                    if not allowed:
                        gap("MEASUREMENT_EVIDENCE_REQUIRED")
                if claim["claim_type"] == "computed_observation" and not any(
                    supports.get(sid, {}).get("support_type") == "calculation" for sid in ids
                ):
                    gap("CALCULATION_EVIDENCE_REQUIRED")
        if manifest.get("study_unit"):
            for support in supports.values():
                try:
                    if support["ref"]["kind"] != "study_unit_snapshot":
                        raise ValueError()
                    from .study_wiki_evidence import resolve_support

                    _resolved = resolve_support(self.wiki, support, manifest, context=study_context)
                    binding = manifest["study_unit"]
                    if any(support["ref"][key] != binding[key] for key in ("unit_id", "snapshot_id", "content_hash")):
                        raise ValueError()
                    scope = support.get("scope") or {}
                    if scope.get("snapshot_entry_kind") == "source_block" and scope.get(
                        "source_block"
                    ) not in binding.get("generation_manifest", {}).get("source_blocks", []):
                        raise ValueError()
                except Exception:
                    gap("STUDY_SUPPORT_INVALID")
        return gaps

    def validate_dependencies(self, manifest):
        gaps = []

        def gap(code, dep=None):
            entry = {"code": code}
            if dep:
                entry["dependency_id"] = dep["dependency_id"]
            if entry not in gaps:
                gaps.append(entry)

        from .study_wiki_evidence import StudySupportContext

        study_context = StudySupportContext(self.wiki)
        for dep in manifest["dependencies"]:
            try:
                if dep["kind"] == "study_unit_snapshot":
                    study_context.unit(dep["ref"])
                elif dep["kind"] == "raw_table":
                    ref = dep["ref"]
                    raw = self.service._raw_artifacts().load("raw", ref["table_version_id"])["payload"]
                    expected = {
                        key: raw[key]
                        for key in (
                            "source_id",
                            "asset_id",
                            "asset_sha256",
                            "table_id",
                            "table_version_id",
                            "source_region_id",
                            "table_range",
                        )
                    }
                    if any(ref[key] != v for key, v in expected.items()) or fingerprint(raw) != ref["snapshot_sha256"]:
                        gap("TABLE_SNAPSHOT_CHANGED", dep)
                        continue
                    try:
                        self.service.store.asset_bytes(ref["asset_id"])
                        if self.service.store.get_asset(ref["asset_id"])["sha256"] != ref["asset_sha256"]:
                            raise ValueError()
                    except Exception:
                        gap("SOURCE_UNAVAILABLE", dep)
                    draft = self.service.store.get_table_draft(ref["source_id"])
                    region = next(
                        (r for r in draft["recipe"]["region_review"]["regions"] if r["id"] == ref["source_region_id"]),
                        None,
                    )
                    if region is None:
                        gap("TABLE_REGION_REMOVED", dep)
                    elif (
                        {key: region.get(key, [] if key.endswith("ranges") else None) for key in REGION_FIELDS}
                        != dep["region"]
                        or draft["reader_options"].get(ref["asset_id"], {}) != dep["reader_options"]
                        or draft["asset_hashes"].get(ref["asset_id"]) != ref["asset_sha256"]
                    ):
                        gap("TABLE_DEFINITION_CHANGED", dep)
                    if dep.get("published_task_id"):
                        source = self.service.store.source_state(ref["source_id"])
                        if source is None or source["published_task_id"] != dep["published_task_id"]:
                            gap("FORMAL_SOURCE_WITHDRAWN", dep)
                elif dep["kind"] == "chat_turn":
                    turn, _, _, _ = self.completed_turn(dep["chat_id"], dep["turn_id"])
                    if fingerprint(turn_binding(turn)) != dep["binding_sha256"]:
                        gap("TURN_CHANGED", dep)
                elif dep["kind"] == "table_analysis_artifact":
                    ref = dep["ref"]
                    payload = read_table_artifact(self.service, ref)
                    if fingerprint(payload) != ref["artifact_sha256"]:
                        gap("ARTIFACT_CHANGED", dep)
                else:
                    gap("SUPPORT_TYPE_UNSUPPORTED", dep)
            except Exception:
                gap(
                    "SOURCE_UNAVAILABLE"
                    if dep["kind"] == "raw_table"
                    else "TURN_CHANGED"
                    if dep["kind"] == "chat_turn"
                    else "ARTIFACT_CHANGED",
                    dep,
                )
        return gaps

    def material(self, revision):
        raws = []
        artifacts = []
        originals = {}
        for dep in revision["dependency_manifest"]["dependencies"]:
            if dep["kind"] == "study_unit_snapshot":
                from .study_units import StudyUnitService

                unit = StudyUnitService(self.service, workspace_id=self.wiki.workspace_id).resolve(dep["ref"])
                artifacts.append(
                    {"kind": "study_unit_snapshot", "ref": deepcopy(dep["ref"]), "snapshot": unit["snapshot"]}
                )
                for source in unit["snapshot"]["payload"]["source_refs"]:
                    originals[source["asset_id"]] = self.service.store.asset_path(source["asset_id"])
            elif dep["kind"] == "raw_table":
                ref = dep["ref"]
                raw = self.service._raw_artifacts().load("raw", ref["table_version_id"])["payload"]
                raws.append(raw)
                self.service.store.asset_bytes(ref["asset_id"])
                originals[ref["asset_id"]] = self.service.store.asset_path(ref["asset_id"])
            elif dep["kind"] == "table_analysis_artifact":
                ref = dep["ref"]
                artifacts.append(read_table_artifact(self.service, ref))
            elif dep["kind"] == "chat_turn":
                turn, snapshot, _, _ = self.completed_turn(dep["chat_id"], dep["turn_id"])
                artifacts.append({"kind": "chat_turn_provenance", "input": snapshot, "turn": turn_binding(turn)})
        return raws, artifacts, originals

    def research_material(self, revision):
        """Explicit document material; never disguise paper records as raw tables."""
        context = revision["dependency_manifest"]["research_summary"]
        knowledge = self.wiki.research_service.knowledge
        records = []
        evidence = []
        passages = []
        originals = {}
        hashes = {}
        from .research_summary_unified import is_unified

        unified = is_unified(revision["dependency_manifest"])
        scopes = {}
        if unified:
            for support in revision["supports"]:
                if support["ref"]["kind"] in {"record_revision", "sample_test_context", "assertion", "evidence"}:
                    scope = support["scope"]
                    scopes[fingerprint(scope)] = scope
        else:
            scope = context["selection"]["scopes"].get("record")
            if scope:
                scopes[fingerprint(scope)] = scope
        for scope in scopes.values():
            adapter = knowledge.adapters[scope["collection_id"]]
            for record in adapter.read_corpus(
                scope["corpus_snapshot_id"], use_policy=scope["use_policy"], mode="live_readback"
            ):
                if unified and not any(
                    s["ref"].get("task_id") == record.ref.task_id
                    and s["ref"].get("package_sha256") == record.ref.package_sha256
                    for s in revision["supports"]
                ):
                    continue
                records.append(
                    {
                        "kind": "document_record",
                        "ref": record.ref.model_dump(mode="json"),
                        "scope": scope,
                        "package": record.package,
                    }
                )
                for saved_asset in record.package["assets"]:
                    aid = saved_asset["asset_id"]
                    asset = self.service.store.get_asset(aid)
                    self.service.store.asset_bytes(aid)
                    if asset["sha256"] != saved_asset["sha256"]:
                        raise StateConflict("SOURCE_UNAVAILABLE")
                    originals[aid] = self.service.store.asset_path(aid)
                    hashes[aid] = saved_asset["sha256"]
        from .knowledge_contracts import VERSION, GetRequest

        for entry in context["registry"].values():
            if entry["kind"] == "quote":
                result = knowledge.get(
                    GetRequest(
                        schema_version=VERSION,
                        request_id="wiki-publication-support",
                        target=entry["ref"],
                        use_policy=entry["scope"]["use_policy"],
                        view="location",
                    )
                )
                material = {
                    "kind": "source_passage" if entry["layer"] == "source_passage" else "document_evidence",
                    "scope": entry["scope"],
                    "entry": entry,
                    "result": result.model_dump(mode="json"),
                }
                (passages if entry["layer"] == "source_passage" else evidence).append(material)
                if entry["layer"] == "source_passage":
                    source = entry["ref"]["source_snapshot_ref"]
                    task = self.service.store.get_task(
                        self.service.store.source_state(source["source_id"])["active_task_id"]
                    )
                    matched = False
                    for aid in task["asset_ids"]:
                        asset = self.service.store.get_asset(aid)
                        if asset["sha256"] == source["source_asset_sha256"]:
                            self.service.store.asset_bytes(aid)
                            originals[aid] = self.service.store.asset_path(aid)
                            hashes[aid] = asset["sha256"]
                            matched = True
                    if not matched:
                        raise StateConflict("SOURCE_UNAVAILABLE")
        return {
            "document_records": records,
            "document_evidence": evidence,
            "source_passages": passages,
            "document_asset_hashes": hashes,
            "originals": originals,
        }

    def unified_material(self, revision):
        """Include actual dependencies of referenced Wiki revisions, with source hashes."""
        result = {
            "raw_tables": [],
            "artifacts": [],
            "originals": {},
            "document_asset_hashes": {},
            "document_records": [],
            "document_evidence": [],
            "source_passages": [],
        }
        pending = [revision]
        seen = set()
        while pending:
            current = pending.pop()
            identity = current.get("revision_id") or fingerprint(current["dependency_manifest"])
            if identity in seen:
                continue
            seen.add(identity)
            if len(seen) > 100:
                raise StateConflict("WIKI_DEPENDENCY_LIMIT")
            raws, artifacts, originals = self.material(current)
            result["raw_tables"].extend(raws)
            result["artifacts"].extend(artifacts)
            result["originals"].update(originals)
            for raw in raws:
                result["document_asset_hashes"][raw["asset_id"]] = raw["asset_sha256"]
            for artifact in artifacts:
                if artifact.get("kind") == "study_unit_snapshot":
                    for asset in artifact["snapshot"]["payload"]["source_refs"]:
                        result["document_asset_hashes"][asset["asset_id"]] = asset["asset_sha256"]
            if current["dependency_manifest"].get("research_summary"):
                docs = self.research_material(current)
                for key in ("document_records", "document_evidence", "source_passages"):
                    result[key].extend(docs[key])
                result["originals"].update(docs["originals"])
                result["document_asset_hashes"].update(docs["document_asset_hashes"])
            for support in current["supports"]:
                ref = support["ref"]
                if ref["kind"] == "wiki_revision":
                    page = self.wiki.get_page(ref["page_id"], ref["revision_id"])
                    if page["gaps"] or any(
                        page["revision"][k] != ref[k]
                        for k in ("revision_id", "content_hash", "dependency_manifest_hash")
                    ):
                        raise StateConflict("WIKI_REFERENCE_CHANGED")
                    pending.append(page["revision"])
        for key in ("raw_tables", "artifacts", "document_records", "document_evidence", "source_passages"):
            result[key] = list({fingerprint(value): value for value in result[key]}.values())
        return result
