"""Local OCR of scanned pages, photos and handwriting, adapted into the shared document IR.

The reader (runtime/handwriting/handwriting_reader.py) runs as a subprocess in the MinerU reader
environment; this module only renders pages, validates its JSON and converts pixels to rendered-page
points. Table cells carry their own bbox and the local reading, with zero-based rows and columns like
MinerU's tables. Two uses:

- `read_handwriting_ir`: a photo (JPG/PNG), or a PDF marked as handwritten, read page by page without a
  parser (orientation, skew, one ruled table and the lines around it). Regions get source='handwriting'.
- `apply_local_ocr`: the scanned pages of a PDF already read by MinerU (pages without a text layer). MinerU
  keeps the layout; its table and text regions are read again locally (any table style, every line with its
  score), a large picture is checked for a ruled table, and a scanned page MinerU could not read at all is
  read page by page. Such regions get source='local_ocr'; MinerU's own reading stays beside them
  (`parser_text`, `parser_cells`).

A second local reader (runtime/handwriting/second_reader.py, PaddleOCR-VL in its own environment) reads
every non-empty cell and line again. Both readings are kept; when they agree the low-score flag is
dropped, when they differ the item is flagged readers_disagree and keeps the first reading. Nothing is
corrected automatically: flags are a low recognition score, a number far outside the other numbers of
its column, a disagreement of the two readers. When a reader is not configured or fails, the document
keeps what was read with an explicit gap (pages without any reading go to the existing page-by-page
visual reading), so every document still gets a result. Reader logs stay in the work directory; gaps
carry error codes only, never log text.
"""

from __future__ import annotations

import re

LOCAL_OCR_STAGE_VERSION = "local-ocr-stage-v1"
READER_VERSION = "local-ocr-reader-v2"
SECOND_READER_VERSION = "handwriting-second-reader-v1"
HANDWRITING_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")
# A PDF page with fewer native characters than this, and a picture on it, is a scanned page.
SCANNED_TEXT_CHARACTERS = 20
# Scanned pages are rendered for local OCR at this long side (about 260 dpi for A4): table rules and small
# print stay measurable; the reader itself reduces text to its reading height.
OCR_RENDER_LONG_SIDE = 3000
# MinerU region kinds read again as text, as a table, or checked for a ruled table (a large picture).
TEXT_KINDS = {
    "text",
    "title",
    "doc_title",
    "paragraph_title",
    "section_title",
    "heading",
    "ref_text",
    "aside_text",
    "list",
    "abstract",
    "caption",
    "footnote",
    "table_caption",
    "table_footnote",
    "image_caption",
}
PICTURE_KINDS = {"image", "chart", "figure"}
PROBE_PAGE_SHARE = 0.1
# The second reader reads table cells and only the text lines that hold a number or scored low; past this many
# items the rest keep the first reading with a gap (a CPU-only machine needs about 6-7 s per item).
MAX_SECOND_ITEMS = 600
# Which items the second reader reads on printed scans; part of the reading's cache key.
SECOND_ITEM_POLICY = "printed-vote-v1"
LOW_SCORE = 0.9
# A number this many times above or below its column's median is flagged. Recipe columns legitimately span
# 0.02 to 160 (ratio < 1000); a misread such as 20505 for 5.05 is four orders of magnitude off.
OUTLIER_RATIO = 1000
RENDER_LONG_SIDE = 2000
READER_TIMEOUT_SECONDS = 600
READER_SECONDS_PER_PAGE = 120
QUEUE_TIMEOUT_SECONDS = 1800  # waiting for the machine's single local parser (MinerU or this reader)
# Second reader: about 0.4 s per item with mlx on an Apple M2 and on a GPU; about 6-7 s per item on a CPU
# (transformers, float32). CPU-only machines raise the per-item allowance (second_seconds_per_item).
SECOND_READER_BASE_SECONDS = 120
SECOND_READER_SECONDS_PER_ITEM = 2.0
_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
# The second reader's answers that are not a reading: formula markup and its refusal phrases.
_NOT_A_READING = re.compile(
    r"\\[(\[]|^\$.*\$$|\[unab|unable to|too blurry|cannot (?:be )?recogni|no (?:visible |legible )?text", re.IGNORECASE
)
