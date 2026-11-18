"""ISO temporal identity used when fingerprinting typed table values."""

from datetime import datetime, time


def temporal_identity(kind, canonical):
    """Match ISO temporal equality without assigning an unstated timezone."""
    if kind == "datetime":
        parsed = datetime.fromisoformat(canonical)
        micros = ((parsed.hour * 60 + parsed.minute) * 60 + parsed.second) * 1_000_000 + parsed.microsecond
        micros += parsed.toordinal() * 86_400_000_000
        offset = parsed.utcoffset()
    else:
        parsed_time = time.fromisoformat(canonical)
        micros = (
            (parsed_time.hour * 60 + parsed_time.minute) * 60 + parsed_time.second
        ) * 1_000_000 + parsed_time.microsecond
        offset = parsed_time.utcoffset()
    if offset is not None:
        micros -= (offset.days * 86400 + offset.seconds) * 1_000_000 + offset.microseconds
    # A time has no date: do not wrap an offset across midnight or invent a day.
    return ["aware" if offset is not None else "naive", micros]
