"""Immutable inline file/image bounds, independent of model billing."""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any, Never


class ModelInputError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def _reject() -> Never:
    raise ModelInputError("unsupported_controlled_input", 400)


@dataclass
class _InputBounds:
    max_pages: int
    total_bytes: int = 0
    pages: int = 0
    images: int = 0

    def decode(self, data: object, mime: str) -> bytes:
        prefix = "data:" + mime + ";base64,"
        limit = (70 if self.max_pages == 30 else 24) * 1024 * 1024
        if type(data) is not str or not data.startswith(prefix) or len(data) > limit:
            _reject()
        try:
            raw = base64.b64decode(data[len(prefix) :], validate=True)
        except (ValueError, binascii.Error):
            _reject()
        if not raw or base64.b64encode(raw).decode() != data[len(prefix) :]:
            _reject()
        self.total_bytes += len(raw)
        if self.total_bytes > (50_000_000 if self.max_pages == 30 else 16 * 1024 * 1024):
            _reject()
        return raw

    def scan(self, value: object, role: object = None) -> None:
        if isinstance(value, list):
            for item in value:
                self.scan(item, role)
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if kind in ("input_file", "input_image"):
            if role != "user":
                _reject()
            if kind == "input_file":
                self.file(value)
            else:
                self.image(value)
            if self.pages > self.max_pages:
                _reject()
            return
        for key, item in value.items():
            self.scan(item, value.get("role", role) if key == "content" else role)

    def file(self, value: dict[str, Any]) -> None:
        if set(value) - {"type", "filename", "file_data", "detail"} or not {"filename", "file_data"} <= value.keys():
            _reject()
        name = value["filename"]
        if (
            type(name) is not str
            or not name
            or len(name) > 255
            or "/" in name
            or "\\" in name
            or any(ord(c) < 32 for c in name)
        ):
            _reject()
        if self.max_pages == 30 and name.lower().endswith(".json"):
            if "detail" in value:
                _reject()
            raw = self.decode(value["file_data"], "application/json")
            try:
                json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                _reject()
            return
        if not name.lower().endswith(".pdf") or value.get("detail", "auto") not in ("auto", "low", "high"):
            _reject()
        raw = self.decode(value["file_data"], "application/pdf")
        if not raw.startswith(b"%PDF-"):
            _reject()
        self.pages += _pdf_pages(raw)

    def image(self, value: dict[str, Any]) -> None:
        if (
            set(value) - {"type", "image_url", "detail"}
            or "image_url" not in value
            or value.get("detail", "high") not in ("auto", "low", "high")
        ):
            _reject()
        url = value["image_url"]
        mime = next(
            (
                m
                for m in ("image/png", "image/jpeg", "image/webp")
                if type(url) is str and url.startswith("data:" + m + ";base64,")
            ),
            None,
        )
        if not mime:
            _reject()
        raw = self.decode(url, mime)
        _validate_image(raw, mime)
        self.images += 1
        if self.images > 128:
            _reject()
        if self.max_pages == 4:
            self.pages += 1


def _pdf_pages(raw: bytes) -> int:
    import fitz

    try:
        with fitz.open(stream=raw, filetype="pdf") as doc:
            if doc.is_encrypted or doc.page_count < 1:
                _reject()
            return int(doc.page_count)
    except ModelInputError:
        raise
    except Exception as exc:
        raise ModelInputError("unsupported_controlled_input", 400) from exc


def _validate_image(raw: bytes, mime: str) -> None:
    import fitz

    signatures = {
        "image/png": raw.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": raw.startswith(b"\xff\xd8\xff"),
        "image/webp": raw.startswith(b"RIFF") and raw[8:12] == b"WEBP",
    }
    if not signatures[mime]:
        _reject()
    try:
        pix = fitz.Pixmap(raw)
        if not 0 < pix.width <= 8192 or not 0 < pix.height <= 8192 or pix.width * pix.height > 32_000_000:
            _reject()
    except ModelInputError:
        raise
    except Exception as exc:
        raise ModelInputError("unsupported_controlled_input", 400) from exc


def validate_controlled_input(request: object, *, max_pages: int) -> None:
    _InputBounds(max_pages).scan(request)
