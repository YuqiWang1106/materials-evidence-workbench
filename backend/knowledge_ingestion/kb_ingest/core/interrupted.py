"""Exclusive private snapshots for processing interrupted by an unresolved model call."""

import os
import tempfile
import uuid
from pathlib import Path

from .serialization import canonical


def save_interrupted(work: Path, payload: object) -> Path:
    """Persist one partial result without replacing any prior snapshot."""
    target = work / f"entity-interrupted-{uuid.uuid4().hex}.json"
    content = (canonical(payload) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=".interrupted-", dir=work)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        directory = os.open(work, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return target
