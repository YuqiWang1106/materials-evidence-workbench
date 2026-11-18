"""Public write-transaction entry for SQLite-backed stores."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from .sqlite import begin_immediate


@contextmanager
def begin_write(engine: Engine) -> Iterator[Session]:
    """Open BEGIN IMMEDIATE, commit on success, and roll back on error.

    Callers that already hold a write transaction must pass that Session into the
    operation; this helper must not commit a caller-owned session.
    """
    with Session(engine) as session:
        begin_immediate(session)
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


@contextmanager
def write_session(engine: Engine, session: Session | None) -> Iterator[Session]:
    """Join an existing write transaction, or open begin_write and own commit."""
    if session is not None:
        yield session
        return
    with begin_write(engine) as owned:
        yield owned


@contextmanager
def rehearse_write(engine: Engine) -> Iterator[Session]:
    """Open BEGIN IMMEDIATE for a dry-run that always rolls back.

    The caller may write and read through the yielded session; nothing becomes
    visible. This helper must not commit.
    """
    with Session(engine) as session:
        begin_immediate(session)
        try:
            yield session
        finally:
            session.rollback()
