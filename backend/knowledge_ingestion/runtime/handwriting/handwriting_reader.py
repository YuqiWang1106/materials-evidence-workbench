"""Local OCR of page images: scanned pages, photos and handwriting; runs in the separate MinerU reader environment.

Usage: python -I handwriting_reader.py --pages PAGES.json --model-root MODELS --output OUT.json

PAGES.json is a list of {"page": int, "image": absolute path, "regions": [...] (optional)}. MODELS is the MinerU
model directory that contains MinerU-4_models_onnx (the pinned PP-OCRv6 detector and recognizer). The website
never imports this file: it only needs OpenCV, NumPy and MinerU's ONNX OCR, which live in the reader environment.

Two ways to read a page:
  - regions given ({"id", "kind": "table" | "text" | "probe", "bbox": pixels}, the layout of a document parser):
    the page is upright as the parser saw it; each table region is read as a table, each text region line by
    line, and a "probe" region (a large picture on a scanned page) becomes a table only when it holds a ruled
    grid. Every result carries its region id.
  - no regions (a photo or a page nothing else could read): choose the orientation (0/90/180/270) whose OCR
    gives the most confident characters, straighten small skew from the long horizontal rules, find the ruled
    tables and read them, and read the text outside them line by line.

Tables, of any common style: the rules are found on a median-filtered copy (a halftone background is lost,
rules stay) as runs that cover most of the table's width or height; a partial rule counts only where it is
solid from one rule to another. A fully ruled table is the grid of its rule positions, with a merged cell where
the rule between two places is missing. A table without column rules (three-line tables, boxed tables, lists)
takes its rows from the text lines and its columns from the x gaps that (almost) no row's ink crosses. Text is
read at a set line height (TEXT_HEIGHT), which the recognizer is trained for and where halftone dots blend into
strokes. A line crossing a column boundary is read again piece by piece only where its ink has a clear gap
(`100 100` is two cells, a sentence is not split). A lone short flat stroke is a dash.

Every box is mapped back to the input image's own pixel coordinates; `upright_bbox` keeps the box in the turned,
straightened working image too (with the page's `rotation` and the applied `deskew_degrees`), so the second
reader can crop exactly the same area. Nothing is guessed: an unreadable cell keeps its location with an empty
reading.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

import cv2
import numpy as np

VERSION = "local-ocr-reader-v2"
ROTATIONS = {0: None, 90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
MIN_RULES = 3  # fewer long horizontal rules: a page without regions is read as text only
TEXT_HEIGHT = 36.0  # pixels per text line when reading (measured best on 12 scanned patent tables)
PROBE_MIN_CELLS = 4  # a picture region is a table only with a ruled grid of at least this many cells
DASH = re.compile(r"[-‐‑–—−一]{1,4}")
CJK = re.compile(r"[　-鿿＀-￯]")
CONTENT = re.compile(r"[0-9A-Za-z一-鿿★☆]")


def load_ocr(model_root):
    from mineru.model.ocr.pp_ocr_v6_onnx import PPOCRv6ONNX

    base = Path(model_root) / "MinerU-4_models_onnx" / "OCR" / "paddleocr"
    return PPOCRv6ONNX(
        det_model_path=str(base / "ch_PP-OCRv6_tiny_det_infer.onnx"),
        rec_model_path=str(base / "ch_PP-OCRv6_small_rec_infer.onnx"),
        drop_score=0.0,
    )


def detect_lines(ocr, image):
    boxes, rec = ocr(image)
    if boxes is None:
        return []
    return [(np.asarray(box, dtype=float), text, float(score)) for box, (text, score) in zip(boxes, rec or [])]


def orientation(ocr, image):
    small = cv2.resize(image, None, fx=0.5, fy=0.5) if max(image.shape[:2]) > 1200 else image
    scores = {}
    for angle, code in ROTATIONS.items():
        rotated = small if code is None else cv2.rotate(small, code)
        scores[angle] = sum(score * len(text.strip()) for _, text, score in detect_lines(ocr, rotated))
    return max(scores, key=lambda angle: (scores[angle], -angle)), scores


def join(parts):
    """Lines of one cell or paragraph as one text: no space between Chinese characters, one space otherwise."""
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if text and not (CJK.search(text[-1]) and CJK.search(part[0])):
            text += " "
        text += part
    return text


# ---- rules ------------------------------------------------------------------------------------------------


def rule_masks(image):
    """(ink, horizontal rules, vertical rules), all from a median-filtered copy: halftone dots are lost, rules
    and strokes stay. Dots of dotted rules are joined along the rule direction only."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    ink = cv2.adaptiveThreshold(cv2.medianBlur(gray, 3), 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 35, 15)
    h, w = ink.shape
    hor = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 1)))
    hor = cv2.morphologyEx(hor, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, w // 12), 1)))
    ver = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 9)))
    ver = cv2.morphologyEx(ver, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, h // 12))))
    return ink, hor, ver


def long_rules(hor, width):
    count, _, stats, _ = cv2.connectedComponentsWithStats(hor, connectivity=8)
    return [stats[i] for i in range(1, count) if stats[i][2] >= width * 0.3]


def skew_angle(hor, width):
    angles = []
    for x, y, cw, ch, _ in long_rules(hor, width):
        ys, xs = np.where(hor[y : y + ch, x : x + cw] > 0)
        if len(xs) > 50:
            angles.append(math.degrees(math.atan(np.polyfit(xs, ys, 1)[0])))
    return float(np.median(angles)) if angles else 0.0


def runs(profile, threshold):
    found, start = [], None
    for i, value in enumerate(list(profile) + [0]):
        if value >= threshold and start is None:
            start = i
        elif value < threshold and start is not None:
            found.append((start, i))
            start = None
    return found


def centres(spans, min_gap):
    """One position per rule; runs closer than min_gap (a double rule) are one rule."""
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] < min_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a + b - 1) / 2 for a, b in merged]


