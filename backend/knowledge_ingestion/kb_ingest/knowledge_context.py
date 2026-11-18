"""Read the exact existing query candidate grain; never widen ownership."""

from .ingest.tables.table_quality import inactive
from .knowledge_snapshot import KnowledgeIntegrityError
from .query import query_context_candidates


def context_assertions(record, ref):
    grains = [
        g
        for g in query_context_candidates(record.projected_package)
        if g["sample_ref"] == ref.sample_ref
        and g["test_ref"] == ref.test_ref
        and (g["run_ref"] is None)
        and (g["series_ref"] is None)
    ]
    if len(grains) != 1:
        raise KnowledgeIntegrityError("REFERENCE_NOT_FOUND")
    return [a for a in grains[0]["candidates"] if not inactive(a)]
