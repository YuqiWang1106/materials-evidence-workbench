"""Versioned lexical recall only: aliases imply neither field equality nor unit conversion."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Sequence

from .core import fingerprint
from .knowledge_domain import load_domain_profile

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]+")
_WORD = re.compile(r"[a-z]+[a-z0-9]*(?:[-_][a-z0-9]+)*|[+-]?(?:\d+(?:\.\d+)?|\.\d+)|[%°µμ]+")
_ALIAS_WORD = re.compile(r"(?<![a-z0-9])[a-z]{2,}[a-z0-9]*(?![a-z0-9])")


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower()


def _base_terms(value: str) -> list[str]:
    terms = _WORD.findall(value)
    for run in _CJK.findall(value):
        terms.extend(run)
        terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    return terms


class _AliasTrie:
    """Character keys point at child nodes; terminals hold (alias index, ascii-boundary) marks.

    The class writes only these two stores, so their types can be declared exactly.
    """

    def __init__(self) -> None:
        self._children: dict[str, _AliasTrie] = {}
        self._terminals: list[tuple[int, bool]] = []

    def descend(self, char: str) -> _AliasTrie:
        found = self._children.get(char)
        if found is not None:
            return found
        created = _AliasTrie()
        self._children[char] = created
        return created

    def step(self, char: str) -> _AliasTrie | None:
        return self._children.get(char)

    def terminals(self) -> Sequence[tuple[int, bool]]:
        return self._terminals

    def add_terminal(self, index: int, bounded: bool) -> None:
        self._terminals.append((index, bounded))


class IndexTokenizer:
    version = "cjk-bigram-english-decimal-units/v1"

    def __init__(
        self, *, alias_groups: Sequence[Sequence[str]] | None = None, alias_version: str | None = None
    ) -> None:
        groups = alias_groups
        version = alias_version
        if groups is None:
            profile = load_domain_profile()
            grouped: dict[str, list[str]] = {}
            for mapping in profile.mappings:
                q = mapping.selector.qualifiers
                # This grouping adds recall synonyms only; exact query remains unchanged.
                identity = q.get("metric_id") or q.get("parameter_id") or fingerprint(mapping.selector.model_dump())
                grouped.setdefault(identity, []).append(mapping.source_label)
            groups = list(grouped.values())
            version = profile.id
        self.alias_groups = tuple(tuple(sorted({_normalize(label) for label in group})) for group in groups)
        if len(self.alias_groups) > 1000 or any(
            len(g) > 100 or any(not label or len(label) > 128 for label in g) for g in self.alias_groups
        ):
            raise ValueError("Alias configuration limit exceeded")
        self.alias_version = (version or "custom") + ":" + fingerprint(self.alias_groups)
        # Match literal labels through a shared prefix tree rather than scanning every
        # document once per alias. Keep the original boundary rules and token identities.
        self._alias_labels = _AliasTrie()
        self._alias_words: dict[str, set[int]] = {}
        self._alias_tokens = tuple("alias-" + fingerprint(group) for group in self.alias_groups)
        for index, group in enumerate(self.alias_groups):
            for label in group:
                node = self._alias_labels
                for char in label:
                    node = node.descend(char)
                node.add_terminal(index, not bool(_CJK.search(label)))
                for word in re.findall(r"[a-z]{2,}[a-z0-9]*", label):
                    self._alias_words.setdefault(word, set()).add(index)

    def _matching_aliases(self, normalized: str) -> Iterator[str]:
        matched: set[int] = set()
        for word in _ALIAS_WORD.findall(normalized):
            matched.update(self._alias_words.get(word, ()))

        def ascii_word(char: str) -> bool:
            return "a" <= char <= "z" or "0" <= char <= "9"

        for start, char in enumerate(normalized):
            node, end = self._alias_labels.step(char), start + 1
            left_boundary = start == 0 or not ascii_word(normalized[start - 1])
            while node is not None:
                right_boundary = end == len(normalized) or not ascii_word(normalized[end])
                for index, bounded in node.terminals():
                    if not bounded or (left_boundary and right_boundary):
                        matched.add(index)
                node = node.step(normalized[end]) if end < len(normalized) else None
                end += 1
        return (self._alias_tokens[index] for index in sorted(matched))

    def tokens(self, value: object, *, query: bool = False) -> list[str]:
        if not isinstance(value, str) or len(value.encode("utf-8")) > (4096 if query else 32768):
            raise ValueError("Lexical text size limit exceeded")
        normalized = _normalize(value)
        terms = _base_terms(normalized)
        if any(len(term) > 128 for term in terms):
            raise ValueError("Lexical term length limit exceeded")
        terms.extend(self._matching_aliases(normalized))
        terms = list(dict.fromkeys(terms))
        if len(terms) > (128 if query else 16384):
            raise ValueError("Lexical term count limit exceeded")
        return terms

    @staticmethod
    def _atom(term: str) -> str:
        # ASCII hex avoids FTS's implicit splitting of decimals, units and CJK.
        return "t" + term.encode("utf-8").hex()

    def document_tokens(self, value: object) -> str:
        return " ".join(self._atom(term) for term in self.tokens(value))

    def match_expression(self, value: object) -> str:
        return " OR ".join('"' + self._atom(term) + '"' for term in self.tokens(value, query=True))
