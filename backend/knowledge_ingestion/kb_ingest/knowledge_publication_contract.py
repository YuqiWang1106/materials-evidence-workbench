"""Publication commands bind a human-reviewed, exact Wiki revision."""

from pydantic import Field

from .knowledge_contracts import Hash, Id, StrictModel


class PublicationRequest(StrictModel):
    request_id: Id
    expected_revision: int = Field(ge=1)
    content_hash: Hash
    dependency_manifest_hash: Hash


class PublicationPreviewRequest(StrictModel):
    expected_revision: int | None = Field(default=None, ge=1)
