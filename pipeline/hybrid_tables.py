"""Hybrid reconstruction for hard N-up pages: Sonnet structure + text-layer numbers.

A few pages place two or more data tables side by side ("N-up"). On those,
Mistral OCR fabricates the numbers and the deterministic clustering jams the
columns (left-edge anchoring can't tell a Claim ID from a partner company number
when both are 7-digit and adjacent). Empirically only full *visual* table
understanding recovers them — and at runtime that means a capable vision model.

Division of labour (no model ever supplies a trusted number):

* the page is split into side-by-side regions deterministically
  (`det_tables._split_x_regions`);
* each region is **cropped** from the page render so the sub-table fills the
  image — this is what makes the read accurate (a full-page send downscales the
  dense table below legibility);
* **Anthropic vision (`claude-sonnet-4-6`)** transcribes each crop as a grid —
  used for *structure*; its numbers are then **reconciled against the pdftotext
  layer**: a value present verbatim is confirmed, anything else is left in place
  but **flagged** (the `verify` stage marks the table low-confidence). We never
  auto-correct a number — a misread can be one digit from a *different* real key.

This is the one place the otherwise Mistral-only runtime calls Anthropic, and
only for the handful of flagged N-up pages. Results cache by crop-image hash.
"""

from __future__ import annotations

import base64
import io
import json
import re

from .util import read_json, sha256_bytes, write_json

MODEL = "claude-sonnet-4-6"
PROMPT_VERSION = "v2"
_MIN_BACKED = 0.80  # below this, the crop read is too poor — fall back to deterministic

_PROMPT = (
    "This image is a single data table from a mineral technical report. "
    "Return STRICT JSON only (no prose, no markdown fences): "
    '{"headers":[...],"rows":[[cell,...],...]}. '
    "Use ONE fixed set of columns for EVERY row. Some rows are indented "
    "continuation/sub-rows that begin with a label such as 'including' or "
    "'which includes' — for those, put the label text in the FIRST column and "
    "align the remaining values under the SAME columns as the normal data rows. "
    "Never add an extra column for such a label, and never shift a sub-row's "
    "values into different columns than the main rows. "
    'Transcribe each cell EXACTLY as printed; an empty cell is "". '
    "Do not skip, merge, or invent rows. Preserve row order top-to-bottom."
)

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def available() -> bool:
    """True if an Anthropic key is configured (the structure step needs it)."""
    import os

    from dotenv import load_dotenv

    load_dotenv(override=True)
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client():
    from dotenv import load_dotenv

    load_dotenv(override=True)
    import anthropic

    return anthropic.Anthropic()


def _sonnet_table(client, crop_png: bytes, cache_dir) -> dict | None:
    """Transcribe one cropped sub-table to {headers, rows}. Cached by image hash.
    Retries with backoff so a transient rate-limit doesn't drop the page to the
    deterministic fallback."""
    import time

    cache_file = cache_dir / f"{sha256_bytes(crop_png, PROMPT_VERSION.encode(), MODEL.encode())}.json"
    cached = read_json(cache_file)
    if cached:
        return cached
    b64 = base64.b64encode(crop_png).decode()
    for attempt in range(3):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=12000,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                            {"type": "text", "text": _PROMPT},
                        ],
                    }
                ],
            )
            txt = "".join(b.text for b in resp.content if b.type == "text")
            txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.M).strip()
            data = json.loads(txt)
            if not isinstance(data.get("rows"), list):
                return None
            write_json(cache_file, data)
            return data
        except Exception:
            if attempt < 2:
                time.sleep(5 * 2**attempt)  # 5s, 10s
                continue
            return None


def _grid_rows(data: dict) -> list[list[str]]:
    headers = [str(h) for h in data.get("headers", [])]
    rows = [[("" if c is None else str(c)) for c in r] for r in data.get("rows", [])]
    width = max([len(headers)] + [len(r) for r in rows], default=0)
    out = []
    if any(headers):
        out.append((headers + [""] * width)[:width])
    for r in rows:
        out.append((r + [""] * width)[:width])
    return out


def _backed_fraction(rows: list[list[str]], truth: set[str]) -> tuple[int, int]:
    total = backed = 0
    for r in rows:
        for cell in r:
            for tok in _NUM.findall(cell.replace(",", "")):
                total += 1
                if tok in truth:
                    backed += 1
    return backed, total


def hybrid_page_tables(
    page_png: bytes,
    regions_pts: list[tuple[float, float]],
    width_pts: float,
    truth_numbers: set[str],
    cache_dir,
) -> list[list[list[str]]] | None:
    """Reconstruct an N-up page: one Sonnet-read table per region, numbers
    reconciled to `truth_numbers`. Returns a list of tables (rows of cells), or
    None if the vision model is unavailable or every region read poorly."""
    from PIL import Image

    client = _client()
    img = Image.open(io.BytesIO(page_png))
    W, H = img.size
    tables: list[list[list[str]]] = []
    any_ok = False
    for x0, x1 in regions_pts:
        # small margin so edge columns aren't clipped, scaled to the region so a
        # narrow N-up strip doesn't pull in its neighbours' columns
        m = max(6.0, 0.04 * (x1 - x0))
        a = max(0.0, (x0 - m) / width_pts)
        b = min(1.0, (x1 + m) / width_pts)
        crop = img.crop((int(a * W), 0, int(b * W), H))
        buf = io.BytesIO()
        crop.save(buf, "PNG")
        data = _sonnet_table(client, buf.getvalue(), cache_dir)
        if not data:
            return None  # a region failed entirely -> let caller fall back
        rows = _grid_rows(data)
        backed, total = _backed_fraction(rows, truth_numbers)
        if total and backed / total < _MIN_BACKED:
            return None  # poor read (bad crop) -> deterministic is safer
        any_ok = True
        tables.append(rows)
    return tables if any_ok else None
