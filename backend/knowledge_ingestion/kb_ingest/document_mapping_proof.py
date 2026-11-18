"""Bind verified table expansion and directional dependencies to actual records.

This is an engineering provenance proof, not a human or scientific approval.
"""

from collections.abc import Hashable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .core import fingerprint
from .ingest.documents.document_quality_types import MappingDependencyIndex, MappingSupportIndex, RecordIndex

SECTIONS = {"objects": "object_id", "assertions": "assertion_id", "relations": "relation_id"}
VERSION = "program-mapping-proof-2"


def record_content_hash(record):
    # A new proof can verify exactly the same shared method. The proof pointer
    # is checked separately; it is not a scientific change to that method.
    return fingerprint(
        {k: v for k, v in record.items() if k not in ("use_status", "review_state", "mapping_review_ref")}
    )


def materialized_snapshot(package):
    return {row[key]: record_content_hash(row) for section, key in SECTIONS.items() for row in package[section]}


def _record_index(package: dict[str, Any]) -> RecordIndex:
    return {row[key]: row for section, key in SECTIONS.items() for row in package[section]}


def bind_mapping_review(package, before, accepted, verification, *, materialized):
    """Bind only exact outputs attested by the ordinary materializer.

    The accepted relation list may be filtered. Its local r:0 must be translated
    via accepted_target_ids, never confused with r:0 in the full compiled table.
    No before/after difference grants approval to unrelated records.
    """
    candidate_hash = fingerprint(accepted)
    if (
        verification.get("accepted_candidate_sha256") != candidate_hash
        or materialized.get("candidate_sha256") != candidate_hash
    ):
        raise ValueError("mapping review/materialization does not bind the accepted candidate")
    if (
        verification.get("proof_sha256") != fingerprint({k: v for k, v in verification.items() if k != "proof_sha256"})
        or not verification.get("mapping_sha256")
        or not verification.get("verified_target_ids")
    ):
        raise ValueError("mapping review proof is absent or changed")
    records = _record_index(package)
    translation = {}
    record_targets = {}
    expected_local = set()
    for section, prefix in (("objects", "o:"), ("facts", "f:"), ("relations", "r:")):
        original_ids = verification.get("accepted_target_ids", {}).get(section, [])
        rows = accepted.get(section, [])
        if len(original_ids) != len(rows):
            raise ValueError("accepted target order is incomplete")
        for i, (target, row) in enumerate(zip(original_ids, rows)):
            local = prefix + (str(i) if section == "relations" else row["key"])
            expected_local.add(local)
            if section != "relations" and target != local:
                raise ValueError("candidate key binding changed")
            if not target.startswith(prefix):
                raise ValueError("candidate target type changed")
            proof = verification.get("targets", {}).get(target, {})
            if proof.get("target_sha256") != fingerprint(row):
                raise ValueError("candidate target content changed")
            rid = materialized.get("targets", {}).get(local)
            if rid not in records or target in translation or rid in record_targets:
                raise ValueError("materialized target identity missing or duplicate")
            expected_section = "assertions" if section == "facts" else section
            if SECTIONS[expected_section] not in records[rid]:
                raise ValueError("materialized target kind changed")
            if materialized.get("record_sha256", {}).get(local) != record_content_hash(records[rid]):
                raise ValueError("materialized target content changed")
            translation[target] = rid
            record_targets[rid] = target
    if set(materialized.get("targets", {})) != expected_local or set(verification["verified_target_ids"]) != set(
        translation
    ):
        raise ValueError("materialized target coverage does not match verified targets")
    dependencies = {}
    external = materialized.get("existing_objects", {})
    for target, rid in translation.items():
        dependencies[rid] = set()
        for ref in verification["targets"][target].get("dependencies", []):
            dependency = external.get(ref[len("existing:") :]) if ref.startswith("existing:") else translation.get(ref)
            if dependency not in records:
                raise ValueError("materialized dependency is absent")
            if ref.startswith("existing:") and dependency not in before:
                raise ValueError("existing dependency was not present before materialization")
            dependencies[rid].add(dependency)
    used_records = set(translation.values()) | {r for deps in dependencies.values() for r in deps}
    used_evidence = {ref for rid in used_records for ref in records[rid].get("evidence_refs", [])}
    evidence = {e["evidence_id"]: fingerprint(e) for e in package["evidence"] if e["evidence_id"] in used_evidence}
    if (
        not translation
        or not used_evidence
        or set(evidence) != used_evidence
        or any(not records[rid].get("evidence_refs") for rid in used_records)
    ):
        raise ValueError("materialized mapping has incomplete record/dependency evidence")
    base = {
        "version": VERSION,
        "verification": deepcopy(verification),
        "source_sha256": {a["asset_id"]: a["sha256"] for a in package["assets"]},
        "source_revision_id": package["header"]["source_revision_id"],
        "record_revision_id": package["header"]["record_revision_id"],
        "target_records": translation,
        "dependencies": {k: sorted(v) for k, v in dependencies.items()},
    }
    proof_id = "mapping-review:" + fingerprint(base)
    # Everything above is checked before modifying records. An invalid mapping
    # cannot leave a half-bound package behind.
    for rid in translation.values():
        records[rid]["mapping_review_ref"] = proof_id
    proof = {
        **base,
        "proof_id": proof_id,
        "records": {rid: record_content_hash(records[rid]) for rid in translation.values()},
        "dependency_records": {rid: record_content_hash(records[rid]) for rid in used_records},
        "evidence": evidence,
    }
    proof["proof_sha256"] = fingerprint(proof)
    package.setdefault("document_mapping_reviews", []).append(proof)
    return proof_id


