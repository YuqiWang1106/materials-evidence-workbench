"""Shared editorial knowledge contracts; human review is not scientific validation."""

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .knowledge_contracts import Id, KnowledgeRef, StrictModel


class KnowledgeClaim(StrictModel):
    claim_id: Id
    claim_type: Literal["editorial_synthesis", "source_description", "computed_observation", "reported_measurement"]
    text: str = Field(min_length=1, max_length=64000)
    support_ids: list[Id] = Field(default_factory=list, max_length=200)
    scientific_validation: Literal[False] = False
    research_provenance: dict[str, JsonValue] | None = None


class KnowledgeSection(StrictModel):
    section_id: Id
    heading: str = Field(min_length=1, max_length=300)
    claims: list[KnowledgeClaim] = Field(default_factory=list, max_length=200)


class WikiContent(StrictModel):
    template_version: Literal["experimental-analysis-v1", "study-unit-wiki-v1"] | None = None
    title: str = Field(min_length=1, max_length=300)
    research_question: str = Field(default="", max_length=8000)
    sections: list[KnowledgeSection] = Field(default_factory=list, max_length=200)
    conditions: list[str] = Field(default_factory=list, max_length=200)
    uncertainties: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def identities(self):
        sections = [s.section_id for s in self.sections]
        claims = [c.claim_id for s in self.sections for c in s.claims]
        if len(set(sections)) != len(sections) or len(set(claims)) != len(claims):
            raise ValueError("Duplicate knowledge identity")
        return self


class KnowledgeSupport(StrictModel):
    support_id: Id
    ref: KnowledgeRef
    support_type: Literal["source_description", "calculation", "source_passage", "reported_evidence"]
    scope: dict[str, JsonValue] | None = None
    research_entry_id: Id | None = None


class WikiConflict(ValueError):
    """Stable safe public reason; no provider payload or local paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)
