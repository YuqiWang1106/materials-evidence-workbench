"""Bounded model selections and explicit research-to-Wiki user commands."""

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .knowledge_contracts import Hash, Id, StrictModel
from .knowledge_summary_contracts import SECTIONS
from .research_chat_evidence import EditorialBlock, SelectedClaim


class UserRequirement(StrictModel):
    kind: Literal["user_requirement"]
    turn_id: Id


Block = Annotated[Union[SelectedClaim, EditorialBlock, UserRequirement], Field(discriminator="kind")]


class SummarySection(StrictModel):
    section_id: Id
    blocks: list[Block] = Field(min_length=1, max_length=20)


class SummaryDraft(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    sections: list[SummarySection] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def ordered(self):
        if [s.section_id for s in self.sections] != [s for s, _ in SECTIONS]:
            raise ValueError("INVALID_SUMMARY_TEMPLATE")
        return self


class SummaryRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=160)
    end_turn_id: Id
    max_turns: int = Field(default=20, ge=1, le=20, strict=True)
    target_page_id: Id | None = None
    base_revision: int | None = Field(default=None, ge=1, strict=True)
    base_content_hash: Hash | None = None

    @model_validator(mode="after")
    def target(self):
        if bool(self.target_page_id) != (self.base_revision is not None and self.base_content_hash is not None):
            raise ValueError("TARGET_BASE_REQUIRED")
        if not self.target_page_id and (self.base_revision is not None or self.base_content_hash is not None):
            raise ValueError("TARGET_BASE_REQUIRED")
        return self
