"""Explicit automatic editorial publication; never scientific source approval."""

POLICY_VERSION = "study-unit-wiki-auto-publish-v1"
RULE_VERSION = "study-unit-wiki-v1"


def is_study_summary(revision):
    manifest = revision.get("dependency_manifest", {})
    binding = manifest.get("study_unit")
    return (
        isinstance(binding, dict)
        and binding.get("publication_policy") == POLICY_VERSION
        and bool(binding.get("unit_id"))
        and not manifest.get("summary")
        and not manifest.get("research_summary")
    )
