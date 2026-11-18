from __future__ import annotations
import json
from hashlib import sha256

def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def fingerprint(value: object) -> str:
    return sha256(canonical(value).encode()).hexdigest()