def _valid_proofs(package: dict[str, Any]) -> RecordIndex:
    if not package.get("document_mapping_reviews"):
        return {}
    by_id = {}
    duplicates = set()
    for p in package.get("document_mapping_reviews", []):
        key = p.get("proof_id")
        if key in by_id:
            duplicates.add(key)
        by_id[key] = p
    assets = {a["asset_id"]: a["sha256"] for a in package["assets"]}
    valid = {}
    for key, p in by_id.items():
        if key in duplicates or p.get("version") != VERSION:
            continue
        if p.get("proof_sha256") != fingerprint({k: v for k, v in p.items() if k != "proof_sha256"}):
            continue
        if (
            p.get("source_revision_id") != package["header"]["source_revision_id"]
            or p.get("record_revision_id") != package["header"]["record_revision_id"]
            or p.get("source_sha256") != assets
        ):
            continue
        valid[key] = p
    return valid


@dataclass(frozen=True)
class _MappingScan:
    records: RecordIndex
    proofs: RecordIndex
    evidence: RecordIndex
    evidence_hash: dict[Hashable, str]
    hashes: dict[Hashable, str]


def _direct_mapping_support(scan: _MappingScan, rid: Hashable, row: dict[str, Any]) -> Iterable[Hashable] | None:
    p = scan.proofs.get(row.get("mapping_review_ref"))
    if not p or p.get("records", {}).get(rid) != scan.hashes[rid]:
        return None
    refs = row.get("evidence_refs", [])
    if not refs or any(
        p.get("evidence", {}).get(e) != scan.evidence_hash.get(e) or e not in scan.evidence for e in refs
    ):
        return None
    deps = p.get("dependencies", {}).get(rid)
    if deps is None:
        return None
    for dependency in deps:
        source = scan.records.get(dependency)
        if source is None or p.get("dependency_records", {}).get(dependency) != scan.hashes.get(dependency):
            return None
        erefs = source.get("evidence_refs", [])
        if not erefs or any(
            e not in scan.evidence or p.get("evidence", {}).get(e) != scan.evidence_hash[e] for e in erefs
        ):
            return None
        if not source.get("mapping_review_ref") and any(
            scan.evidence[e].get("support_review") not in ("model_semantic_review", "human_source_review")
            for e in erefs
        ):
            return None
    return deps


def mapping_dependency_index(package: dict[str, Any]) -> MappingDependencyIndex:
    """Immutable proof edges also drive explicit review revocation at query time."""
    result: MappingDependencyIndex = {}
    records = _record_index(package)
    for p in _valid_proofs(package).values():
        for record, deps in p.get("dependencies", {}).items():
            current = records.get(record, {})
            if current.get("mapping_review_ref") == p["proof_id"] and p.get("records", {}).get(
                record
            ) == record_content_hash(current):
                result.setdefault(record, set()).update(deps)
    return result


def mapping_record_supported(package: dict[str, Any], record: dict[str, Any]) -> bool:
    key = next((record[k] for k in SECTIONS.values() if k in record), None)
    expected = mapping_support_index(package).get(key)
    return expected is not None and expected == record_content_hash(record)


def mapping_support_index(package: dict[str, Any]) -> MappingSupportIndex:
    """Check content/evidence and transitive proof dependencies once per package."""
    records = _record_index(package)
    evidence = {e["evidence_id"]: e for e in package["evidence"]}
    evidence_hash = {key: fingerprint(row) for key, row in evidence.items()}
    hashes = {key: record_content_hash(row) for key, row in records.items()}
    scan = _MappingScan(records, _valid_proofs(package), evidence, evidence_hash, hashes)
    supported: MappingSupportIndex = {}
    dependencies: dict[Hashable, Iterable[Hashable]] = {}
    for rid, row in records.items():
        deps = _direct_mapping_support(scan, rid, row)
        if deps is not None:
            supported[rid] = scan.hashes[rid]
            dependencies[rid] = deps
    # Only consumers of a lost support are removed. A shared method does not
    # depend on every result that happened to use it.
    while True:
        failed = {
            rid
            for rid, deps in dependencies.items()
            if rid in supported and any(records[d].get("mapping_review_ref") and d not in supported for d in deps)
        }
        if not failed:
            return supported
        for rid in failed:
            supported.pop(rid)