def rule_lines(hor, ver, height):
    """(row rule ys, column rule xs). A full rule covers most of the table's width or height. A partial rule
    (under a spanning header, beside a merged cell) is kept only where it is solid from one rule of the other
    direction to another (column rules across at least two rows); marks of a background or bold text are not."""
    gap = max(6, int(height * 0.5))
    hd = cv2.dilate(hor, np.ones((7, 1), np.uint8)) > 0
    vd = cv2.dilate(ver, np.ones((1, 7), np.uint8)) > 0
    hprof = hd.mean(axis=1)
    strong_y = runs(hprof, 0.75)
    overlaps = lambda span, strong: any(a < span[1] and b > span[0] for a, b in strong)
    weak_y = [r for r in runs(hprof, 0.25) if not overlaps(r, strong_y) and r[1] - r[0] <= max(10, 0.4 * height)]
    ys = centres(strong_y, gap)
    if len(ys) < 2:
        return sorted(ys + centres(weak_y, gap)), []
    vprof = vd[int(ys[0]) : int(ys[-1]) + 1].mean(axis=0)
    strong_x = runs(vprof, 0.75)
    xs = centres(strong_x, gap)

    def solid(values, share, steps=1):
        return any(
            share(values[i], values[j]) >= 0.95 for i in range(len(values)) for j in range(i + steps, len(values))
        )

    for a, b in runs(vprof, 0.2):
        if overlaps((a, b), strong_x) or b - a > max(10, 0.4 * height):
            continue
        x = (a + b - 1) // 2
        if solid(
            ys, lambda y0, y1: (ver[int(y0) : int(y1) + 1, max(0, x - 2) : x + 3] > 0).any(axis=1).mean(), steps=2
        ):
            xs.append(x)
    xs = sorted(xs)
    if len(xs) >= 2:
        # Row rules outside the column rules' reach (a page rule above the caption) are not the table's.
        reach = np.where(vd[:, [int(x) for x in xs]].any(axis=1))[0]
        ys = [y for y in ys if reach[0] - gap <= y <= reach[-1] + gap]
        for a, b in weak_y:
            y = (a + b - 1) // 2
            if solid(xs, lambda x0, x1: (hor[max(0, y - 2) : y + 3, int(x0) : int(x1) + 1] > 0).any(axis=0).mean()):
                ys.append(y)
    return sorted(ys), xs


# ---- reading --------------------------------------------------------------------------------------------


