"""Preserve already-reviewed direct-source records across later model passes."""

from __future__ import annotations

from copy import deepcopy

from kb_ingest.core import fingerprint


def _object_context(value, reference=None):
    """Identify explicit nested subjects using their complete candidate context.

    A correction may append evidence. Match only an identical scoped node whose
    previously cited evidence is still present; labels alone never match nodes.
    """
    from collections import Counter

    objects = {o["key"]: o for o in value["objects"]}
    result = {}
    by_core = {}
    for item in (reference or {}).values():
        by_core.setdefault(item["core"], []).append(item)

    def resolve(key):
        if key is None:
            return None
        if key not in objects:
            return {"unresolved": key}
        if key in result:
            return result[key]["identity"]
        obj = objects[key]
        parents = [p for p in objects if key.startswith(p + "/")]
        parent = max(parents, key=len) if parents else None
        core = fingerprint(
            {
                **{
                    k: v
                    for k, v in obj.items()
                    if k not in ("key", "scope", "owner_key", "material_key", "evidence", "identity_evidence")
                },
                "parent_scope": resolve(parent),
            }
        )
        evidence = Counter(
            fingerprint([field, e]) for field in ("evidence", "identity_evidence") for e in obj.get(field, [])
        )
        matches = {row["identity"] for row in by_core.get(core, []) if not row["evidence"] - evidence}
        if len(matches) > 1:
            raise ValueError("ambiguous source identity during correction")
        identity = next(iter(matches)) if matches else fingerprint({"core": core, "evidence": evidence})
        result[key] = {"core": core, "evidence": evidence, "identity": identity}
        return identity

    for key in objects:
        resolve(key)
    return result


def _preservation_records(value, *, prior=False, context=None, reference=None):
    """Protect supported records, using full context rather than a filtered tree."""
    from collections import Counter

    context = value if context is None else context
    identities = _object_context(context, reference)
    objects = {o["key"]: o for o in value["objects"]}
    facts = {f["key"]: f for f in value["facts"]}
    fact_ids = {}
    visiting = set()

    def object_ref(key):
        if key is None:
            return None
        return identities[key]["identity"] if key in identities else {"unresolved": key}

    def fact_ref(key):
        if key in fact_ids:
            return fact_ids[key]
        if key not in facts or key in visiting:
            return {"unresolved": key}
        visiting.add(key)
        fact = {k: v for k, v in facts[key].items() if k not in ("key", "object_key", "input_fact_keys", "evidence")}
        fact["object"] = object_ref(facts[key].get("object_key"))
        fact["inputs"] = [fact_ref(k) for k in facts[key].get("input_fact_keys", [])]
        visiting.remove(key)
        fact_ids[key] = fingerprint(fact)
        return fact_ids[key]

    missing = (
        [r for r in value.get("missing_records", []) if r["status"] == "not_reported"]
        if prior
        else value.get("unreported_fields", [])
    )
    source_issues = [i for i in value.get("issues", []) if i.get("origin") != "direct_conversion"]
    needed = {f["object_key"] for f in facts.values()}
    needed.update(k for r in value["relations"] for k in (r["from_key"], r["to_key"]))
    needed.update(r.get("object_key") for r in missing)
    needed.update(k for issue in source_issues for k in issue.get("object_keys", []))
    # Keep accepted context objects that support accepted records. Empty filtered shells are not correction constraints.
    while True:
        before = set(needed)
        for key in before:
            if key not in objects:
                continue
            needed.update(p for p in objects if key.startswith(p + "/"))
            needed.update(objects[key].get(k) for k in ("owner_key", "material_key"))
        if needed == before:
            break
    objects = {k: o for k, o in objects.items() if k in needed}
    ids = [object_ref(k) for k in objects]
    if len(set(ids)) != len(ids):
        raise ValueError("ambiguous repeated object identity during correction")
    rows = {
        "objects": {"o:" + k: o for k, o in objects.items()},
        "facts": {"f:" + k: f for k, f in facts.items()},
        "relations": {"r:" + str(i): r for i, r in enumerate(value["relations"])},
        "missing": {(r.get("target", "m:" + str(i)) if prior else "m:" + str(i)): r for i, r in enumerate(missing)},
    }
    sections = {
        "objects": {
            "o:" + k: fingerprint(
                {
                    "identity": object_ref(k),
                    "owner": object_ref(o.get("owner_key")),
                    "material": object_ref(o.get("material_key")),
                }
            )
            for k, o in objects.items()
        },
        "facts": {"f:" + k: fingerprint(fact_ref(k)) for k in facts},
        "relations": {
            "r:" + str(i): fingerprint(
                {
                    **{k: v for k, v in r.items() if k not in ("from_key", "to_key", "evidence")},
                    "from": object_ref(r["from_key"]),
                    "to": object_ref(r["to_key"]),
                }
            )
            for i, r in enumerate(value["relations"])
        },
        "missing": {
            key: fingerprint(
                {"object": object_ref(r.get("object_key")), **{k: r.get(k) for k in ("field_id", "reason")}}
            )
            for key, r in rows["missing"].items()
        },
    }
    counts = {section: Counter(items.values()) for section, items in sections.items()}
    for section, items in sections.items():
        counts[section + "_evidence"] = Counter(
            fingerprint([signature, field, e])
            for key, signature in items.items()
            for field in ("evidence", "identity_evidence")
            for e in rows[section][key].get(field, [])
        )
    counts["source_issues"] = Counter(
        fingerprint(
            {
                **{k: v for k, v in issue.items() if k not in ("object_keys", "origin")},
                "objects": sorted(fingerprint(object_ref(k)) for k in issue.get("object_keys", [])),
            }
        )
        for issue in source_issues
    )
    targets = {key: (section, signature) for section, items in sections.items() for key, signature in items.items()}
    return counts, targets


