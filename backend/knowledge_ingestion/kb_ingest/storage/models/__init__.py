"""Import every mapped class so the public Base always contains the complete schema."""

from .base import Base
from .entities import KbAttribute, KbEntity, KbRelated
from .ingestion import (
    Asset,
    AssetUpload,
    PackageRevision,
    ReadingVersion,
    ReusableRecipe,
    Source,
    SourceConfirmation,
    SourceOperation,
    TableDraft,
    Task,
)
from .knowledge import (
    KnowledgeDependency,
    KnowledgeEvent,
    KnowledgeEventConsumer,
    KnowledgeItem,
    KnowledgePublication,
    KnowledgeReviewEvent,
    KnowledgeRevision,
    KnowledgeSupportRow,
)
from .knowledge_index import KnowledgeIndexCurrent, KnowledgeIndexEntry, KnowledgeIndexGeneration
from .knowledge_relations import KnowledgeRelation
from .model_request_results import ModelRequestResult
from .model_requests import ModelRequest
from .project_budget import ProjectBudget, ProjectCallReservation
from .study_units import StudyUnit, StudyUnitRevision, StudyWikiJob
from .wiki_embeddings import WikiEmbedding

__all__ = [
    "Asset",
    "AssetUpload",
    "Base",
    "KbAttribute",
    "KbEntity",
    "KbRelated",
    "KnowledgeDependency",
    "KnowledgeEvent",
    "KnowledgeEventConsumer",
    "KnowledgeIndexCurrent",
    "KnowledgeIndexEntry",
    "KnowledgeIndexGeneration",
    "KnowledgeItem",
    "KnowledgePublication",
    "KnowledgeRelation",
    "KnowledgeReviewEvent",
    "KnowledgeRevision",
    "KnowledgeSupportRow",
    "ModelRequest",
    "ModelRequestResult",
    "PackageRevision",
    "ProjectBudget",
    "ProjectCallReservation",
    "ReadingVersion",
    "ReusableRecipe",
    "Source",
    "SourceConfirmation",
    "SourceOperation",
    "StudyUnit",
    "StudyUnitRevision",
    "StudyWikiJob",
    "TableDraft",
    "Task",
    "WikiEmbedding",
]
