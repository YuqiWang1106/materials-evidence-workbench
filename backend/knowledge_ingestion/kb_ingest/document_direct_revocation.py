"""Downstream invalidation for materialized direct nested document records."""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from typing import Any

from .document_direct_quality import (
    _CHILD_DEPENDS_ON_PARENT,
    _CONTEXT_FIELDS,
    _REFERENCE_DEPENDENCIES,
)
from .document_mapping_proof import mapping_dependency_index
from .ingest.documents.document_quality_types import MappingDependencyIndex


def _block_owned_objects(package: dict[str, Any], blocked: set[Hashable]) -> None:
    for obj in package["objects"]:
        if any(obj.get(key) in blocked for key in ("owner_ref", "material_ref", "about_subject_ref", "activity_ref")):
            blocked.add(obj["object_id"])


def _block_direct_relations(package: dict[str, Any], blocked: set[Hashable]) -> None:
    for relation in package["relations"]:
        if relation["predicate"] in _CHILD_DEPENDS_ON_PARENT:
            support, consumer = relation["from_ref"], relation["to_ref"]
        elif relation["predicate"] in _REFERENCE_DEPENDENCIES:
            support, consumer = relation["to_ref"], relation["from_ref"]
        else:
            continue
        if support in blocked or relation["relation_id"] in blocked:
            blocked.add(consumer)


def _block_facts_and_derivations(package: dict[str, Any], blocked: set[Hashable]) -> None:
    for assertion in package["assertions"]:
        if assertion["subject_ref"] in blocked:
            blocked.add(assertion["assertion_id"])
        if assertion["assertion_id"] in blocked and assertion["field_id"] in _CONTEXT_FIELDS:
            blocked.add(assertion["subject_ref"])
    for derivation in package["derivations"]:
        if set(derivation["input_assertion_refs"]) & blocked:
            blocked.add(derivation["output_assertion_ref"])


def _expand_direct_invalidations(
    package: dict[str, Any], blocked: set[Hashable], mapping_dependencies: MappingDependencyIndex
) -> set[Hashable]:
    for issue in package["issues"]:
        if issue["issue_id"] in blocked:
            blocked.update(issue.get("target_refs", []))
    _block_owned_objects(package, blocked)
    _block_direct_relations(package, blocked)
    _block_facts_and_derivations(package, blocked)
    for consumer, dependencies in mapping_dependencies.items():
        if dependencies & blocked:
            blocked.add(consumer)
    return blocked


def direct_invalidated_references(package: dict[str, Any], target_refs: Iterable[Hashable]) -> set[Hashable]:
    """Use the same ownership direction before publication and after review.

    Direct children consume their parent's attribution; methods and materials
    support their referencing consumers. Removing one sample's ownership edge
    therefore cannot revoke an independent sibling or their shared method.
    The input package and its original review receipt remain immutable.
    """
    mapping_dependencies = mapping_dependency_index(package)
    blocked = set(target_refs)
    while True:
        previous = set(blocked)
        blocked = _expand_direct_invalidations(package, blocked, mapping_dependencies)
        if blocked == previous:
            return blocked
