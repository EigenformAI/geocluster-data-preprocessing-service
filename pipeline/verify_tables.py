"""Stage 5 VERIFY: cross-check each table CSV against the ground-truth text layer.

For every table:
  1. Pull the numeric tokens out of the CSV.
  2. Compare them against the numbers pdftotext found on the same page (a
     different extraction path than Mistral OCR).
  3. Confidence = pass when (almost) every CSV number is backed by the text
     layer and the rows aren't ragged; else 'low' -> review queue.
  4. Derive the table's bounding box from the matched words' positions and crop
     the page render so a human can verify the CSV against the source image.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from PIL import Image

from . import ground_truth as gt
from .render import DPI
from .util import Paths, mark_stage_done, read_json, write_json

STAGE = "verify"
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[str]:
    return [m.group(0).replace(",", "") for m in _NUM_RE.finditer(text or "")]


def _csv_numbers(csv_path: Path) -> tuple[list[str], list[int]]:
    nums: list[str] = []
    row_lens: list[int] = []
    with open(csv_path, newline="") as f:
        for row in csv.reader(f):
            row_lens.append(len(row))
            for cell in row:
                nums.extend(_numbers(cell))
    return nums, row_lens


def _page_words(gt_pages: list[dict], page: int) -> list[dict]:
    idx = page - 1
    if 0 <= idx < len(gt_pages) and gt_pages[idx].get("page") == page:
        return gt_pages[idx].get("words", [])
    for p in gt_pages:  # fallback if ordering is off
        if p.get("page") == page:
            return p.get("words", [])
    return []


def _crop_bbox(words: list[dict], wanted: set[str]) -> tuple[tuple[float, float, float, float], bool] | None:
    matched = [w for w in words if _numbers(w["t"]) and _numbers(w["t"])[0] in wanted]
    if not matched:
        return None
    x0 = min(w["x0"] for w in matched)
    y0 = min(w["y0"] for w in matched)
    x1 = max(w["x1"] for w in matched)
    y1 = max(w["y1"] for w in matched)
    tall = sum(1 for w in matched if (w["y1"] - w["y0"]) > 1.5 * (w["x1"] - w["x0"]))
    rotated = tall > len(matched) / 2
    return (x0, y0, x1, y1), rotated


def _save_crop(paths: Paths, page: int, bbox, rotated: bool, out: Path) -> None:
    png = paths.page_png(page)
    if not png.exists():
        return
    scale = DPI / 72.0
    margin = 14
    img = Image.open(png)
    x0, y0, x1, y1 = bbox
    box = (
        max(0, int(x0 * scale) - margin),
        max(0, int(y0 * scale) - margin),
        min(img.width, int(x1 * scale) + margin),
        min(img.height, int(y1 * scale) + margin),
    )
    crop = img.crop(box)
    if rotated:
        crop = crop.transpose(Image.ROTATE_90)
    crop.save(out)


def run(paths: Paths, force: bool = False) -> dict:
    gt_pages = gt.load(paths).get("pages", [])
    table_index = read_json(paths.root / "tables_index.json", default=[]) or []

    # A page with almost no digital numbers is image/scanned: there is nothing to
    # audit OCR against, so its tables are 'unverifiable' rather than 'low'.
    MIN_PAGE_DIGITS = 20

    results = []
    low = unverifiable = 0
    for rec in table_index:
        csv_path = paths.root / rec["csv"]
        page = rec["page"]
        csv_nums, row_lens = _csv_numbers(csv_path)
        words = _page_words(gt_pages, page)
        page_num_list = [n for w in words for n in _numbers(w["t"])]
        page_num_set = set(page_num_list)

        found = [n for n in csv_nums if n in page_num_set]
        missing = [n for n in csv_nums if n not in page_num_set]
        coverage = (len(found) / len(csv_nums)) if csv_nums else 1.0
        ragged = len(set(row_lens)) > 1

        if rec.get("bbox"):  # tight per-region box from det_tables (vision_hybrid)
            bbox_info = (tuple(rec["bbox"]), False)
        else:
            bbox_info = _crop_bbox(words, set(csv_nums))
        rotated = bool(bbox_info and bbox_info[1])
        passed = coverage >= 0.95 and not ragged
        if passed:
            confidence = "high"
        elif len(page_num_list) < MIN_PAGE_DIGITS:
            confidence = "unverifiable"
            unverifiable += 1
        else:
            confidence = "low"
            low += 1

        meta = {
            "id": rec["id"],
            "page": page,
            "section": rec.get("section"),
            "method": rec.get("method", "mistral_ocr"),
            "n_rows": rec["n_rows"],
            "n_cols": rec["n_cols"],
            "rotation_deg": 90 if rotated else 0,
            "text_layer_checksum": {
                "csv_numbers": len(csv_nums),
                "found": len(found),
                "missing": missing[:50],
                "coverage": round(coverage, 4),
                "ragged_rows": ragged,
                "page_digit_count": len(page_num_list),
                "pass": passed,
            },
            "confidence": confidence,
            "csv": rec["csv"],
        }

        crop_rel = None
        crop_path = paths.tables / f"{rec['id']}.crop.png"
        if bbox_info:
            _save_crop(paths, page, bbox_info[0], rotated, crop_path)
            if crop_path.exists():
                crop_rel = f"tables/{crop_path.name}"
        elif rec.get("crop_png") and crop_path.exists():
            # pre-written upright crop (vision_unverified image table) — keep it
            crop_rel = f"tables/{crop_path.name}"
        meta["crop_png"] = crop_rel

        write_json(paths.tables / f"{rec['id']}.meta.json", meta)
        results.append(meta)

    write_json(paths.root / "tables_verified.json", results)
    info = {"n_tables": len(results), "n_low_confidence": low, "n_unverifiable": unverifiable}
    mark_stage_done(paths, STAGE, info)
    return info
