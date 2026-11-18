"""Server-owned publication policies; legacy research remains manually reviewed."""

POLICY_VERSION = "table-wiki-auto-publish-v1"
UNIFIED_INPUT_POLICY_VERSION = "unified-research-summary-v1"
UNIFIED_POLICY_VERSION = "unified-research-summary-auto-publish-v1"


def is_table_summary(revision):
    manifest = revision.get("dependency_manifest", {})
    return (
        isinstance(manifest.get("summary"), dict) and bool(manifest["summary"]) and not manifest.get("research_summary")
    )


def is_unified_summary(revision):
    manifest = revision.get("dependency_manifest", {})
    research = manifest.get("research_summary")
    return isinstance(research, dict) and research.get("input_policy_version") == UNIFIED_INPUT_POLICY_VERSION


def accepted_review(review, revision):
    if review is None:
        return False
    decision = review["decision"] if isinstance(review, dict) else review.decision
    reviewer = review["reviewer"] if isinstance(review, dict) else review.reviewer
    from .study_wiki_policy import POLICY_VERSION as STUDY_POLICY
    from .study_wiki_policy import is_study_summary

    return (
        decision == "approved"
        or (decision == "auto_accepted" and reviewer == STUDY_POLICY and is_study_summary(revision))
        or (decision == "auto_accepted" and reviewer == POLICY_VERSION and is_table_summary(revision))
        or (decision == "auto_accepted" and reviewer == UNIFIED_POLICY_VERSION and is_unified_summary(revision))
    )
