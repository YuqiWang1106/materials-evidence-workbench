"""Catalogue-backed domain configuration. No query rewrite or comparability engine."""

import json
from hashlib import sha256
from typing import Literal

from pydantic import Field, model_validator

from .contract import PROFILES
from .core.paths import PACKAGE_ROOT
from .core.serialization import canonical
from .knowledge_contracts import ConditionSelector, Hash, Id, Operation, StrictModel, Version

ROOT = PACKAGE_ROOT / "knowledge_profiles"


class LabelMapping(StrictModel):
    source_label: str
    selector: ConditionSelector
    catalogue_profile_id: Id
    mapping_status: Literal["catalogue_declared"]

    @model_validator(mode="after")
    def catalogue(self):
        p = PROFILES.get(self.catalogue_profile_id)
        if (
            p is None
            or p["field_id"] != self.selector.field_id
            or any(p["qualifiers"].get(k) != v for k, v in self.selector.qualifiers.items())
        ):
            raise ValueError("Mapping differs from catalogue")
        if self.source_label not in p["qualifiers"].values():
            raise ValueError("Source label lacks catalogue support")
        return self


class ObservedTuple(StrictModel):
    # Source metadata such as property is not necessarily an allowed query filter.
    field_id: Literal["observation.value"]
    qualifiers: dict[str, str] = Field(min_length=1, max_length=32)


class ObservedMapping(StrictModel):
    """Whole-tuple task interpretation, never an exact-query alias."""

    id: Id
    version: Version
    observed: ObservedTuple
    canonical: ConditionSelector
    catalogue_profile_id: Id
    sha256: Hash

    @model_validator(mode="after")
    def declared(self):
        if sha256(canonical(self.model_dump(exclude={"sha256"})).encode()).hexdigest() != self.sha256:
            raise ValueError("OBSERVED_MAPPING_MISMATCH")
        p = PROFILES.get(self.catalogue_profile_id)
        if self.observed.field_id != "observation.value" or self.canonical.field_id != self.observed.field_id:
            raise ValueError("Observed mapping must preserve observation field")
        if (
            not self.observed.qualifiers
            or not self.canonical.qualifiers
            or p is None
            or p["field_id"] != self.canonical.field_id
        ):
            raise ValueError("Observed mapping lacks catalogue support")
        if any(p["qualifiers"].get(k) != v for k, v in self.canonical.qualifiers.items()):
            raise ValueError("Observed mapping differs from catalogue")
        return self


class DomainProfile(StrictModel):
    id: Version
    catalogue_version: Version
    sample_identity_verified_by_collection: Literal[False]
    mappings: list[LabelMapping] = Field(default_factory=list)
    observed_mappings: list[ObservedMapping] = Field(default_factory=list)
    unsupported: list[Id]
    category_aliases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_observed_rules(self):
        ids = set()
        tuples = set()
        for rule in self.observed_mappings:
            key = canonical(rule.observed.model_dump())
            if rule.id in ids or key in tuples:
                raise ValueError("Ambiguous observed mapping")
            ids.add(rule.id)
            tuples.add(key)
        return self


class ComparisonCondition(StrictModel):
    selector: ConditionSelector
    required: bool
    rule: Literal["same_raw", "same_unit_basis", "source_explicit_relation"]


class ComparisonProfile(StrictModel):
    id: Id
    version: Version
    sha256: Hash
    required_conditions: list[ComparisonCondition]
    allowed_differences: list[ConditionSelector]
    allowed_operations: list[Operation]
    vocabulary: Version
    scientific_validation: Literal[False]
    training_admission: Literal[False]

    @model_validator(mode="after")
    def integrity(self):
        data = self.model_dump(exclude={"sha256"})
        if sha256(canonical(data).encode()).hexdigest() != self.sha256:
            raise ValueError("COMPARISON_PROFILE_MISMATCH")
        if not self.required_conditions:
            raise ValueError("Comparison profile requires conditions")
        return self


def load_domain_profile() -> DomainProfile:
    return DomainProfile.model_validate_json((ROOT / "geometry_v1.json").read_text(encoding="utf-8"))


def load_comparison_profiles() -> list[ComparisonProfile]:
    payload = json.loads((ROOT / "comparability_geometry_v1.json").read_text(encoding="utf-8"))
    return [ComparisonProfile.model_validate(p) for p in payload]


def map_label(source_label: str) -> LabelMapping | None:
    """Explicit spelling only; retains source label, never normalizes query eq."""
    return next((m for m in load_domain_profile().mappings if m.source_label == source_label), None)


def resolve_comparison_profile(profile_id: str, version: str, digest: str) -> ComparisonProfile:
    p = next((p for p in load_comparison_profiles() if p.id == profile_id and p.version == version), None)
    if p is None:
        raise ValueError("COMPARISON_PROFILE_UNSUPPORTED")
    if p.sha256 != digest:
        raise ValueError("COMPARISON_PROFILE_MISMATCH")
    return p