class Reader:
    """Text and tables of one image. `scale` reduces the image for reading text (TEXT_HEIGHT per line);
    structure is always measured on the full image. Boxes are in the full image's pixels."""

    def __init__(self, ocr):
        self.ocr = ocr

    def set_image(self, image, scale=None):
        self.image = image
        self.ink, self.hor, self.ver = rule_masks(image)
        self.scale = self.measure(image) if scale is None else scale
        self.small = (
            image
            if self.scale == 1.0
            else cv2.resize(
                cv2.GaussianBlur(image, (0, 0), 0.6 / self.scale),
                None,
                fx=self.scale,
                fy=self.scale,
                interpolation=cv2.INTER_AREA,
            )
        )

    def measure(self, image):
        """The reading scale: the median detected line height brought to TEXT_HEIGHT, never enlarged."""
        probe = cv2.resize(image, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        found = [box for box, text, _ in detect_lines(self.ocr, probe) if text.strip()]
        if not found:
            return 1.0
        measured = 2 * float(np.median([box[:, 1].max() - box[:, 1].min() for box in found]))
        scale = min(1.0, max(0.25, TEXT_HEIGHT / measured))
        return 1.0 if scale > 0.85 else scale

    def rec(self, box):
        x0, y0, x1, y1 = (int(round(v * self.scale)) for v in box)
        crop = self.small[max(0, y0) : y1, max(0, x0) : x1]
        if crop.size == 0 or crop.shape[0] < 6 or crop.shape[1] < 6:
            return "", 0.0
        ((text, score),) = self.ocr.text_recognizer([crop])[0]
        return text.strip(), float(score)

    def lines(self, area=None, clean=True):
        """Detected lines inside `area` (full-image pixels; the whole image when None); each {'box', 'text',
        'score'} in full-image pixels. `clean` (tables) removes specks and writes dashes as '-'; prose keeps every
        line with text, since a short line there ('一种', '、168') is content."""
        x0, y0 = (0, 0) if area is None else (max(0, int(area[0])), max(0, int(area[1])))
        x1, y1 = (self.image.shape[1], self.image.shape[0]) if area is None else (int(area[2]), int(area[3]))
        s = self.scale
        crop = self.small[int(y0 * s) : int(math.ceil(y1 * s)), int(x0 * s) : int(math.ceil(x1 * s))]
        if crop.size == 0:
            return []
        found = []
        for box, text, score in detect_lines(self.ocr, crop):
            if text.strip():
                found.append(
                    {
                        "box": [
                            x0 + box[:, 0].min() / s,
                            y0 + box[:, 1].min() / s,
                            x0 + box[:, 0].max() / s,
                            y0 + box[:, 1].max() / s,
                        ],
                        "text": text.strip(),
                        "score": score,
                    }
                )
        if not found or not clean:
            return found
        height = float(np.median([l["box"][3] - l["box"][1] for l in found]))
        # The line height of everything detected, specks included: what the cell rules below were measured with.
        self.line_height = height
        # Specks of a halftone background read as short low-score text; they are not content.
        found = [
            l
            for l in found
            if (l["box"][3] - l["box"][1] >= 0.5 * height or DASH.fullmatch(l["text"]))
            and not (len(l["text"]) <= 2 and l["score"] < 0.8 and not DASH.fullmatch(l["text"]))
        ]
        for l in found:
            if DASH.fullmatch(l["text"]):
                l["text"] = "-" * len(l["text"])
            elif len(l["text"]) <= 2 and not CJK.search(l["text"]) and self.flat_mark(l["box"], height):
                l["text"] = "-"  # a dash read as a one: its only mark is short and flat, a printed 1 is tall
        return [l for l in found if CONTENT.search(l["text"]) or set(l["text"]) == {"-"}]

    def flat_mark(self, box, height):
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        inner = self.ink[max(0, y0) : y1, max(0, x0) : x1]
        if inner.size == 0:
            return False
        _, _, stats, _ = cv2.connectedComponentsWithStats(inner, connectivity=8)
        big = [st for st in stats[1:] if st[4] >= 4]
        if not big:
            return False
        main = max(big, key=lambda st: st[4])
        return main[3] <= 0.3 * height and main[2] >= 2 * main[3]

    # ---- tables -------------------------------------------------------------------------------------------
    def table(self, area):
        """{'style', 'cells': [{row, col, row_span, col_span, text, score, empty, box}]} for the table filling
        `area`; style is 'ruled', 'three_line', 'unruled' or 'empty'."""
        x0, y0, x1, y1 = (int(round(v)) for v in area)
        lines = self.lines(area)
        if not lines:
            return {"style": "empty", "cells": []}
        height = self.line_height
        hor, ver = self.hor[y0:y1, x0:x1], self.ver[y0:y1, x0:x1]
        ys, xs = rule_lines(hor, ver, height)
        ys, xs = [y + y0 for y in ys], [x + x0 for x in xs]
        if len(ys) >= 2:
            # The table body lies between the outer row rules; a caption above or prose below is not part of it.
            body = [
                l for l in lines if ys[0] - height * 0.3 <= (l["box"][1] + l["box"][3]) / 2 <= ys[-1] + height * 0.3
            ]
            if len(body) >= 0.5 * len(lines):
                lines = body
        cells = self.ruled(lines, height, ys, xs) if len(xs) >= 3 and len(ys) >= 3 else None
        style = "ruled" if cells is not None else "three_line" if ys else "unruled"
        if cells is None:
            cells = self.by_text(lines, height, ys, xs)
        for cell in cells:
            cell["empty"] = not cell["text"]
        return {"style": style, "cells": cells}

    def ruled(self, lines, height, ys, xs):
        """The grid of the rule positions; two neighbouring places are one merged cell when the rule between
        them is (mostly) missing. Fragmented cell interiors (dithered scans) do not matter."""
        tol = max(8, height * 0.5)
        rows, cols = len(ys) - 1, len(xs) - 1

        def ruled_share(mask, fixed, a, b, axis):
            band, f, a, b = int(max(3, tol / 2)), int(fixed), int(a), int(b)
            strip = (
                mask[max(0, f - band) : f + band + 1, a:b] if axis == 0 else mask[a:b, max(0, f - band) : f + band + 1]
            )
            return float((strip.max(axis=0 if axis == 0 else 1) > 0).mean()) if strip.size else 0.0

        parent = list(range(rows * cols))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for r in range(rows):
            for c in range(cols):
                my = min(tol, (ys[r + 1] - ys[r]) / 4)  # stay clear of the crossing rules, inside short rows too
                if c + 1 < cols and ruled_share(self.ver, xs[c + 1], ys[r] + my, ys[r + 1] - my, 1) < 0.5:
                    parent[find(r * cols + c + 1)] = find(r * cols + c)
                mx = min(tol, (xs[c + 1] - xs[c]) / 4)
                if r + 1 < rows and ruled_share(self.hor, ys[r + 1], xs[c] + mx, xs[c + 1] - mx, 0) < 0.5:
                    parent[find((r + 1) * cols + c)] = find(r * cols + c)
        groups = {}
        for i in range(rows * cols):
            groups.setdefault(find(i), []).append(divmod(i, cols))
        cells = []
        for places in groups.values():
            r0, r1 = min(p[0] for p in places), max(p[0] for p in places)
            c0, c1 = min(p[1] for p in places), max(p[1] for p in places)
            if len(places) != (r1 - r0 + 1) * (c1 - c0 + 1):  # not a rectangle: keep the places apart
                cells.extend(
                    {"row": r, "col": c, "row_span": 1, "col_span": 1, "box": [xs[c], ys[r], xs[c + 1], ys[r + 1]]}
                    for r, c in places
                )
                continue
            cells.append(
                {
                    "row": r0,
                    "col": c0,
                    "row_span": r1 - r0 + 1,
                    "col_span": c1 - c0 + 1,
                    "box": [xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1]],
                }
            )
        cells.sort(key=lambda cell: (cell["row"], cell["col"]))
        self.fill(cells, lines, height)
        return cells

    def by_text(self, lines, height, ys, xs):
        """Rows from lines that overlap vertically (a rule between them separates); columns from the x gaps that
        (almost) no row's ink crosses, and the column rules. Ink, not line boxes: the detector often joins a
        whole row (`46 45 46 47`) into one box."""
        bands = []
        for l in sorted(lines, key=lambda l: l["box"][1]):
            top, bottom = l["box"][1] + 0.2 * height, l["box"][3] - 0.2 * height
            if (
                bands
                and top <= bands[-1]["y1"]
                and not any(
                    bands[-1]["y1"] - 0.2 * height < y < top + 0.2 * height and bands[-1]["y0"] < y < bottom for y in ys
                )
            ):
                bands[-1]["y1"] = max(bands[-1]["y1"], bottom)
                bands[-1]["lines"].append(l)
            else:
                bands.append({"y0": top, "y1": bottom, "lines": [l]})
        x0, x1 = int(min(l["box"][0] for l in lines)), int(max(l["box"][2] for l in lines)) + 1
        ink = cv2.bitwise_and(
            self.ink, cv2.bitwise_not(cv2.dilate(cv2.bitwise_or(self.hor, self.ver), np.ones((5, 5), np.uint8)))
        )
        close = max(1, int(height * 0.25))
        cover = np.zeros(x1 - x0 + 1, dtype=int)
        for band in bands:
            ya, yb = int(band["y0"]), int(band["y1"]) + 1
            mark = ink[ya:yb, x0 : x1 + 1].max(axis=0) > 0 if yb > ya else np.zeros_like(cover, dtype=bool)
            cover += (
                cv2.morphologyEx(
                    mark.astype(np.uint8)[None, :] * 255,
                    cv2.MORPH_CLOSE,
                    cv2.getStructuringElement(cv2.MORPH_RECT, (close, 1)),
                )[0]
                > 0
            )
        # A spanning header or a long sentence may cross a gap in a few rows.
        allowed = max(1, round(0.1 * len(bands))) if len(bands) >= 4 else 0
        gaps, start = [], None
        for i, value in enumerate(cover):
            if value <= allowed and start is None:
                start = i
            elif value > allowed and start is not None:
                if i - start >= 0.25 * height:
                    gaps.append((start + i) / 2 + x0)
                start = None
        bounds = sorted(set([x0 - 1] + gaps + [x for x in xs if x0 < x < x1] + [x1 + 1]))
        bounds = [b for i, b in enumerate(bounds) if i == 0 or b - bounds[i - 1] > height * 0.5]
        columns = list(zip(bounds[:-1], bounds[1:]))
        found = {}
        for r, band in enumerate(bands):
            for l in sorted(band["lines"], key=lambda l: (l["box"][1], l["box"][0])):
                hit = [
                    c
                    for c, (a, b) in enumerate(columns)
                    if min(b, l["box"][2]) - max(a, l["box"][0]) > 0.25 * min(b - a, l["box"][2] - l["box"][0])
                ]
                hit = hit or [
                    min(range(len(columns)), key=lambda c: abs(sum(columns[c]) / 2 - (l["box"][0] + l["box"][2]) / 2))
                ]
                pieces = self.split(l, [columns[c] for c in hit]) if len(hit) > 1 else None
                for c, (text, score) in zip(hit, pieces) if pieces else [(hit[0], (l["text"], l["score"]))]:
                    cell = found.setdefault((r, c), {"parts": [], "scores": [], "span": 1})
                    cell["parts"].append(text)
                    cell["scores"].append(score)
                    if not pieces:
                        cell["span"] = max(cell["span"], hit[-1] - hit[0] + 1)
        cells = []
        for r, band in enumerate(bands):
            covered = set()
            for c in range(len(columns)):
                if c in covered:
                    continue
                cell = found.get((r, c))
                span = min(cell["span"], len(columns) - c) if cell else 1
                covered.update(range(c, c + span))
                box = [columns[c][0], band["y0"] - 0.2 * height, columns[c + span - 1][1], band["y1"] + 0.2 * height]
                if cell:
                    text, score = join(cell["parts"]), round(min(cell["scores"]), 4)
                else:
                    text, score = self.ink_read(box, height)
                cells.append(
                    {"row": r, "col": c, "row_span": 1, "col_span": span, "box": box, "text": text, "score": score}
                )
        return cells

    def split(self, line, columns):
        """Pieces (text, score) of a line that crosses column boundaries, one per column; None when it is one
        cell: a line is split only where its own ink has a clear gap near each boundary."""
        h = line["box"][3] - line["box"][1]
        x0, y0, x1, y1 = (int(round(v)) for v in line["box"])
        ink = self.ink[max(0, y0) : y1, max(0, x0) : x1]
        if not ink.size:
            return None
        ink = ink.max(axis=0) > 0
        gaps, start = [], None
        for i, value in enumerate(list(ink) + [True]):
            if not value and start is None:
                start = i
            elif value and start is not None:
                if i - start >= 0.4 * h:
                    gaps.append((x0 + start, x0 + i))
                start = None
        for (_, a_end), (b_start, _) in zip(columns[:-1], columns[1:]):
            edge = (a_end + b_start) / 2
            if not any(g0 - 1.5 * h <= edge <= g1 + 1.5 * h for g0, g1 in gaps):
                return None
        pieces = []
        for a, b in columns:
            text, score = self.rec([max(a, line["box"][0]), line["box"][1], min(b, line["box"][2]), line["box"][3]])
            if not text or score < 0.5:
                return None
            pieces.append((text, round(score, 4)))
        whole = re.sub(r"\s", "", line["text"])
        joined = "".join(re.sub(r"\s", "", text) for text, _ in pieces)
        if joined == whole or (len(joined) >= 0.8 * len(whole) and set(joined) <= set(whole) | set("0123456789.")):
            return pieces
        return None

    def ink_read(self, box, height):
        """(text, score) of a cell no detected line fell in: its character-sized marks read on their own, or a
        dash; ('', None) when it holds none."""
        x0, y0, x1, y1 = (int(round(v)) for v in box)
        inner = self.ink[max(0, y0) + 4 : y1 - 4, max(0, x0) + 4 : x1 - 4]
        if inner.size == 0:
            return "", None
        _, _, stats, _ = cv2.connectedComponentsWithStats(inner, connectivity=8)
        flat = [s for s in stats[1:] if s[3] <= height * 0.3 and max(2 * s[3], 0.15 * height) <= s[2] <= height * 2]
        marks = [s for s in stats[1:] if height * 0.35 <= s[3] <= height * 1.6 and s[2] <= height * 3]
        if not marks:
            # One short flat stroke alone in a cell is a dash (the recognizer reads it as 1 or 一).
            return ("-", None) if len(flat) == 1 else ("", None)
        pad = int(height * 0.25)
        text, score = self.rec(
            [
                x0 + 4 + min(s[0] for s in marks) - pad,
                y0 + 4 + min(s[1] for s in marks) - pad,
                x0 + 4 + max(s[0] + s[2] for s in marks) + pad,
                y0 + 4 + max(s[1] + s[3] for s in marks) + pad,
            ]
        )
        if DASH.fullmatch(text or ""):
            return "-", round(score, 4)
        return (text, round(score, 4)) if text and score >= 0.5 else ("", None)

    def fill(self, cells, lines, height):
        """Put every line in the cell holding it; a line crossing cells is split where its ink has gaps."""
        for cell in cells:
            cell["parts"], cell["scores"] = [], []
        for l in sorted(lines, key=lambda l: (l["box"][1], l["box"][0])):
            cx, cy = (l["box"][0] + l["box"][2]) / 2, (l["box"][1] + l["box"][3]) / 2
            hits = sorted(
                (
                    c
                    for c in cells
                    if c["box"][1] <= cy <= c["box"][3]
                    and min(c["box"][2], l["box"][2]) - max(c["box"][0], l["box"][0])
                    > 0.25 * (l["box"][2] - l["box"][0])
                ),
                key=lambda c: c["box"][0],
            )
            if len(hits) > 1:
                pieces = self.split(l, [(c["box"][0], c["box"][2]) for c in hits])
                if pieces:
                    for c, (text, score) in zip(hits, pieces):
                        c["parts"].append(text)
                        c["scores"].append(score)
                    continue
            target = (
                hits[0]
                if len(hits) == 1
                else next(
                    (c for c in cells if c["box"][0] <= cx <= c["box"][2] and c["box"][1] <= cy <= c["box"][3]), None
                )
            )
            if target:
                target["parts"].append(l["text"])
                target["scores"].append(l["score"])
        for cell in cells:
            parts, scores = cell.pop("parts"), cell.pop("scores")
            if parts:
                cell["text"], cell["score"] = join(parts), round(min(scores), 4)
            else:
                cell["text"], cell["score"] = self.ink_read(cell["box"], height)


