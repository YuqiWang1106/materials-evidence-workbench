"""Stable request hashes; independent of module and resource locations.

result_hash is the single public digest for model result and cache identities.
The formula is sha256 of JSON with sort_keys, compact separators, ensure_ascii
false, and allow_nan false.
"""

import hashlib
import json
from typing import Any

type Json = dict[str, Any]

__all__ = ["Json", "request_hash", "result_hash"]


def result_hash(result: object) -> str:
    return hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def request_hash(identity: Json) -> str:
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
