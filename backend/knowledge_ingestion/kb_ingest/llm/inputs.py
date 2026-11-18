"""Validate typed file/image parts before reserving or dispatching a request."""

import base64
import binascii
import json
from pathlib import Path

from .identity import Json

_INPUT_FILE_TYPES = {
    ".pdf": ("application/pdf", "pdf"),
    ".json": ("application/json", "json"),
    ".md": ("text/markdown", "text"),
    ".markdown": ("text/markdown", "text"),
    ".txt": ("text/plain", "text"),
}
_RESPONSES_FILE_LIMIT = 50_000_000


def _checked_filename(filename: object) -> str:
    suffix = Path(filename).suffix.lower() if isinstance(filename, str) else ""
    if (
        not isinstance(filename, str)
        or suffix not in _INPUT_FILE_TYPES
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or any(ord(char) < 32 for char in filename)
        or len(filename) > 255
    ):
        message = "invalid input file filename"
        raise ValueError(message)
    return suffix


def _file_part(file: Json) -> tuple[Json, int, bool]:
    if not isinstance(file, dict) or set(file) - {"filename", "file_data", "detail"}:
        message = "invalid input file fields"
        raise ValueError(message)
    filename, data, detail = file.get("filename"), file.get("file_data"), file.get("detail", "auto")
    suffix = _checked_filename(filename)
    media_type, kind = _INPUT_FILE_TYPES[suffix]
    prefix = f"data:{media_type};base64,"
    if not isinstance(data, str) or not data.startswith(prefix):
        message = "invalid input file data or media type"
        raise ValueError(message)
    if (kind == "pdf" and detail not in {"auto", "low", "high"}) or (kind != "pdf" and "detail" in file):
        message = "invalid input file detail"
        raise ValueError(message)
    try:
        decoded = base64.b64decode(data[len(prefix) :], validate=True)
    except (ValueError, binascii.Error):
        message = "invalid input file base64 data"
        raise ValueError(message) from None
    _validate_file_content(kind, decoded)
    part = {"type": "input_file", "filename": filename, "file_data": data}
    if kind == "pdf":
        part["detail"] = detail
    return part, len(decoded), kind == "pdf"


def _validate_file_content(kind: str, decoded: bytes) -> None:
    if not decoded or len(decoded) >= _RESPONSES_FILE_LIMIT:
        message = "invalid input file bytes or size"
        raise ValueError(message)
    if kind == "pdf":
        if not decoded.startswith(b"%PDF-"):
            message = "invalid PDF file bytes"
            raise ValueError(message)
    else:
        try:
            text = decoded.decode("utf-8")
            if kind == "json":
                json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = "invalid text input file content"
            raise ValueError(message) from None


def _require(valid: bool, message: str) -> None:
    if not valid:
        raise ValueError(message)


def _validated_input_files(files: list[Json] | None) -> tuple[list[Json], int, bool]:
    if files is None:
        return [], 0, False
    _require(isinstance(files, list), "invalid input files")
    parts = []
    total_size = 0
    has_pdf = False
    for file in files:
        part, size, pdf = _file_part(file)
        total_size += size
        has_pdf |= pdf
        if total_size > _RESPONSES_FILE_LIMIT:
            message = "input files exceed combined size limit"
            raise ValueError(message)
        parts.append(part)
    return parts, total_size, has_pdf


def _validated_image_inputs(images: list[str] | None, *, file_bytes: int) -> list[str]:
    if images is None:
        return []
    _require(isinstance(images, list), "invalid image inputs")
    output = []
    total_size = file_bytes
    for image in images:
        if not isinstance(image, str) or not image.startswith("data:image/") or ";base64," not in image:
            message = "invalid image input"
            raise ValueError(message)
        header, encoded = image.split(",", 1)
        if header.split(";", 1)[0] not in {"data:image/png", "data:image/jpeg", "data:image/webp"}:
            message = "unsupported image input type"
            raise ValueError(message)
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            message = "invalid image input base64"
            raise ValueError(message) from None
        total_size += len(raw)
        if not raw or total_size > _RESPONSES_FILE_LIMIT:
            message = "input files and images exceed combined size limit"
            raise ValueError(message)
        output.append(image)
    return output
