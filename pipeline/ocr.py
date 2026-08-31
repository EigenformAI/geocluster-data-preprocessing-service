"""Stage 3 OCR: Mistral OCR -> per-page markdown + extracted images + bboxes.

Processed in page-range chunks so the 583-page report is resumable and stays
within OCR document limits. A chunk is skipped if its per-page markdown already
exists, so re-runs never re-bill.
"""

from __future__ import annotations

import base64
import os
import re
import time
from pathlib import Path

from .mistral_client import get_client
from .util import Paths, mark_stage_done, write_json

STAGE = "ocr"
MODEL = "mistral-ocr-latest"
CHUNK = 25  # pages per OCR request
TIMEOUT_MS = 600_000  # 10 min per request (large image-bearing responses)
ATTEMPTS = 3


def _process(client, url: str, chunk: list[int]) -> dict:
    last = None
    for i in range(ATTEMPTS):
        try:
            return client.ocr.process(
                model=MODEL,
                document={"type": "document_url", "document_url": url},
                pages=chunk,
                include_image_base64=True,
                timeout_ms=TIMEOUT_MS,
            ).model_dump()
        except Exception as e:  # network/timeout — back off and retry
            last = e
            time.sleep(5 * (i + 1))
    raise last


def _ocr_dir(paths: Paths) -> Path:
    return paths.root / "ocr"


def _page_md(paths: Paths, page: int) -> Path:
    return _ocr_dir(paths) / f"page_{page:04d}.md"


def _page_json(paths: Paths, page: int) -> Path:
    return _ocr_dir(paths) / f"page_{page:04d}.json"


def _decode_image(b64: str) -> bytes:
    if "," in b64 and b64.lstrip().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    return base64.b64decode(b64)


def _ext_for(image_id: str, b64: str) -> str:
    m = re.match(r"data:image/(\w+);base64", b64.lstrip())
    if m:
        return m.group(1).replace("jpeg", "jpg")
    suffix = Path(image_id).suffix.lstrip(".")
    return (suffix or "png").replace("jpeg", "jpg")


def _chunk_done(paths: Paths, pages: list[int]) -> bool:
    return all(_page_md(paths, p).exists() for p in pages)


def run(paths: Paths, n_pages: int, force: bool = False) -> dict:
    backend = os.environ.get("PIPELINE_OCR_BACKEND", "mistral").strip().lower()
    if backend == "digital":
        from . import digital_ocr

        return digital_ocr.run(paths, n_pages, force)
    if backend != "mistral":
        raise ValueError("PIPELINE_OCR_BACKEND must be one of digital, mistral")

    img_dir = _ocr_dir(paths) / "img"
    img_dir.mkdir(parents=True, exist_ok=True)

    chunks = [
        list(range(s, min(s + CHUNK, n_pages)))  # 0-indexed page numbers
        for s in range(0, n_pages, CHUNK)
    ]
    pending = [c for c in chunks if force or not _chunk_done(paths, [i + 1 for i in c])]
    if not pending:
        mark_stage_done(paths, STAGE, {"n_pages": n_pages, "model": MODEL})
        return {"skipped": True}

    client = get_client()
    uploaded = client.files.upload(
        file={"file_name": paths.source_pdf.name, "content": paths.source_pdf.read_bytes()},
        purpose="ocr",
    )
    signed = client.files.get_signed_url(file_id=uploaded.id)

    pages_written = 0
    for chunk in pending:
        resp = _process(client, signed.url, chunk)

        for page in resp.get("pages", []):
            page_no = int(page.get("index", 0)) + 1
            _page_md(paths, page_no).write_text(page.get("markdown", "") or "")

            images = []
            for img in page.get("images", []) or []:
                img_id = str(img.get("id", f"img_{len(images)}"))
                b64 = img.get("image_base64")
                rel = None
                if b64:
                    ext = _ext_for(img_id, b64)
                    fname = f"p{page_no:04d}_{Path(img_id).stem}.{ext}"
                    (img_dir / fname).write_bytes(_decode_image(b64))
                    rel = f"img/{fname}"
                images.append(
                    {
                        "id": img_id,
                        "x0": img.get("top_left_x"),
                        "y0": img.get("top_left_y"),
                        "x1": img.get("bottom_right_x"),
                        "y1": img.get("bottom_right_y"),
                        "file": rel,
                    }
                )
            write_json(
                _page_json(paths, page_no),
                {"page": page_no, "dimensions": page.get("dimensions", {}), "images": images},
            )
            pages_written += 1

    mark_stage_done(paths, STAGE, {"n_pages": n_pages, "model": MODEL})
    return {"skipped": False, "pages_written": pages_written}
