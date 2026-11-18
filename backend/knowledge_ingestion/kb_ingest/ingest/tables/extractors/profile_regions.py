"""Bounded, data-only row selection shared by proposal validation and execution."""

from __future__ import annotations

MAX_REGIONS = 128
REGION_LAYOUTS = frozenset({"records", "objects"})


def selected_ranges(profile: dict, nrows: int, path: str = "profile") -> list[tuple[int, int]] | None:
    """Validate a discontinuous selection; None means the legacy contiguous range."""
    if "data_ranges" not in profile:
        return None
    if profile.get("layout") not in REGION_LAYOUTS:
        raise ValueError(f"{path}.data_ranges: supported only for records and objects")
    if "data_start_row" in profile or "data_end_row" in profile:
        raise ValueError(f"{path}.data_ranges: mutually exclusive with data_start_row/data_end_row")
    value = profile["data_ranges"]
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REGIONS:
        raise ValueError(
            f"{path}.data_ranges: expected array of 1..{MAX_REGIONS} row intervals; received {type(value).__name__}"
        )
    result = []
    for index, pair in enumerate(value):
        where = f"{path}.data_ranges[{index}]"
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{where}: expected array [start,end]; received {type(pair).__name__}")
        for item, row in enumerate(pair):
            if type(row) is not int:
                raise ValueError(
                    f"{where}[{item}]: expected one-based integer <= {nrows}; received {type(row).__name__}"
                )
            if not 1 <= row <= nrows:
                raise ValueError(f"{where}[{item}]: expected one-based integer <= {nrows}; received out-of-range int")
        start, end = pair
        if start > end:
            raise ValueError(f"{where}: expected start <= end")
        if result and start <= result[-1][1]:
            reason = "overlap" if start >= result[-1][0] else "increasing order"
            raise ValueError(f"{where}: expected strictly increasing non-overlapping ranges; received {reason}")
        result.append((start, end))
    return result


def expand_profile(profile: dict, nrows: int) -> list[dict]:
    """Create legacy executor inputs without changing any source coordinates."""
    ranges = selected_ranges(profile, nrows)
    if ranges is None:
        return [profile]
    expanded = []
    for start, end in ranges:
        segment = {key: value for key, value in profile.items() if key != "data_ranges"}
        segment["data_start_row"] = start
        segment["data_end_row"] = end
        if len(ranges) > 1:
            segment["_region_scope"] = [start, end]
        expanded.append(segment)
    return expanded
