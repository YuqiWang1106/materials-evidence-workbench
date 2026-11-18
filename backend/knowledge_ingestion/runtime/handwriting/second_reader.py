"""Second local reading of handwritten cells and lines with PaddleOCR-VL, for comparison with the first reader.

Runs as a subprocess in its own environment, never in the backend environment. Two backends read the same
official PaddleOCR-VL weights: mlx (mlx-vlm, Apple Silicon) and transformers (PyTorch; CUDA when available,
otherwise CPU; the Linux route). `--backend auto` picks mlx on Apple Silicon when it is installed, otherwise
transformers.

Each page image is turned and straightened exactly as the first reader did (clockwise rotation, then the
deskew angle about the image centre), and each item is cropped there from its upright box, so a cell of a
skewed table does not pick up its neighbours' rules. Tiny crops are enlarged and read with the model's plain
OCR task. Only the text is returned; comparing it with the first reading is the backend's job. One failing
item is reported on its own and does not stop the others.

Usage: python -I second_reader.py --items ITEMS.json --model-dir MODEL --output OUT.json [--backend auto]
       [--dtype auto]   (extra flags go in the configured command, e.g. ["python", "-I", "second_reader.py",
                         "--backend", "transformers", "--dtype", "bfloat16"])
ITEMS.json: [{"id": str, "kind": "cell"|"line", "image": path, "rotation": 0|90|180|270,
              "deskew": degrees, "upright_bbox": [x0, y0, x1, y1], "scale": optional reduction (0, 1]}]
              (pixels of the turned, straightened page; "scale" is the first reader's reading size)
An item without upright_bbox gives "bbox" in the page image's own pixels and is cropped, then turned.
Crops are written next to OUT.json in "<OUT stem>-crops/" so the backend can remove them even if this
process is stopped. OUT.json is rewritten (atomically) every SAVE_EVERY items, so a stopped run still leaves
the readings made so far.

Tested with mlx-vlm 0.7.11, and with torch 2.15 + transformers 5.22 (the model loads natively, without
remote code; transformers 5 or later is required).
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import sys
from pathlib import Path

VERSION = "handwriting-second-reader-v1"
PROMPT = json.loads(Path(__file__).with_name("second_reader_prompt.json").read_text(encoding="utf-8"))["text"]
# A small margin around every box. Cropping cells 3 px inside their rules, as the first reader does, kept rule
# edges out but cut final strokes, and the two readers then agreed on the same misreading (2.02 for 2.03);
# catching such misreadings is the point of the second reading. Revisit with more samples.
PAD = {"cell": 3, "line": 3}
SAVE_EVERY = 10
MIN_SIDE = 64  # strokes of a small cell survive the model's patching only when enlarged
MAX_TOKENS = {"cell": 64, "line": 256}


def page_view(views, item):
    """The page image turned and straightened like the first reader's working image (cached per page)."""
    from PIL import Image

    rotation, deskew = int(item.get("rotation") or 0) % 360, float(item.get("deskew") or 0.0)
    key = (item["image"], rotation, deskew)
    if key not in views:
        if any(cached[0] != item["image"] for cached in views):
            views.clear()  # items come page by page; keep only the current page's images
        image = Image.open(item["image"]).convert("RGB")
        if rotation:
            image = image.rotate(-rotation, expand=True)  # clockwise, like cv2.rotate in the first reader
        if deskew:
            # cv2.getRotationMatrix2D(centre, angle) and PIL rotate(angle) both turn counter-clockwise.
            image = image.rotate(
                deskew, resample=Image.BILINEAR, center=(image.width / 2, image.height / 2), fillcolor=(255, 255, 255)
            )
        views[key] = image
    return views[key]


