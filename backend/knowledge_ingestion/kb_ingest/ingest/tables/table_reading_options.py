"""Reader option validation and candidate selection."""

from __future__ import annotations

import codecs
import csv
from copy import deepcopy
from pathlib import Path

from kb_ingest.core import fingerprint

from .readers.tables import read_tables

LIMITS = {"max_bytes": 50_000_000, "max_cells": 1_000_000, "max_uncompressed_bytes": 128_000_000}


def validate_reader_options(options=None):
    """Reject executable hooks, coercing CSV modes and relaxed safety limits."""
    from .table_recipe import keys

    options = {} if options is None else deepcopy(options)
    dialect_keys = {
        "delimiter",
        "quotechar",
        "escapechar",
        "doublequote",
        "skipinitialspace",
        "quoting",
        "strict",
        "lineterminator",
    }
    keys(options, {"encoding", "dialect", "formula_mode", *LIMITS, *dialect_keys}, path="reader_options")
    from .readers.tables import FORMULA_MODES

    if options.get("formula_mode", "preserve") not in FORMULA_MODES:
        raise ValueError("reader_options.formula_mode must be preserve or stored_value")
    for name, ceiling in LIMITS.items():
        if name in options and (type(options[name]) is not int or not 1 <= options[name] <= ceiling):
            raise ValueError(f"reader_options.{name} must be a positive integer <= {ceiling}")
    if "encoding" in options:
        if not isinstance(options["encoding"], str):
            raise ValueError("reader_options.encoding must name a text encoding")
        try:
            codecs.lookup(options["encoding"])
        except LookupError:
            raise ValueError("reader_options.encoding is unknown") from None
    dialect = options.get("dialect", {})
    if isinstance(dialect, str):
        try:
            csv.get_dialect(dialect)
        except csv.Error:
            raise ValueError("reader_options.dialect is unknown") from None
        dialect = {}
    keys(dialect, dialect_keys, path="reader_options.dialect")
    dialect = {**dialect, **{key: options[key] for key in dialect_keys if key in options}}
    for name in ("delimiter", "quotechar", "escapechar"):
        if name in dialect and not (name != "delimiter" and dialect[name] is None):
            if not isinstance(dialect[name], str) or len(dialect[name]) != 1 or dialect[name] in "\r\n\x00":
                raise ValueError(f"reader_options.{name} must be one delimiter character")
    for name in ("doublequote", "skipinitialspace", "strict"):
        if name in dialect and type(dialect[name]) is not bool:
            raise ValueError(f"reader_options.{name} must be boolean")
    if dialect.get("strict", True) is not True:
        raise ValueError("reader_options requires strict CSV reading")
    if "quoting" in dialect and (
        type(dialect["quoting"]) is not int
        or dialect["quoting"] not in (csv.QUOTE_MINIMAL, csv.QUOTE_ALL, csv.QUOTE_NONE)
    ):
        raise ValueError("reader_options.quoting must preserve source strings")
    if "lineterminator" in dialect and (
        not isinstance(dialect["lineterminator"], str) or len(dialect["lineterminator"]) > 2
    ):
        raise ValueError("reader_options.lineterminator is invalid")
    return options


def reader_candidates(path, options):
    options = validate_reader_options(options)
    path = Path(path)
    raw = None
    suspicious_nul = False
    if path.suffix.lower() == ".csv":
        if path.stat().st_size > options.get("max_bytes", 50_000_000):
            raise ValueError("Source exceeds reader safety boundary")
        raw = path.read_bytes()
        suspicious_nul = bool(raw and raw.count(b"\x00") / len(raw) > 0.1)
    try:
        first = read_tables(path, options=options)
        if all(t.get("extraction_safe") for t in first) and not suspicious_nul:
            return [{"candidate_id": "candidate-0", "options": options, "tables": first}]
    except ValueError:
        if path.suffix.lower() != ".csv":
            raise
        first = None
    if path.suffix.lower() != ".csv" or path.stat().st_size > options.get("max_bytes", 50_000_000):
        raise ValueError("Source exceeds reader safety boundary")
    if raw is None:
        raw = path.read_bytes()
    if options.get("encoding"):
        encodings = [options["encoding"]]
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings = ["utf-16"]
    elif raw.startswith(b"\xef\xbb\xbf") or (first and not suspicious_nul):
        encodings = ["utf-8-sig"]
    else:
        encodings = ["cp1252", "gb18030", "latin1"]
        if raw and raw.count(b"\x00") / len(raw) > 0.1:
            encodings = ["utf-16-le", "utf-16-be"] + encodings
    candidates, signatures = [], set()
    for encoding in encodings:
        try:
            decoded = raw.decode(encoding, errors="strict")
            if "\x00" in decoded:
                continue
            reencoded = decoded.encode(encoding, errors="strict")
            if encoding == "utf-8-sig" and not raw.startswith(b"\xef\xbb\xbf"):
                reencoded = decoded.encode("utf-8")
            elif encoding == "utf-16":
                endian = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be"
                reencoded = raw[:2] + decoded.encode(endian)
            if reencoded != raw:
                continue
            configured = {**options, "encoding": encoding}
            tables = read_tables(path, options=configured)
        except (UnicodeError, ValueError, LookupError):
            continue
        choices = [(configured, tables)]
        if not all(t.get("extraction_safe") for t in tables):
            choices = []
            for diagnostic in tables[0].get("parse_diagnostics", {}).get("candidate_dialects", []):
                if diagnostic.get("status") != "candidate":
                    continue
                explicit = {**configured, "dialect": diagnostic["dialect"]}
                try:
                    choices.append((explicit, read_tables(path, options=explicit)))
                except ValueError:
                    continue
        for configured, tables in choices:
            signature = fingerprint([{"rows": t["rows"], "sheet": t["sheet"]} for t in tables])
            if signature in signatures:
                continue
            signatures.add(signature)
            candidates.append({"candidate_id": f"candidate-{len(candidates)}", "options": configured, "tables": tables})
            if len(candidates) > 8:
                raise ValueError("More than eight distinct lossless reader interpretations; source left unresolved")
    if not candidates:
        raise ValueError("No bounded lossless source decoding/dialect candidate")
    return candidates
