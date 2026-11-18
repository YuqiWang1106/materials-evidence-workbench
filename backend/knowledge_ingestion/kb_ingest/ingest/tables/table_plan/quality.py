"""Replay registered v2 instructions against retained originals, never v1 policy."""

from collections import defaultdict
from hashlib import sha256

from kb_ingest.contract import ContractError, strict_equal
from kb_ingest.core.serialization import canonical

from .compiler import COMPILER_VERSION, QUALITY_VERSION
from .engine import extract_plan


def verify_plan_package(package):
    from ..table_quality import _tables

    header = package["header"]
    if (
        header.get("reading_plan_version") != "2"
        or header.get("compiler_version") != COMPILER_VERSION
        or header.get("table_quality_version") != QUALITY_VERSION
    ):
        raise ContractError("Unsupported v2 reading plan or compiler policy version")
    _, tables = _tables(package)
    by_asset = defaultdict(list)
    for (asset, _), table in tables.items():
        by_asset[asset].append(table)
    try:
        replay = extract_plan(
            [a for a in package["assets"] if a.get("role") != "supplemental_note"],
            header["source_id"],
            header["reading_plan"],
            header["processing_run_id"],
            tables_by_asset=by_asset,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ContractError("V2 source plan cannot be replayed: " + str(exc)) from exc
    recipe = header.get("selected_scope", {})
    confirmation = header.get("reading_confirmation", {})
    if recipe and (
        recipe.get("version") != "2" or not strict_equal(recipe.get("reading_plan"), header["reading_plan"])
    ):
        raise ContractError("Selected scope differs from confirmed v2 plan")
    notes = recipe.get("supplements", [])
    note_assets = [a for a in package["assets"] if a.get("role") == "supplemental_note"]
    if len(notes) != len(note_assets):
        raise ContractError("Supplement count differs from confirmed scope")
    for note, asset in zip(notes, note_assets):
        statement = {
            **note,
            "confirmed_at": confirmation.get("confirmed_at"),
            "confirmation_version_id": confirmation.get("version_id"),
        }
        evidence = [e for e in package["evidence"] if e["asset_ref"] == asset["asset_id"]]
        if (
            asset["sha256"] != sha256(canonical(statement).encode()).hexdigest()
            or len(evidence) != 1
            or not strict_equal(evidence[0].get("content"), statement)
            or evidence[0].get("content_kind") != "human_supplied_statement"
            or evidence[0].get("scientific_use") != "context_only"
        ):
            raise ContractError("Supplement is not the separately confirmed source statement")
    if recipe.get("excluded_ranges"):
        if not confirmation.get("version_id"):
            raise ContractError("Exclusion lacks reading confirmation")
        from ..table_reading import apply_plan_exclusions

        apply_plan_exclusions(replay, recipe["excluded_ranges"], confirmation["version_id"])
    for key in ("plan_hash", "table_plan_summary", "profile_extensions", "source_table_inventory"):
        if not strict_equal(header.get(key), replay["header"].get(key)):
            raise ContractError("V2 reading plan proof differs: " + key)
    # Approval, revision and lifecycle bookkeeping can evolve independently;
    # source assertions and their provenance are immutable within this revision.
    mutable = {"review_state", "independent_validation", "use_status", "issue_refs", "lifecycle_status"}
    for section, idkey in (
        ("objects", "object_id"),
        ("assertions", "assertion_id"),
        ("assessments", "assessment_id"),
        ("relations", "relation_id"),
        ("coverage", "coverage_id"),
    ):
        expected = {x[idkey]: x for x in replay[section]}
        actual = {x[idkey]: x for x in package[section]}
        if set(expected) != set(actual):
            raise ContractError("V2 plan output membership differs: " + section)
        for ident, row in expected.items():
            if row.get("use_status") == "blocked" and actual[ident].get("use_status") != "blocked":
                raise ContractError("V2 scientific proof requires blocked status")
            if not set(row.get("issue_refs", [])) <= set(actual[ident].get("issue_refs", [])):
                raise ContractError("V2 scientific blocking proof lost issue references")
            projection = {k: v for k, v in actual[ident].items() if k not in mutable}
            wanted = {k: v for k, v in row.items() if k not in mutable}
            if not strict_equal(projection, wanted):
                raise ContractError("V2 source-bound proof differs: " + section)
    original_issues = {i["issue_id"]: i for i in package["issues"]}
    for issue in replay["issues"]:
        if issue["issue_id"] not in original_issues or any(
            not strict_equal(original_issues[issue["issue_id"]].get(k), v)
            for k, v in issue.items()
            if k not in ("resolution_status", "treatment")
        ):
            raise ContractError("V2 plan diagnostics were removed or changed")
    return {
        "version": QUALITY_VERSION,
        "verified": True,
        "assertions": len(replay["assertions"]),
        "objects": len(replay["objects"]),
        "relations": len(replay["relations"]),
    }