def preserves_reviewed(candidate, accepted, *, original=None, candidate_context=None):
    """Preserve supported values/ownership and all old evidence; permit additions."""
    original = accepted if original is None else original
    try:
        reference = _object_context(original)
        old, _ = _preservation_records(accepted, prior=True, context=original)
        new, _ = _preservation_records(candidate, context=candidate_context, reference=reference)
    except ValueError:
        return False
    return not any(old[section] - new[section] for section in old)


def correction_support_view(candidate, accepted, report, blocks):
    """Replay explicit prior support only for facts that actually passed review.

    This view is for preservation checks. The new source review still receives
    the untouched candidate and must independently choose support again.
    """
    from kb_ingest.document_evidence_context import replay_review_support

    accepted_keys = {"f:" + row["key"] for row in accepted["facts"]}
    trace = [row for row in report.get("evidence_binding", []) if row["target"] in accepted_keys]
    return replay_review_support(candidate, trace, blocks)


def _retain_source_issues(candidate, previous):
    """Carry original source qualifications independently of correction wording."""
    from collections import Counter

    result = deepcopy(candidate)
    old = _object_context(previous)
    new = _object_context(candidate, old)
    keys = {}
    for key, item in new.items():
        keys.setdefault(item["identity"], []).append(key)
    present = Counter(fingerprint(i) for i in result.get("issues", []))
    for issue in previous.get("issues", []):
        if issue.get("origin") == "direct_conversion":
            continue
        retained = deepcopy(issue)
        retained["object_keys"] = []
        for key in issue.get("object_keys", []):
            matches = keys.get(old.get(key, {}).get("identity"), [])
            if len(matches) == 1:
                retained["object_keys"].append(matches[0])
            elif key not in old:
                retained["object_keys"].append(key)
            else:
                # Do not bind a vanished source qualification to an array slot.
                raise ValueError("source issue subject changed or became ambiguous during correction")
        signature = fingerprint(retained)
        if present[signature]:
            present[signature] -= 1
        else:
            result.setdefault("issues", []).append(retained)
    return result


def _retained_review_disagreements(accepted, candidate, previous_report, report, *, original=None):
    """Map actually checked/rejected targets back to preserved source records.

    Missing responses, including a budget stop, never manufacture a rejection.
    A supported response can still fail a required unit or source check; its
    recorded program rejection is also a disagreement with the earlier pass.
    """
    original = accepted if original is None else original
    _, old = _preservation_records(accepted, prior=True, context=original)
    _, new = _preservation_records(candidate, reference=_object_context(original))
    old_by_content = {}
    for key, identity in old.items():
        old_by_content.setdefault(identity, []).append(key)
    rejected = {row["target"]: row for row in report.get("direct_rejected", [])}
    result = {}
    for check in report.get("received_checks", []):
        key = check["target"]
        if key not in rejected or key not in new:
            continue
        for prior_key in old_by_content.get(new[key], []):
            result.setdefault(
                prior_key,
                {
                    "target": prior_key,
                    "reviewed_target": key,
                    "verdict": check.get("verdict"),
                    "unit_complete": check.get("unit_complete"),
                    "reason": rejected[key]["reason"],
                    "review_reason": check.get("reason"),
                    "candidate_sha256": report["candidate_sha256"],
                    "review_sha256": report["review_sha256"],
                    "prior_candidate_sha256": previous_report["candidate_sha256"],
                    "prior_review_sha256": previous_report["review_sha256"],
                },
            )
    return list(result.values())
