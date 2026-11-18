"""Conversions at JSON-file, chronological ordering and public API boundaries."""

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from fractions import Fraction

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_INVALID = "Expected epoch seconds or an aware timestamp"
_ISO_TIME = re.compile(
    r"(?P<date>\d{4}-?(?:\d{2}-?\d{2}|W\d{2}(?:-?\d)?))."
    r"(?P<clock>\d{2}(?::?\d{2}){0,2})(?P<fraction>[.,]\d+)?"
    r"(?P<zone>Z|[+-]\d{2}(?::?\d{2}){0,2})(?P<offset_fraction>[.,]\d+)?",
    re.DOTALL,
)


def _precise_seconds(text: str) -> Fraction | None:
    """Recover ISO fractions beyond datetime's precision, including fractional UTC offsets."""
    match = _ISO_TIME.fullmatch(text)
    if match is None:
        return None
    fraction = match["fraction"] or ""
    offset = match["offset_fraction"] or ""
    if len(fraction) <= 7 and not offset:
        return None
    whole = datetime.fromisoformat(match["date"] + "T" + match["clock"] + match["zone"])
    seconds = (whole - _EPOCH) // timedelta(seconds=1)
    adjustment = Fraction("0." + offset[1:])
    if match["zone"].startswith("-"):
        adjustment = -adjustment
    return seconds + Fraction("0." + fraction[1:]) - adjustment


def _parse(value: object) -> tuple[datetime, Fraction | None]:
    """Return the UTC datetime and, for legacy values, the original ordering precision."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, UTC), Fraction(str(value))
        instant = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(instant, datetime) or instant.utcoffset() is None:
            raise ValueError(_INVALID)
        if isinstance(value, str) and (seconds := _precise_seconds(value)) is not None:
            return _EPOCH + (seconds * 1_000_000 // 1) * _MICROSECOND, seconds
        return instant.astimezone(UTC), None
    except (OverflowError, OSError) as exc:
        raise ValueError(_INVALID) from exc


def utc_datetime(value: object) -> datetime:
    return _parse(value)[0]


def utc_text(value: object) -> str:
    return utc_datetime(value).isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"


def time_order(value: object) -> Fraction:
    """Keep the original resolution when ordering mixed JSON numbers and ISO timestamps."""
    instant, seconds = _parse(value)
    if seconds is not None:
        return seconds
    return Fraction((instant - _EPOCH) // _MICROSECOND, 1_000_000)


def epoch_seconds(value: object) -> float:
    return float(time_order(value))


def record_times(record: Mapping[str, object], convert: Callable[[object], object]) -> dict[str, object]:
    return {**record, "created_at": convert(record["created_at"]), "updated_at": convert(record["updated_at"])}


def public_times(record: Mapping[str, object], convert: Callable[[object], float] = epoch_seconds) -> dict[str, float]:
    return {key: convert(record[key]) for key in ("created_at", "updated_at") if key in record}
