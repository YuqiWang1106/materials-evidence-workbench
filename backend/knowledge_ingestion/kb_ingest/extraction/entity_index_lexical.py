"""Word-match (BM25) scoring over IndexTokenizer terms."""

from __future__ import annotations

import math
import re
from collections.abc import Container, Iterator, Sequence

from kb_ingest.knowledge_index_tokens import IndexTokenizer

from .entity_index_units import Unit

ROUTE_DEPTH = 10
_CJK_CHAR = re.compile(r"[\u3400-\u9fff]")
_CJK_PAIR = re.compile(r"[\u3400-\u9fff]{2}")
K1, B = 1.2, 0.75


class EntityTokenizer(IndexTokenizer):
    """Match complete entity labels without expanding their individual words."""

    def _alias_matches(self, normalized: str) -> list[tuple[int, int, int]]:
        def ascii_word(char: str) -> bool:
            return "a" <= char <= "z" or "0" <= char <= "9"

        matches: list[tuple[int, int, int]] = []
        for start, char in enumerate(normalized):
            node, end = self._alias_labels.step(char), start + 1
            left = start == 0 or not ascii_word(normalized[start - 1])
            while node is not None:
                right = end == len(normalized) or not ascii_word(normalized[end])
                for index, bounded in node.terminals():
                    if not bounded or (left and right):
                        matches.append((start, end, index))
                node = node.step(normalized[end]) if end < len(normalized) else None
                end += 1
        return matches

    def _matching_aliases(self, normalized: str) -> Iterator[str]:
        return (self._alias_tokens[index] for index in sorted({match[2] for match in self._alias_matches(normalized)}))


class Lexical:
    """BM25 over IndexTokenizer terms. The corpus is small (hundreds to a few thousand units), so it lives in memory."""

    def __init__(self, units: Sequence[Unit], groups: Sequence[Sequence[str]] | None) -> None:
        self.tokenizer = EntityTokenizer(alias_groups=groups, alias_version="entity-kb")
        self.docs: list[tuple[Unit, dict[str, int], int, set[str]]] = []
        frequency: dict[str, int] = {}
        for unit in units:
            try:
                terms = self.tokenizer.tokens(unit.index_text[:8000])
            except ValueError:
                terms = []
            counts: dict[str, int] = {}
            for term in terms:
                counts[term] = counts.get(term, 0) + 1
            own = set(counts)
            if unit.own_text:
                try:
                    own = set(self.tokenizer.tokens(unit.own_text[:8000]))
                except ValueError:
                    pass
            self.docs.append((unit, counts, len(terms), own))
            for term in counts:
                frequency[term] = frequency.get(term, 0) + 1
        self.average = (sum(length for _, _, length, _ in self.docs) / len(self.docs)) if self.docs else 0
        self.idf = {
            term: math.log(1 + (len(self.docs) - count + 0.5) / (count + 0.5)) for term, count in frequency.items()
        }

    def rank(
        self,
        query: object,
        allowed: Container[object],
        depth: int = ROUTE_DEPTH,
        minimum: float | None = None,
        own: bool = False,
    ) -> list[object]:
        """Units by BM25. With `minimum`, a unit must also cover that share of the query's weight: the rarity of the
        query words it contains over the rarity of all query words. Single Chinese characters are left out of the
        coverage when the query has character pairs (石 alone would let 石墨烯 cover 硅灰石); words found nowhere in
        the knowledge base are left out too, so the filler of a sentence query (最高的) does not sink every line, and a
        query made only of such words finds nothing. With `own`, an attribute's coverage counts only its own words."""
        try:
            terms = set(self.tokenizer.tokens(query, query=True))
        except ValueError:
            return []
        weighed = terms
        if any(_CJK_PAIR.fullmatch(term) for term in terms):
            weighed = {term for term in terms if not _CJK_CHAR.fullmatch(term)}
        total = sum(self.idf.get(term, 0.0) for term in weighed)
        scored: list[tuple[float, object]] = []
        for unit, counts, length, own_terms in self.docs:
            if unit.key not in allowed:
                continue
            words = own_terms if own else counts
            if minimum is not None and (
                not total or sum(self.idf[term] for term in weighed if term in words) / total < minimum
            ):
                continue
            score = 0.0
            for term in terms:
                count = counts.get(term)
                if count:
                    score += (
                        self.idf[term] * count * (K1 + 1) / (count + K1 * (1 - B + B * length / (self.average or 1)))
                    )
            if score > 0:
                scored.append((score, unit.key))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [key for _, key in scored[:depth]]
