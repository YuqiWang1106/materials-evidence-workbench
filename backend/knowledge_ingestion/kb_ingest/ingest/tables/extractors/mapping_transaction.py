"""Copy mutable semantics while sharing enforced read-only source evidence.

Mapping executors append evidence rows, but never amend a committed excerpt.
Object merges, assertion annotations, header extensions and coverage replacement
remain mutable and require independent copies for rollback. Keeping those copies
is intentional: only evidence.content has the narrower immutable lifecycle.
"""

from copy import deepcopy


def _readonly(*args, **kwargs):
    raise TypeError("Committed source evidence is immutable")


class _EvidenceDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _readonly

    def __deepcopy__(self, memo):
        return self


class _EvidenceList(list):
    __setitem__ = __delitem__ = __iadd__ = __imul__ = append = clear = extend = insert = pop = remove = reverse = (
        sort
    ) = _readonly

    def __deepcopy__(self, memo):
        return self


def _freeze(value):
    """Preserve JSON dict/list behavior and values, forbid nested mutations."""
    if isinstance(value, (_EvidenceDict, _EvidenceList)):
        return value
    if isinstance(value, dict):
        return _EvidenceDict((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return _EvidenceList(_freeze(item) for item in value)
    return value


def snapshot_mapping(package, runs):
    """Freeze each new committed payload once; fork every mutable semantic row.

    Freezing changes neither canonical bytes nor the external input tables. The
    existing package and its snapshot can safely share only these guarded trees;
    an accidental mapping write raises TypeError inside the normal rollback path.
    Newly appended evidence is private until the next mapping commits it here.
    """
    for evidence in package["evidence"]:
        evidence["content"] = _freeze(evidence["content"])
    return deepcopy(package), deepcopy(runs)


def release_evidence(package):
    """Leave the private transaction representation at the public boundary.

    Extraction historically returns ordinary editable JSON containers. Restore
    that contract once after all mappings, rather than once for every profile.
    """

    def thaw(value):
        if isinstance(value, dict):
            return {key: thaw(item) for key, item in value.items()}
        if isinstance(value, list):
            return [thaw(item) for item in value]
        return value

    for evidence in package["evidence"]:
        evidence["content"] = thaw(evidence["content"])
    return package
