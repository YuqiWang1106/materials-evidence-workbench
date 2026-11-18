"""Transient model selections; compile into the existing answer, never new facts."""

from typing import Annotated, Literal, Union

from pydantic import Field, TypeAdapter

from .core.serialization import canonical
from .knowledge_contracts import Id, StrictModel
from .research_chat_evidence import AnswerDraft, EditorialBlock, SelectedClaim

MAX_WIRE_BYTES = 16_000


class TextSegment(StrictModel):
    kind: Literal["text"]
    text: str = Field(
        min_length=1,
        max_length=3000,
        description="Explanation only; no scientific values, units or placeholder syntax.",
    )


class ReferenceSegment(StrictModel):
    kind: Literal["fact", "label"]
    id: Id = Field(description="An actually read fact ID; the server supplies its value or source label.")


class WireEditorialBlock(StrictModel):
    kind: Literal["editorial", "hypothesis", "question", "limitation"]
    segments: list[Annotated[Union[TextSegment, ReferenceSegment], Field(discriminator="kind")]] = Field(
        min_length=1, max_length=40
    )


WireBlock = Annotated[Union[SelectedClaim, WireEditorialBlock], Field(discriminator="kind")]
_BLOCK = TypeAdapter(WireBlock)


class WireAnswerDraft(StrictModel):
    blocks: list[WireBlock] = Field(min_length=1, max_length=40)
    focus_ids: list[Id] = Field(default_factory=list, max_length=20)


def bounded_wire(draft):
    if len(canonical(draft.model_dump(mode="json")).encode("utf-8")) > MAX_WIRE_BYTES:
        raise ValueError("ANSWER_WIRE_LIMIT")


def compile_block(raw, registry):
    """Bind references without mutating the registry or inspecting source prose."""
    block = _BLOCK.validate_python(raw)
    if isinstance(block, SelectedClaim):
        registry.item(block.id, block.kind)
        return block
    fragments = []
    related = []
    for segment in block.segments:
        if isinstance(segment, TextSegment):
            # Even split text segments cannot manufacture the server token syntax.
            if "{" in segment.text or "}" in segment.text:
                raise ValueError("MODEL_PLACEHOLDER_FORBIDDEN")
            fragments.append(segment.text)
        else:
            registry.item(segment.id, "fact")
            fragments.append("{{" + segment.kind + ":" + segment.id + "}}")
            if segment.id not in related:
                related.append(segment.id)
    # No truncation or correction. Old text/dependency bounds still apply, and
    # the caller must use the existing renderer on the complete joined text.
    return EditorialBlock(kind=block.kind, text="".join(fragments), related_ids=related)


def compile_answer(raw, registry):
    draft = WireAnswerDraft.model_validate(raw)
    bounded_wire(draft)
    return AnswerDraft(blocks=[compile_block(b, registry) for b in draft.blocks], focus_ids=draft.focus_ids)
