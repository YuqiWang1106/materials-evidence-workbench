"""UTC wall-clock instants for SQLAlchemy columns.

``UtcDateTime`` accepts only aware ``datetime`` values, converts them to UTC,
and stores fixed-precision ISO-8601 text. The type ignores the SQLAlchemy
dialect: every backend stores the same text. Database columns and table-chat files share the format:

    YYYY-MM-DDTHH:MM:SS.ffffffZ

Every stored value uses this exact 27-character layout: zero-padded
components, six microsecond digits, an uppercase ``T`` separator, and an
uppercase ``Z`` suffix, with no offset and no variable fractional width.
Lexicographic order of the stored text is therefore the same as chronological
order. ``NULL`` stays ``NULL``. Values read back are always aware UTC. Naive
datetimes and any other stored shape are rejected: the text must have length
27, literal ``T`` and ``Z``, and re-encoding must reproduce it exactly.
"""

from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

_UTC = UTC
_STORED_LENGTH = 27
_NAIVE = "UtcDateTime rejects naive datetime"
_WRONG_TYPE = "UtcDateTime only accepts an aware datetime or None"
_WRONG_STORED = "UtcDateTime stored value must be YYYY-MM-DDTHH:MM:SS.ffffffZ"


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(_WRONG_TYPE)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(_NAIVE)
    return value.astimezone(_UTC)


def encode_utc(value: object) -> str:
    return _as_utc(value).isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"


def decode_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError(_WRONG_STORED)
    if len(value) != _STORED_LENGTH or value[10] != "T" or value[-1] != "Z":
        raise ValueError(_WRONG_STORED)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(_WRONG_STORED) from error
    if encode_utc(parsed) != value:
        raise ValueError(_WRONG_STORED)
    return parsed


class UtcDateTime(TypeDecorator[datetime]):
    """Aware UTC datetime stored as fixed-precision ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` text.

    Dialect is ignored; all backends persist this same ISO text.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: object, dialect: Dialect) -> str | None:
        return None if value is None else encode_utc(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        return None if value is None else decode_utc(value)