def straightened_table(ocr, image, area):
    """Read the table in `area` of an upright page: a skewed table is straightened on its own crop first, and
    its boxes are mapped back to the page."""
    x0, y0, x1, y1 = (max(0, int(v)) for v in area)
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        return {"style": "empty", "cells": []}
    _, hor, _ = rule_masks(crop)
    skew, matrix = skew_angle(hor, crop.shape[1]), None
    if abs(skew) > 0.1:
        matrix = cv2.getRotationMatrix2D((crop.shape[1] / 2, crop.shape[0] / 2), skew, 1.0)
        crop = cv2.warpAffine(
            crop, matrix, (crop.shape[1], crop.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
        )
    reader = Reader(ocr)
    reader.set_image(crop)
    result = reader.table([0, 0, crop.shape[1], crop.shape[0]])
    inverse = cv2.invertAffineTransform(matrix) if matrix is not None else None
    for cell in result["cells"]:
        bx0, by0, bx1, by1 = cell.pop("box")
        points = [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)]
        if inverse is not None:
            points = [tuple(inverse @ np.array([px, py, 1.0])) for px, py in points]
        cell["box"] = [
            x0 + min(p[0] for p in points),
            y0 + min(p[1] for p in points),
            x0 + max(p[0] for p in points),
            y0 + max(p[1] for p in points),
        ]
    result["skew_degrees"] = round(skew, 3)
    result["text_scale"] = round(reader.scale, 3)
    return result


