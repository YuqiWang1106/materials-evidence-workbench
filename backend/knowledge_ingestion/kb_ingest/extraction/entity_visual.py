"""Select entity visual reads without changing the source IR, blocks, or locators.

Only a narrow, syntactically complete plain MinerU equation can avoid duplicate
transcription. This is an OCR sufficiency decision, never scientific validation.
Figures, charts, unknown/complex formula syntax and concrete reading risks keep
their original visual read. The shared document reader retains its own defaults.
"""

from __future__ import annotations

import ast

VISUAL_KINDS = {"image", "chart", "figure", "equation", "formula", "unread_page"}
FORMULA_PARTS = {"equation", "formula", "inline_equation", "display_equation", "interline_equation"}
PLACEHOLDERS = {"unknown", "unreadable", "unparsed", "unclear", "missing", "placeholder", "nan", "none", "null", "todo"}


def _plain_equation(text):
    """Recognize a small arithmetic grammar; never execute or simplify a formula."""
    text = str(text or "").strip()
    for delimiter in ("$$", "$"):
        if text.startswith(delimiter) and text.endswith(delimiter) and len(text) > 2 * len(delimiter):
            text = text[len(delimiter) : -len(delimiter)].strip()
            break
    if (
        len(text) > 256
        or text.count("=") != 1
        or not any(c.isalpha() for c in text)
        or any(not (c.isalnum() or c.isspace() or c in "_+*/^().=-") for c in text)
    ):
        return False
    allowed = (
        ast.Expression,
        ast.Name,
        ast.Load,
        ast.Constant,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.UAdd,
        ast.USub,
    )
    for side in text.split("="):
        try:
            expression = ast.parse(side.strip().replace("^", "**"), mode="eval")
        except (SyntaxError, ValueError, RecursionError):
            return False
        for node in ast.walk(expression):
            if not isinstance(node, allowed):
                return False
            if isinstance(node, ast.Name) and node.id.casefold() in PLACEHOLDERS:
                return False
            if isinstance(node, ast.Constant) and type(node.value) not in (int, float):
                return False
    return True


def _specific_risk(row):
    if any(
        row.get(key)
        for key in (
            "requires_image_verification",
            "requires_verification",
            "diagnostics",
            "uncertainties",
            "uncertainty",
            "error",
            "errors",
            "failure",
            "failures",
            "conflicts",
            "reading_disagreements",
        )
    ):
        return True
    if row.get("eligible_for_extraction") is False:
        return True
    if row.get("verification_state") not in (None, "", "verified", "human_verified"):
        return True
    # MinerU attaches this generic reminder to every formula. Preserve it in the
    # original evidence, but do not confuse it with an actual parsing diagnostic.
    return any(risk != "formula_requires_source_review" for risk in row.get("reading_risks") or [])


def _formula_body(region):
    parts = region.get("source_parts") or []
    if not parts:
        if any(region.get(key) for key in ("caption", "captions", "footnote", "footnotes")):
            return False
        return _plain_equation(region.get("text"))
    if not isinstance(parts, list) or any(not isinstance(part, dict) for part in parts):
        return False
    annotations = [part for part in parts if str(part.get("type", "")).endswith(("_caption", "_footnote"))]
    bodies = []
    for part in parts:
        if part.get("type") not in FORMULA_PARTS:
            continue
        path = part.get("content_path")
        if not isinstance(path, list):
            return False
        # Nested inline math in a caption/footnote is still annotation text.
        if any(
            isinstance(note.get("content_path"), list) and path[: len(note["content_path"])] == note["content_path"]
            for note in annotations
        ):
            continue
        if _specific_risk(part):
            return False
        bodies.append(part)
    text = " ".join(str(region.get("text") or "").split())
    return bool(bodies) and all(
        _plain_equation(part.get("text")) and " ".join(str(part.get("text") or "").split()) in text for part in bodies
    )


def select_visual_blocks(blocks, documents=None):
    """Return the same candidate block objects plus a serializable decision audit.

    The raw region lookup is keyed by asset and IR ID, then checked against page
    and reader identity. Missing/ambiguous raw evidence fails closed to visual
    reading, including compatibility callers that do not supply documents.
    """
    regions = {}
    for document in documents or []:
        for region in document.get("regions", []):
            key = (document.get("asset_ref"), region.get("id"))
            regions.setdefault(key, []).append(region)
    selected, skipped, decisions = [], [], []
    for block in blocks:
        kind = block.get("kind")
        if kind not in VISUAL_KINDS:
            continue
        reason = "unread_page" if kind == "unread_page" else "figure_requires_visual_reading"
        skip = False
        if kind in ("equation", "formula"):
            matches = regions.get((block.get("asset_ref"), block.get("ir_region")), [])
            region = matches[0] if len(matches) == 1 else None
            if (
                block.get("source") != "mineru"
                or region is None
                or region.get("source") != "mineru"
                or region.get("kind") != kind
                or region.get("page") != block.get("page")
            ):
                reason = "formula_source_unresolved"
            elif _specific_risk(block) or _specific_risk(region):
                reason = "formula_has_reading_risk"
            elif " ".join(str(block.get("text") or "").split()) != " ".join(str(region.get("text") or "").split()):
                reason = "formula_text_not_reusable"
            elif _formula_body(region):
                reason, skip = "usable_mineru_formula", True
            else:
                reason = "formula_body_incomplete_or_unsupported"
        if skip:
            skipped.append(block["block_id"])
        else:
            selected.append(block)
        decisions.append(
            {
                "block_id": block["block_id"],
                "asset_ref": block.get("asset_ref"),
                "page": block.get("page"),
                "ir_region": block.get("ir_region"),
                "selected": not skip,
                "reason": reason,
            }
        )
    return selected, {
        "version": "entity-visual-selection-v1",
        "selected_block_ids": [block["block_id"] for block in selected],
        "skipped_block_ids": skipped,
        "decisions": decisions,
    }
