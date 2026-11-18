"""Read-only issue ownership and explicitly bound maintenance overrides.

No text classifier, model call, source mutation or automatic review is involved.
"""

import json
import time
from collections import defaultdict
from hashlib import sha256

from .core import StateConflict, atomic_json, fingerprint

VERSION = "document-issue-scope-v1"


def _rows(value):
    return value if isinstance(value, list) else []


def _issue_entries(raw):
    value = raw.get("issues", [])
    if isinstance(value, list):
        return {f"/issues/{i}": issue for i, issue in enumerate(value)}
    # Even a damaged collection stays visible as one document-level notice.
    return {"/issues": value}


def _target_index(raw):
    pointers, refs = {}, defaultdict(list)

    def add(pointer, kind, node, sample=None):
        entry = dict(pointer=pointer, kind=kind, node=node, sample=sample)
        pointers[pointer] = entry
        if isinstance(node, dict) and isinstance(node.get("ref"), str) and node["ref"]:
            refs[node["ref"]].append(entry)

    def facts(node, field, pointer):
        for i, fact in enumerate(_rows(node.get(field))):
            add(f"{pointer}/{field}/{i}", "field", fact)

    for si, study in enumerate(_rows(raw.get("studies"))):
        if not isinstance(study, dict):
            continue
        study_pointer = f"/studies/{si}"
        add(study_pointer, "document", study)
        for i, sample in enumerate(_rows(study.get("samples"))):
            if not isinstance(sample, dict):
                continue
            pointer = f"{study_pointer}/samples/{i}"
            add(pointer, "sample", sample)
            for field in ("composition", "preparation"):
                facts(sample, field, pointer)
            for j, test in enumerate(_rows(sample.get("tests"))):
                if isinstance(test, dict):
                    test_pointer = f"{pointer}/tests/{j}"
                    add(test_pointer, "sample", test, sample=pointer)
                    for field in ("conditions", "results"):
                        facts(test, field, test_pointer)
    for i, method in enumerate(_rows(raw.get("shared_methods"))):
        if isinstance(method, dict):
            pointer = f"/shared_methods/{i}"
            add(pointer, "document", method)
            facts(method, "details", pointer)
    for i, statement in enumerate(_rows(raw.get("statements"))):
        add(f"/statements/{i}", "field", statement)
    return pointers, refs


def _resolve_targets(values, pointers, refs, *, pointer_only=False):
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value for value in values)
        or len(values) != len(set(values))
    ):
        raise ValueError("issue targets must be nonempty unique strings")
    result = []
    for value in values:
        if pointer_only:
            entry = pointers.get(value)
        elif value in refs:
            entry = refs[value][0] if len(refs[value]) == 1 else None
            if entry is not None and value in pointers and entry["pointer"] != value:
                entry = None
        else:
            entry = pointers.get(value)
        if entry is None:
            raise ValueError("issue target is unknown or ambiguous")
        result.append(entry)
    return result


def _targets(scope, values, pointers, refs, *, pointer_only=False):
    if scope not in ("document", "sample", "field"):
        raise ValueError("invalid issue scope")
    if scope == "document":
        if values != []:
            raise ValueError("document issue targets must be empty")
        return []
    entries = _resolve_targets(values, pointers, refs, pointer_only=pointer_only)
    if any(entry["kind"] != scope for entry in entries):
        raise ValueError("issue target kind does not match scope")
    return sorted({entry["sample"] or entry["pointer"] for entry in entries})


def _native_item(pointer, issue, pointers, refs):
    item = dict(issue_pointer=pointer, scope="document", target_pointers=[], origin="legacy")
    if not isinstance(issue, dict):
        return item
    if "scope" in issue or "target_refs" in issue:
        item["origin"] = "schema"
        try:
            targets = _targets(issue.get("scope"), issue.get("target_refs"), pointers, refs)
            item.update(scope=issue["scope"], target_pointers=targets)
        except ValueError:
            item["origin"] = "invalid_schema"
        return item
    try:
        entries = _resolve_targets(issue.get("refs"), pointers, refs)
        kinds = {entry["kind"] for entry in entries}
        if kinds in ({"sample"}, {"field"}):
            scope = next(iter(kinds))
            item.update(scope=scope, target_pointers=sorted({entry["sample"] or entry["pointer"] for entry in entries}))
    except ValueError:
        pass
    return item


def _validate_override_items(raw, items):
    pointers, refs = _target_index(raw)
    issues = _issue_entries(raw)
    if not isinstance(items, list):
        raise ValueError("issue overrides must be a list")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"issue_pointer", "issue_sha256", "scope", "target_pointers"}:
            raise ValueError("invalid issue override item")
        pointer = item["issue_pointer"]
        if not isinstance(pointer, str) or pointer not in issues or pointer == "/issues" or pointer in seen:
            raise ValueError("issue pointer is unknown, malformed or duplicated")
        if item["issue_sha256"] != fingerprint(issues[pointer]):
            raise ValueError("issue fingerprint binding does not match")
        targets = _targets(item["scope"], item["target_pointers"], pointers, refs, pointer_only=True)
        result.append({**item, "target_pointers": targets})
        seen.add(pointer)
    return result


