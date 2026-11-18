"""A fact value that takes only part of a table cell is left out on its own.

The table mapping compiler reads a cell as a whole and separates the number from its unit itself. A plan that
quotes or spans a part of a cell used to fail the whole table; now only that fact is dropped and reported as
`cell_value_cut`, its cell stays unmapped, and the rest of the table compiles.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Protocol

CELL_VALUE_CUT = "cell_value_cut"
CELL_VALUE_CUT_REASON = (
    "cell facts must preserve the entire original cell value: use kind=cell or axis with span=null "
    "for the whole cell (the program separates the number from its unit); never quote part of a cell"
)


class Bound(Protocol):
    """A resolved selector: the source block and the text selected in it."""

    @property
    def block(self) -> Mapping[str, object]: ...

    @property
    def value(self) -> str: ...


def cut_fact_indexes(
    values: Sequence[Mapping[str, object]],
    resolve: Callable[[Mapping[str, object]], Bound],
    missing: type[Exception],
) -> set[int]:
    """Indexes of the fact value selectors that take only part of a table cell.

    Args:
        values: The value selector of every fact template, in plan order.
        resolve: Resolves a selector outside any record.
        missing: What `resolve` raises when the selected content is absent; such a quote is not a cut.
    """
    return {index for index, value in enumerate(values) if _cuts_cell(value, resolve, missing)}


def _cuts_cell(
    value: Mapping[str, object], resolve: Callable[[Mapping[str, object]], Bound], missing: type[Exception]
) -> bool:
    if value["kind"] in ("cell", "axis"):
        return value["span"] is not None
    if value["kind"] != "quote":
        return False
    try:
        bound = resolve(value)
    except missing:
        return False
    return bound.block["kind"] == "table_cell" and bound.value != bound.block["text"]


def cut_count(diagnostics: Iterable[Mapping[str, object]]) -> int:
    """How many fact values a compiled table left out for cutting a cell."""
    return sum(1 for item in diagnostics if item["code"] == CELL_VALUE_CUT)