def _box(image, bbox, pad):
    x0, y0, x1, y1 = (float(value) for value in bbox)
    box = (
        max(0, int(x0) - pad),
        max(0, int(y0) - pad),
        min(image.width, int(x1 + 0.999) + pad),
        min(image.height, int(y1 + 0.999) + pad),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("empty crop")
    return box


def crop(views, item):
    from PIL import Image

    pad = PAD.get(item.get("kind"), 3)
    if item.get("upright_bbox"):
        view = page_view(views, item)
        part = view.crop(_box(view, item["upright_bbox"], pad))
    else:
        image = page_view(views, {"image": item["image"]})
        part = image.crop(_box(image, item["bbox"], pad)).rotate(-int(item.get("rotation") or 0), expand=True)
    # The first reader read text at a reduced size (a scan's halftone dots blend into strokes there); the same
    # crop is reduced alike when the item says so.
    reduce = float(item.get("scale") or 1.0)
    if 0 < reduce < 1:
        part = part.resize((max(1, round(part.width * reduce)), max(1, round(part.height * reduce))), Image.LANCZOS)
    scale = max(1, math.ceil(MIN_SIDE / min(part.size)))
    if scale > 1:
        part = part.resize((part.width * scale, part.height * scale), Image.LANCZOS)
    return part


class MlxReader:
    def __init__(self, model_dir):
        from mlx_vlm import generate, load
        from mlx_vlm.prompt_utils import apply_chat_template

        self.generate = generate
        self.model, self.processor = load(model_dir)
        self.prompt = apply_chat_template(self.processor, self.model.config, PROMPT, num_images=1)

    def read(self, path, image, max_tokens):
        result = self.generate(
            self.model, self.processor, self.prompt, [str(path)], max_tokens=max_tokens, temperature=0.0, verbose=False
        )
        return str(getattr(result, "text", result) or "")


class TransformersReader:
    def __init__(self, model_dir, dtype="auto"):
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # float32 is the faster choice on CPU (about 3.2 GB of memory); bfloat16 halves that for small machines.
        if dtype == "auto":
            dtype = "float32" if self.device == "cpu" else "bfloat16" if torch.cuda.is_bf16_supported() else "float16"
        dtype = getattr(torch, dtype)
        self.model = AutoModelForImageTextToText.from_pretrained(model_dir, dtype=dtype).to(self.device).eval()
        self.processor = AutoProcessor.from_pretrained(model_dir)

    def read(self, path, image, max_tokens):
        messages = [{"role": "user", "content": [{"type": "image", "image": image}, {"type": "text", "text": PROMPT}]}]
        # The processor's own size limits (preprocessor_config.json) are the model card's element-level defaults.
        inputs = self.processor.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt"
        ).to(self.device)
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
        return self.processor.decode(output[0][inputs["input_ids"].shape[-1] :], skip_special_tokens=True)


def make_reader(backend, model_dir, dtype="auto"):
    if backend == "auto":
        backend = "transformers"
        if sys.platform == "darwin" and platform.machine() == "arm64":
            try:
                import mlx_vlm  # noqa: F401

                backend = "mlx"
            except Exception:  # a missing or broken mlx install falls back to transformers
                pass
    return MlxReader(model_dir) if backend == "mlx" else TransformersReader(model_dir, dtype)


def save(output, readings, errors):
    partial = output.with_name(output.name + ".partial")
    partial.write_text(
        json.dumps({"version": VERSION, "readings": readings, "errors": errors}, ensure_ascii=False), encoding="utf-8"
    )
    partial.replace(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--items", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--backend", choices=("auto", "mlx", "transformers"), default="auto")
    parser.add_argument(
        "--dtype",
        choices=("auto", "float32", "bfloat16"),
        default="auto",
        help="transformers backend only; auto is bfloat16 on CUDA, float32 on CPU",
    )
    args = parser.parse_args(argv)
    items = json.loads(Path(args.items).read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise SystemExit("items must be a list")
    reader = make_reader(args.backend, args.model_dir, args.dtype)
    readings, errors, views = {}, {}, {}
    output = Path(args.output)
    scratch = output.with_name(output.stem + "-crops")
    scratch.mkdir(exist_ok=True)
    try:
        for index, item in enumerate(items):
            try:
                path = scratch / f"{index}.png"
                image = crop(views, item)
                image.save(path)
                readings[str(item["id"])] = reader.read(path, image, MAX_TOKENS.get(item.get("kind"), 64)).strip()
            except Exception as exc:  # one bad item must not cost the others their reading
                errors[str(item.get("id"))] = type(exc).__name__
            if (index + 1) % SAVE_EVERY == 0:
                save(output, readings, errors)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    save(output, readings, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
