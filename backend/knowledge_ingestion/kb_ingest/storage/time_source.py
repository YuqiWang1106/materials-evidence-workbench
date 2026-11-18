"""Retain the verified instant alongside an epoch-valued service view."""

from __future__ import annotations

from typing import Self

from .file_time import epoch_seconds


class _SourcedEpoch(float):
    source: object

    def __new__(cls, value: float, source: object) -> Self:
        result = super().__new__(cls, value)
        result.source = source
        return result

    def __reduce__(self) -> tuple[type[Self], tuple[float, object]]:
        return type(self), (float(self), self.source)


def sourced_epoch(value: object) -> float:
    """Keep numeric consumers and JSON hashes on their existing epoch representation."""
    if isinstance(value, _SourcedEpoch):
        return value
    return _SourcedEpoch(epoch_seconds(value), value)


def source_instant(value: object) -> object:
    """Use the source from the same read when rendering a website clock field."""
    return value.source if isinstance(value, _SourcedEpoch) else value
