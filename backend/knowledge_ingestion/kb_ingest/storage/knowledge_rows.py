"""Serialize knowledge and Wiki rows for epoch-based response and fingerprint contracts."""

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime

from .models import Base


def knowledge_row(row: Base, convert: Callable[[datetime], float] = datetime.timestamp) -> dict[str, object]:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = convert(value) if isinstance(value, datetime) else deepcopy(value)
    return result
