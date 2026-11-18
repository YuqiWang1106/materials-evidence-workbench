"""Public confirmation operations: dry-run claim projection and atomic graph publication."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from kb_ingest.core import StateConflict
from kb_ingest.core.serialization import fingerprint
from kb_ingest.storage.task_repository import Publication, finish_publish
from kb_ingest.storage.transactions import begin_write, rehearse_write

from .entity_store_index import load_index_corpus
from .entity_store_types import ClaimRef, IndexCorpus
from .entity_store_write import WriteCounts


class ClaimsChanged(StateConflict):
    def __init__(self) -> None:
        super().__init__("Claims changed during confirmation; reload before confirming")


class PublicationWorkspaceConflict(StateConflict):
    def __init__(self) -> None:
        super().__init__("Publication plan workspace does not match the entity store")


@dataclass(frozen=True)
class TaskPublication:
    workspace_id: str
    task_id: str
    receipt: Mapping[str, object]
    progress: str | None
    make_current: bool = True
    # The task's line once the write is known: what was written, not what was planned.
    describe: Callable[[WriteCounts], str] | None = None


@dataclass(frozen=True)
class ConfirmationClaims:
    groups: Mapping[str, str | None]
    expected: Sequence[ClaimRef]


class _Confirmable(Protocol):
    engine: Engine
    workspace_id: str | None

    def write(
        self, workspace_id: str, source_id: str, resolved: Mapping[str, object], *, session: Session | None = None
    ) -> WriteCounts: ...

    def claims_by_refs(self, workspace_id: str, *, session: Session | None = None) -> list[ClaimRef]: ...

    def set_claim_groups(
        self, workspace_id: str, groups: dict[str, str | None], *, session: Session | None = None
    ) -> None: ...


class EntityConfirmation:
    """Mixin: project claims with a rolled-back write, then confirm graph and task together."""

    engine: Engine

    def index_corpus(self, workspace_id: str) -> IndexCorpus:
        return load_index_corpus(self.engine, workspace_id)

    def project_confirmation_claims(
        self: _Confirmable, workspace_id: str, source_id: str, graph: Mapping[str, object]
    ) -> list[ClaimRef]:
        """Write the graph, read claim refs, and roll back so no draft rows become visible."""
        with rehearse_write(self.engine) as session:
            self.write(workspace_id, source_id, graph, session=session)
            return self.claims_by_refs(workspace_id, session=session)

    def confirm_source_graph(
        self: _Confirmable,
        workspace_id: str,
        source_id: str,
        graph: Mapping[str, object],
        *,
        claims: ConfirmationClaims | None = None,
        publication: TaskPublication | None = None,
    ) -> None:
        """Write the graph, optional claim groups, and task publication in one committed transaction.

        workspace_id is the graph space (study/wiki, or the stable store default). It may
        differ from publication.workspace_id, which must equal this store's workspace_id.
        """
        if publication is not None:
            _require_publication_workspace(self, publication)
        with begin_write(self.engine) as session:
            written = self.write(workspace_id, source_id, graph, session=session)
            if claims is not None:
                _require_expected_claims(self.claims_by_refs(workspace_id, session=session), claims.expected)
                self.set_claim_groups(workspace_id, dict(claims.groups), session=session)
            if publication is not None:
                progress = publication.describe(written) if publication.describe else publication.progress
                receipt = {**publication.receipt, "written": asdict(written)}
                finish_publish(
                    session,
                    publication.workspace_id,
                    publication.task_id,
                    Publication(receipt, publication.make_current, progress),
                )


def _claim_fingerprint(rows: Sequence[ClaimRef]) -> str:
    return fingerprint(sorted(rows, key=lambda row: row["attribute_id"]))


def _require_expected_claims(current: Sequence[ClaimRef], expected: Sequence[ClaimRef]) -> None:
    if _claim_fingerprint(current) != _claim_fingerprint(expected):
        raise ClaimsChanged


def _require_publication_workspace(store: _Confirmable, publication: TaskPublication) -> None:
    if store.workspace_id is None or publication.workspace_id != store.workspace_id:
        raise PublicationWorkspaceConflict