def reading_order(lines):
    """Lines top to bottom; lines sharing a row (vertical overlap) left to right."""
    rows = []
    for line in sorted(lines, key=lambda l: l["box"][1]):
        middle = (line["box"][1] + line["box"][3]) / 2
        if rows and rows[-1]["y0"] <= middle <= rows[-1]["y1"]:
            rows[-1]["lines"].append(line)
            rows[-1]["y1"] = max(rows[-1]["y1"], line["box"][3])
        else:
            rows.append({"y0": line["box"][1], "y1": line["box"][3], "lines": [line]})
    return [line for row in rows for line in sorted(row["lines"], key=lambda l: l["box"][0])]


class Mapping:
    """Points in the rotated, straightened image back to the input image's pixels."""

    def __init__(self, shape, angle, matrix):
        self.h, self.w = shape[:2]
        self.angle = angle
        self.inverse = cv2.invertAffineTransform(matrix) if matrix is not None else None

    def point(self, x, y):
        if self.inverse is not None:
            x, y = self.inverse @ np.array([x, y, 1.0])
        if self.angle == 90:  # rotated (x, y) came from original (y, h-1-x)
            return y, self.h - 1 - x
        if self.angle == 180:
            return self.w - 1 - x, self.h - 1 - y
        if self.angle == 270:  # rotated (x, y) came from original (w-1-y, x)
            return self.w - 1 - y, x
        return x, y

    def box(self, points):
        mapped = [self.point(float(x), float(y)) for x, y in points]
        xs, ys = [p[0] for p in mapped], [p[1] for p in mapped]
        return [
            round(max(0.0, min(xs)), 2),
            round(max(0.0, min(ys)), 2),
            round(min(float(self.w), max(xs)), 2),
            round(min(float(self.h), max(ys)), 2),
        ]