def _semantic_items(raw, items):
    issues = _issue_entries(raw)
    return [
        dict(
            issue_pointer=item["issue_pointer"],
            issue_sha256=fingerprint(issues[item["issue_pointer"]]),
            scope=item["scope"],
            target_pointers=sorted(item["target_pointers"]),
        )
        for item in sorted(items, key=lambda row: row["issue_pointer"])
    ]


def annotation_sha256(raw, annotations):
    """Translation and audit origin never change review semantics."""
    return fingerprint(_semantic_items(raw, annotations["items"]))


def project_issue_annotations(raw, *, candidate_id=None, source_sha256=None, sidecar=None, sidecar_present=False):
    pointers, refs = _target_index(raw)
    issues = _issue_entries(raw)
    items = [_native_item(pointer, issue, pointers, refs) for pointer, issue in issues.items()]
    if sidecar_present or sidecar is not None:
        try:
            if (
                not isinstance(sidecar, dict)
                or sidecar.get("version") != VERSION
                or sidecar.get("candidate_id") != candidate_id
                or sidecar.get("raw_sha256") != fingerprint(raw)
                or sidecar.get("source_sha256") != source_sha256
                or sidecar.get("issues_sha256") != fingerprint(raw.get("issues", []))
            ):
                raise ValueError("sidecar binding does not match")
            overrides = {item["issue_pointer"]: item for item in _validate_override_items(raw, sidecar.get("items"))}
            for item in items:
                if item["issue_pointer"] in overrides:
                    chosen = overrides[item["issue_pointer"]]
                    item.update(scope=chosen["scope"], target_pointers=chosen["target_pointers"], origin="manual")
        except (ValueError, TypeError):
            items = [
                dict(issue_pointer=pointer, scope="document", target_pointers=[], origin="invalid_sidecar")
                for pointer in issues
            ]
    semantics = _semantic_items(raw, items)
    field_sha256 = {
        pointer: fingerprint(
            [item for item in semantics if item["scope"] == "field" and pointer in item["target_pointers"]]
        )
        for pointer, entry in pointers.items()
        if entry["kind"] == "field"
    }
    return dict(version=VERSION, items=items, field_sha256=field_sha256)


def issue_scope_path(root, candidate_id):
    return root / "issue-scopes" / (fingerprint(candidate_id)[:24] + ".json")


def load_issue_annotations(root, raw, candidate_id, source_sha256):
    path = issue_scope_path(root, candidate_id)
    present = path.exists() or path.is_symlink()
    sidecar = None
    if present:
        try:
            if path.is_symlink() or path.parent.is_symlink():
                raise ValueError("unsafe issue sidecar path")
            sidecar = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    return project_issue_annotations(
        raw, candidate_id=candidate_id, source_sha256=source_sha256, sidecar=sidecar, sidecar_present=present
    )


def write_issue_scope_overrides(
    service, task_id, *, expected_candidate_sha256, expected_review_sha256, candidate_id, items, operator
):
    """Replace this group's explicit overrides; no HTTP entrypoint or model call."""
    task = service.store.get_task(task_id)
    with service.store.lock("source:" + task["source_id"]):
        task = service.store.get_task(task_id)
        if task["state"] not in ("needs_review", "failed"):
            raise StateConflict("已发布或历史候选只读；请创建新的审核版本")
        view = service.document_candidates(task_id, offset=0, limit=10000)
        if (
            view.get("candidate_sha256") != expected_candidate_sha256
            or view.get("review_sha256") != expected_review_sha256
        ):
            raise StateConflict("candidate or issue review version changed; reload before maintaining scope")
        group = next((row for row in view.get("groups", []) if row["candidate_id"] == candidate_id), None)
        if not group or group.get("raw_status") != "available":
            raise ValueError("candidate group not found")
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 200:
            raise ValueError("issue scope operator is required")
        checked = _validate_override_items(group["raw"], items)
        task = service.store.get_task(task_id)
        source_hashes = {aid: service.store.get_asset(aid)["sha256"] for aid in task["asset_ids"]}
        for aid, digest in source_hashes.items():
            if sha256(service.store.asset_path(aid).read_bytes()).hexdigest() != digest:
                raise StateConflict("document original integrity failure")
        root = service._document_artifact_root(task)
        path = issue_scope_path(root, candidate_id)
        if path.is_symlink() or path.parent.is_symlink():
            raise StateConflict("unsafe issue sidecar path")
        atomic_json(
            path,
            dict(
                version=VERSION,
                candidate_id=candidate_id,
                raw_sha256=fingerprint(group["raw"]),
                source_sha256=source_hashes,
                issues_sha256=fingerprint(group["raw"].get("issues", [])),
                items=checked,
                operator=operator.strip(),
                updated_at=time.time(),
            ),
        )
        return service.document_candidates(task_id, offset=0, limit=10000)
