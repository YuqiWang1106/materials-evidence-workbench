"""A line written twice becomes one attribute with several cites.

Two lines of one entity and section are one reading only when they say the same words (width, case, spacing
and a closing full stop aside). Lines that share a value or most of their wording are not assumed to be
the same fact; a table line and its restatement in the prose remain separate.
"""

from collections.abc import Sequence
from typing import NotRequired, TypedDict

import kb_ingest.core.contract_entity as entity_keys
from kb_ingest.core.conservation import require_conserved


class FoldEntity(TypedDict):
    name: str
    aliases: NotRequired[list[str]]
    local_id: NotRequired[str]
    type: NotRequired[str]


class FoldAttribute(TypedDict):
    local_id: str
    section: str
    text: str
    refs: NotRequired[list[str]]
    citations: NotRequired[list[dict[str, object]]]
    from_table: NotRequired[bool]
    block_id: NotRequired[str]
    quote: NotRequired[str]


class FoldGraph(TypedDict):
    entities: Sequence[FoldEntity]
    attributes: Sequence[FoldAttribute]
    related: NotRequired[object]


def fold_graph(graph: FoldGraph) -> FoldGraph:
    """Within one paper's graph: a repeated line of an entity becomes one, the table line kept first."""
    rows = graph["attributes"]
    order = sorted(range(len(rows)), key=lambda index: (not rows[index].get("from_table"), index))
    kept: list[tuple[int, FoldAttribute]] = []
    first: dict[tuple[str, str, str], FoldAttribute] = {}
    repeated = 0
    for index in order:
        row = rows[index]
        key = (row["local_id"], row["section"], entity_keys.reading_key(row["text"]))
        match = first.get(key)
        if match is None:
            row = {**row, "citations": list(row.get("citations") or [])}
            first[key] = row
            kept.append((index, row))
            continue
        match["citations"] += [entity_keys.cite(row), *(row.get("citations") or [])]
        match["refs"] = list(dict.fromkeys([*match.get("refs", []), *row.get("refs", [])]))
        repeated += 1
    require_conserved("fold attribute", entered=len(rows), kept=len(kept), set_aside=repeated)
    # Kept lines stay in their original order.
    return {**graph, "attributes": [row for _, row in sorted(kept, key=lambda item: item[0])]}