def corners(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def upright(box):
    return [round(float(v), 2) for v in box]


def cell_record(cell, mapping):
    return {
        "row": cell["row"],
        "col": cell["col"],
        "row_span": cell["row_span"],
        "col_span": cell["col_span"],
        "text": cell["text"],
        "score": cell["score"],
        "empty": cell["empty"],
        "bbox": mapping.box(corners(*cell["box"])),
        "upright_bbox": upright(cell["box"]),
    }


def line_record(line, mapping, region=None):
    record = {
        "text": line["text"],
        "score": round(float(line["score"]), 4),
        "bbox": mapping.box(corners(*line["box"])),
        "upright_bbox": upright(line["box"]),
    }
    return {**record, "region": region} if region is not None else record


def find_frames(image):
    """The frames of the ruled tables on a whole page, from their long horizontal rules. Tables above one another
    are told apart by their column rules: no column rule crosses the space between two tables. A page whose rules
    have no column rules at all (three-line tables) gives one frame."""
    _, hor, ver = rule_masks(image)
    h, w = hor.shape
    rules = sorted(long_rules(hor, w), key=lambda r: r[1])
    if len(rules) < MIN_RULES:
        return []
    groups, columns = [[rules[0]]], (ver > 0)
    has_columns = bool(columns.any())
    spacing = float(np.median([b[1] - a[1] for a, b in zip(rules, rules[1:])]))
    for previous, rule in zip(rules, rules[1:]):
        top, bottom = int(previous[1] + previous[3]) + 2, int(rule[1]) - 2
        crossed = bottom <= top or bool(columns[top:bottom].all(axis=0).any())
        # Only a wider space than the rows' (a caption between tables) can separate; a faint column rule cannot.
        if has_columns and not crossed and rule[1] - previous[1] > 1.5 * spacing:
            groups.append([rule])
        else:
            groups[-1].append(rule)
    frames, pad = [], max(4, w // 200)
    for group in groups:
        if len(group) < MIN_RULES:
            continue
        # Medians of the rules' ends ignore paper edges and stray long strokes.
        left, right = int(np.median([r[0] for r in group])), min(w - 1, int(np.median([r[0] + r[2] for r in group])))
        top, bottom = int(min(r[1] for r in group)), min(h - 1, int(max(r[1] + r[3] for r in group)))
        frames.append([max(0, left - pad), max(0, top - pad), min(w, right + pad), min(h, bottom + pad)])
    return frames


def read_whole_page(ocr, original):
    angle, scores = orientation(ocr, original)
    image = original if ROTATIONS[angle] is None else cv2.rotate(original, ROTATIONS[angle])
    _, hor, _ = rule_masks(image)
    h, w = hor.shape
    skew, matrix = skew_angle(hor, w), None
    if abs(skew) > 0.1:
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), skew, 1.0)
        image = cv2.warpAffine(image, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    mapping = Mapping(original.shape, angle, matrix)
    reader = Reader(ocr)
    reader.set_image(image)
    tables, frames = [], []
    for frame in find_frames(image):
        found = reader.table(frame)
        if len(found["cells"]) >= PROBE_MIN_CELLS:
            frames.append(frame)
            tables.append(
                {
                    "bbox": mapping.box(corners(*frame)),
                    "style": found["style"],
                    "text_scale": round(reader.scale, 3),
                    "cells": [cell_record(cell, mapping) for cell in found["cells"]],
                }
            )
    inside = lambda line, f: (
        f[0] <= (line["box"][0] + line["box"][2]) / 2 <= f[2] and f[1] <= (line["box"][1] + line["box"][3]) / 2 <= f[3]
    )
    lines = [line for line in reading_order(reader.lines()) if not any(inside(line, f) for f in frames)]
    return {
        "rotation": angle,
        "orientation_scores": {str(k): round(v, 2) for k, v in scores.items()},
        "skew_degrees": round(skew, 3),
        "deskew_degrees": float(skew) if matrix is not None else 0.0,
        "text_scale": round(reader.scale, 3),
        "tables": tables,
        "lines": [line_record(line, mapping) for line in lines],
    }


def read_regions(ocr, image, regions):
    """The regions of an upright page (a parser's layout), each read by its kind."""
    mapping = Mapping(image.shape, 0, None)
    reader = Reader(ocr)
    reader.set_image(image)
    tables, lines, failed = [], [], []
    texts = [region for region in regions if region["kind"] == "text"]
    # Text is detected once on the whole page; each line belongs to the text region holding its centre.
    owned = {region["id"]: [] for region in texts}
    for line in reader.lines(clean=False) if texts else []:
        cx, cy = (line["box"][0] + line["box"][2]) / 2, (line["box"][1] + line["box"][3]) / 2
        holders = [
            region
            for region in texts
            if region["bbox"][0] - 6 <= cx <= region["bbox"][2] + 6
            and region["bbox"][1] - 6 <= cy <= region["bbox"][3] + 6
        ]
        if holders:
            owner = min(holders, key=lambda r: (r["bbox"][2] - r["bbox"][0]) * (r["bbox"][3] - r["bbox"][1]))
            owned[owner["id"]].append(line)
    for region in texts:
        lines.extend(line_record(line, mapping, region["id"]) for line in reading_order(owned[region["id"]]))
    for region in regions:
        if region["kind"] == "text":
            continue
        area = [float(v) for v in region["bbox"]]
        pad = 6
        area = [
            max(0.0, area[0] - pad),
            max(0.0, area[1] - pad),
            min(float(image.shape[1]), area[2] + pad),
            min(float(image.shape[0]), area[3] + pad),
        ]
        try:
            found = straightened_table(ocr, image, area)
            rows = {cell["row"] for cell in found["cells"]}
            columns = {cell["col"] for cell in found["cells"]}
            if region["kind"] == "probe" and (
                found["style"] != "ruled" or len(found["cells"]) < PROBE_MIN_CELLS or len(rows) < 2 or len(columns) < 2
            ):
                continue
            tables.append(
                {
                    "region": region["id"],
                    "probe": region["kind"] == "probe",
                    "style": found["style"],
                    "skew_degrees": found["skew_degrees"],
                    "text_scale": found["text_scale"],
                    "bbox": mapping.box(corners(*area)),
                    "cells": [cell_record(cell, mapping) for cell in found["cells"]],
                }
            )
        except Exception as exc:  # one region must not cost the page its other regions
            failed.append({"region": region["id"], "error": type(exc).__name__})
    return {
        "rotation": 0,
        "skew_degrees": 0.0,
        "deskew_degrees": 0.0,
        "text_scale": round(reader.scale, 3),
        "tables": tables,
        "lines": lines,
        "region_errors": failed,
    }


def read_page(ocr, item):
    original = cv2.imread(str(item["image"]))
    if original is None:
        raise ValueError("unreadable page image")
    page = read_regions(ocr, original, item["regions"]) if "regions" in item else read_whole_page(ocr, original)
    return {"width": original.shape[1], "height": original.shape[0], **page}


def valid_regions(regions):
    if not isinstance(regions, list):
        return False
    for region in regions:
        box = region.get("bbox") if isinstance(region, dict) else None
        if (
            not isinstance(region, dict)
            or not isinstance(region.get("id"), str)
            or region.get("kind") not in ("table", "text", "probe")
            or not isinstance(box, list)
            or len(box) != 4
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
            or box[2] <= box[0]
            or box[3] <= box[1]
        ):
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pages", required=True)
    parser.add_argument("--model-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    pages = json.loads(Path(args.pages).read_text(encoding="utf-8"))
    if not isinstance(pages, list) or not pages:
        raise SystemExit("pages must be a non-empty list")
    if any("regions" in item and not valid_regions(item["regions"]) for item in pages):
        raise SystemExit("regions must be a list of {id, kind: table|text|probe, bbox}")
    ocr = load_ocr(args.model_root)
    result = {"version": VERSION, "pages": []}
    for item in pages:
        try:
            result["pages"].append({"page": int(item["page"]), **read_page(ocr, item)})
        except Exception as exc:  # one bad page must not cost the other pages their reading
            result["pages"].append({"page": int(item["page"]), "error": type(exc).__name__})
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
