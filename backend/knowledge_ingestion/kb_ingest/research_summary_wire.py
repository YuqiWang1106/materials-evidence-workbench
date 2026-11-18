"""Transient seven-section model wire; saved Wiki proofs keep their old shape."""

from typing import Annotated, Union

from pydantic import Field

from .knowledge_contracts import Id, StrictModel
from .research_chat_answer_wire import WireEditorialBlock, bounded_wire, compile_block
from .research_chat_evidence import EvidenceRegistry, SelectedClaim
from .research_summary_contracts import SummaryDraft, UserRequirement


class WireSummarySection(StrictModel):
    section_id: Id
    blocks: list[Annotated[Union[SelectedClaim, WireEditorialBlock, UserRequirement], Field(discriminator="kind")]] = (
        Field(min_length=1, max_length=20)
    )


class WireSummaryDraft(SummaryDraft):
    # Inherit the exact title bounds and seven-section ordering validator.
    sections: list[WireSummarySection] = Field(min_length=7, max_length=7)


def compile_summary(raw, snapshot):
    draft = WireSummaryDraft.model_validate(raw)
    bounded_wire(draft)
    context = snapshot["manifest"]["research_summary"]
    registry = EvidenceRegistry(context["selection"], prior=context["registry"])
    return SummaryDraft(
        title=draft.title,
        sections=[
            {
                "section_id": section.section_id,
                "blocks": [b if isinstance(b, UserRequirement) else compile_block(b, registry) for b in section.blocks],
            }
            for section in draft.sections
        ],
    )
