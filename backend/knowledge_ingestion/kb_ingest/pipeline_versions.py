"""Bump the selected pipeline version when its processing semantics change."""

PIPELINE_VERSION = "ingestion-pipeline-v1"
DOCUMENT_PIPELINE_VERSION = "document-pipeline-v3"
DIRECT_PIPELINE_VERSION = "direct-document-pipeline-v1"
CONTROLLED_PIPELINE_VERSION = "controlled-document-pipeline-v1"
PROGRAM_PIPELINE_VERSION = "program-document-pipeline-v1"
ENTITY_PIPELINE_VERSION = "entity-pipeline-v5"


def document_pipeline_version(mode: str, strategy: str) -> str:
    if mode == "direct-six-v1" and strategy == "mineru_text_full_pdf":
        return CONTROLLED_PIPELINE_VERSION
    return {
        "legacy": DOCUMENT_PIPELINE_VERSION,
        "direct-six-v1": DIRECT_PIPELINE_VERSION,
        "program-only-v1": PROGRAM_PIPELINE_VERSION,
        "entity-graph-v1": ENTITY_PIPELINE_VERSION,
    }[mode]
