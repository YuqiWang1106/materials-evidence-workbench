"""Cache identities for the independently installed local OCR readers."""

from collections.abc import Sequence
from functools import lru_cache
from hashlib import file_digest
from pathlib import Path

from kb_ingest.core.paths import HANDWRITING_RUNTIME as LOCAL_OCR_RUNTIME

PRIMARY_WEIGHTS = (
    "MinerU-4_models_onnx/OCR/paddleocr/ch_PP-OCRv6_tiny_det_infer.onnx",
    "MinerU-4_models_onnx/OCR/paddleocr/ch_PP-OCRv6_small_rec_infer.onnx",
)
# Inputs used by the native MLX/Transformers PaddleOCR-VL loaders.
SECOND_MODEL_FILES = (
    "config.json",
    "generation_config.json",
    "processor_config.json",
    "preprocessor_config.json",
    "added_tokens.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "tokenizer.model",
    "chat_template.jinja",
    "chat_template.json",
)
SECOND_WEIGHT_PATTERNS = (
    "*.safetensors",
    "*.safetensors.index.json",
    "pytorch_model*.bin",
    "pytorch_model*.bin.index.json",
)
SECOND_PROMPT = LOCAL_OCR_RUNTIME / "second_reader_prompt.json"


@lru_cache(maxsize=512)
def _file_hash(path: Path, _file_state: tuple[int, ...]) -> str:
    # Immutable installed weights can serve many pages and tasks in a process.
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _file_identity(path: Path) -> str | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return _file_hash(path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def _command_identity(command: Sequence[str]) -> list[tuple[str, str]]:
    # The bundled reader moves with the website. Distinct external commands remain distinct.
    identity = []
    for argument in command:
        path = Path(argument)
        if path.is_relative_to(LOCAL_OCR_RUNTIME):
            identity.append(("bundled", str(path.relative_to(LOCAL_OCR_RUNTIME))))
        else:
            identity.append(("argument", argument))
    return identity


def reader_identity(
    command: Sequence[str], version: str, model_root: str | Path, weights: Sequence[str]
) -> dict[str, object]:
    root = Path(model_root)
    digests = {name: _file_identity(root / name) for name in weights}
    return {
        "reader_version": version,
        "command": _command_identity(command),
        "weights": digests,
        # Standalone readers can select a model with a different on-disk layout.
        "model_directory": str(root) if not any(digests.values()) else None,
    }


def second_reader_identity(
    command: Sequence[str] | None, version: str, model_root: str | Path | None
) -> dict[str, object] | None:
    if not command or not model_root:
        return None
    root = Path(model_root)
    weights = [path.name for pattern in SECOND_WEIGHT_PATTERNS for path in root.glob(pattern)]
    templates = [str(path.relative_to(root)) for path in (root / "chat_templates").glob("*.jinja")]
    return {
        **reader_identity(command, version, root, (*SECOND_MODEL_FILES, *weights, *templates)),
        "prompt_sha256": _file_identity(SECOND_PROMPT),
    }
