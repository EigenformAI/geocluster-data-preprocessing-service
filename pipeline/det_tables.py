"""Deterministic table reconstruction from the pdftotext digital text layer.

Mistral OCR alters numbers in dense tables. On pages that actually have a
digital text layer, the authentic values are in the pdftotext word boxes
(`ground_truth.json`). This module rebuilds tables straight from those word
positions — values are the real digital text by construction — using
column-anchor + row clustering (the layout these reports use: words align at
repeating x-anchors).

Table *detection* stays anchored to OCR (we only run on pages OCR flagged as
tabular and that are text-rich), so we never invent a table out of prose.

Words are sourced from pdfplumber (same embedded text layer, cleaner word
joining). Before clustering, the page is split on interior vertical whitespace
gaps so two side-by-side tables ("double tables") are reconstructed separately
instead of being interleaved into one wide table. A normal single table has no
interior gap, so the split is a no-op for it.
"""

from __future__ import annotations

from statistics import mean, median, pstdev

# tolerances in PDF points
_LINE_FRAC = 0.6        # share of word height for same-line grouping
_X_GAP = 8.0            # gap between left-edge clusters -> separate anchor
_ASSIGN_TOL = 28.0      # max distance from a word's left edge to its column
_MIN_CELLS = 3          # a row must populate this many columns to be "table"

# region-split (side-by-side tables) — PDF points
_GAP_MIN = 11.0         # min width of an empty vertical band to count as a gutter
_GAP_MARGIN = 40.0      # ignore gutters inside this margin of either page edge
_REGION_MIN_WORDS = 20  # each side of a split must hold at least this many words
_ROW_MIN = 4            # populated cells that mark a real data row (vs a wrapped line)

# column-structure split (N-up tables sharing a repeated header)
_HDR_TOP_FRAC = 0.20    # header band = top this fraction of the region by height
_HDR_CV_MAX = 0.20      # max coeff. of variation of a header token's repeat spacing
_HDR_MIN_PERIOD = 80.0  # min sub-table width (points) — don't slice into slivers
_HDR_MIN_AGREE = 3      # distinct header tokens that must share the same period


def _lines(words: list[dict]) -> list[list[dict]]:
    ws = sorted(words, key=lambda w: ((w["y0"] + w["y1"]) / 2, w["x0"]))
    lines: list[list[dict]] = []
    cur: list[dict] = []
    cy = ch = 0.0
    for w in ws:
        yc = (w["y0"] + w["y1"]) / 2
        h = w["y1"] - w["y0"]
        if cur and abs(yc - cy) <= max(ch, h) * _LINE_FRAC:
            cur.append(w)
            cy = sum((x["y0"] + x["y1"]) / 2 for x in cur) / len(cur)
            ch = max(ch, h)
        else:
            if cur:
                lines.append(sorted(cur, key=lambda w: w["x0"]))
            cur, cy, ch = [w], yc, h
    if cur:
        lines.append(sorted(cur, key=lambda w: w["x0"]))
    return lines


def _anchors(lines: list[list[dict]]) -> list[float]:
    """Column x-anchors = left-edge clusters supported by many distinct lines."""
    edges = sorted(w["x0"] for ln in lines for w in ln)
    clusters: list[list[float]] = []
    for x in edges:
        if clusters and x - clusters[-1][-1] > _X_GAP:
            clusters.append([])
        if not clusters:
            clusters.append([])
        clusters[-1].append(x)
    centers = [sum(c) / len(c) for c in clusters]
    min_support = max(3, int(0.2 * len(lines)))
    anchors = []
    for c in centers:
        support = sum(1 for ln in lines if any(abs(w["x0"] - c) <= _ASSIGN_TOL for w in ln))
        if support >= min_support:
            anchors.append(c)
    # merge anchors that ended up within a tolerance of each other
    merged: list[float] = []
    for c in sorted(anchors):
        if merged and c - merged[-1] <= _X_GAP:
            merged[-1] = (merged[-1] + c) / 2
        else:
            merged.append(c)
    return merged


def _row_cells(line: list[dict], anchors: list[float]) -> tuple[list[str], int]:
    cells = [""] * len(anchors)
    for w in line:
        # nearest anchor at/left of the word, else nearest overall
        best, bd = 0, float("inf")
        for i, a in enumerate(anchors):
            d = abs(w["x0"] - a)
            if d < bd:
                best, bd = i, d
        cells[best] = (cells[best] + " " + w["t"]).strip() if cells[best] else w["t"]
    populated = sum(1 for c in cells if c)
    return cells, populated


def extract_page_tables(words: list[dict]) -> list[list[list[str]]]:
    """Return a list of tables (each a list of rows, each a list of cell strings)."""
    lines = _lines(words)
    if len(lines) < 3:
        return []
    anchors = _anchors(lines)
    if len(anchors) < _MIN_CELLS:
        return []

    rows = [_row_cells(ln, anchors) for ln in lines]
    tables: list[list[list[str]]] = []
    block: list[list[str]] = []
    for cells, populated in rows:
        if populated >= _MIN_CELLS:
            block.append(cells)
        else:
            if len(block) >= 2:
                tables.append(_trim(block))
            block = []
    if len(block) >= 2:
        tables.append(_trim(block))
    return tables


def _trim(block: list[list[str]]) -> list[list[str]]:
    """Drop columns that are empty across every row of the block."""
    if not block:
        return block
    keep = [i for i in range(len(block[0])) if any(r[i] for r in block)]
    return [[r[i] for i in keep] for r in block]


# ---- region split (side-by-side / "double" tables) --------------------------


def _cx(w: dict) -> float:
    return (w["x0"] + w["x1"]) / 2


def _header_repeat_regions(words: list[dict], width: float) -> list[tuple[float, float]] | None:
    """Split an N-up table that repeats one header across N column-groups.

    Many header tokens (e.g. 'Hole ID', 'From', 'NSR') each recur with the same
    horizontal *period*; the leftmost such column's positions give the splits.
    Robust to a bridged or absent whitespace gutter (unlike _whitespace_regions).
    """
    ys = [(w["y0"] + w["y1"]) / 2 for w in words]  # word vertical centres
    band = min(ys) + _HDR_TOP_FRAC * (max(ys) - min(ys))
    pos: dict[str, list[float]] = {}
    for w, y in zip(words, ys):
        t = w["t"].strip()
        if y <= band and len(t) >= 2 and any(c.isalpha() for c in t):
            pos.setdefault(t, []).append(_cx(w))

    by_n: dict[int, list[tuple[float, list[float]]]] = {}
    for xs in (sorted(v) for v in pos.values()):
        if len(xs) < 2:
            continue
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        period = mean(gaps)
        if period >= _HDR_MIN_PERIOD and (pstdev(gaps) / period if len(gaps) > 1 else 0.0) <= _HDR_CV_MAX:
            by_n.setdefault(len(xs), []).append((period, xs))

    best: tuple[int, list[float]] | None = None
    for n, group in by_n.items():
        if n < 2 or len(group) < _HDR_MIN_AGREE:
            continue
        period = median([p for p, _ in group])
        agree = [g for g in group if abs(g[0] - period) <= 0.15 * period]
        if len(agree) >= _HDR_MIN_AGREE and (best is None or len(agree) > best[0]):
            starts = min(agree, key=lambda g: g[1][0])[1]  # leftmost repeating column
            best = (len(agree), [(0.0, period)] + [(s, period) for s in starts[1:]])
    if not best:
        return None
    bounds = [0.0] + [s - 0.08 * p for s, p in best[1][1:]] + [width]
    return list(zip(bounds, bounds[1:]))


def _whitespace_regions(words: list[dict], width: float) -> list[tuple[float, float]] | None:
    """Split on interior vertical whitespace gutters (side-by-side tables)."""
    bin_w = 2.0
    nb = int(width / bin_w) + 1
    occupied = [False] * nb
    for w in words:
        for b in range(int(w["x0"] / bin_w), int(w["x1"] / bin_w) + 1):
            if 0 <= b < nb:
                occupied[b] = True

    gutters: list[float] = []
    b = 0
    while b < nb:
        if occupied[b]:
            b += 1
            continue
        start = b
        while b < nb and not occupied[b]:
            b += 1
        x0, x1 = start * bin_w, b * bin_w
        if (x1 - x0) >= _GAP_MIN and x0 > _GAP_MARGIN and x1 < width - _GAP_MARGIN:
            gutters.append((x0 + x1) / 2)

    splits = sorted(
        g for g in gutters
        if sum(1 for w in words if _cx(w) < g) >= _REGION_MIN_WORDS
        and sum(1 for w in words if _cx(w) >= g) >= _REGION_MIN_WORDS
    )
    if not splits:
        return None
    bounds = [0.0] + splits + [width]
    return list(zip(bounds, bounds[1:]))


def _regions_word_ok(words: list[dict], regions: list[tuple[float, float]]) -> bool:
    return all(
        sum(1 for w in words if x0 <= _cx(w) < x1) >= _REGION_MIN_WORDS
        for x0, x1 in regions
    )


def _split_x_regions(words: list[dict], width: float) -> list[tuple[float, float]]:
    """Split a page into side-by-side / N-up table regions.

    Tries a repeated-header (column-structure) split first — it handles N-up
    tables even when the gutter is bridged or absent — then falls back to a
    whitespace-gutter split. A page with neither signal stays one region, so
    single tables are untouched.
    """
    if not words or not width:
        return [(0.0, width or 0.0)]
    for regions in (_header_repeat_regions(words, width), _whitespace_regions(words, width)):
        if regions and len(regions) >= 2 and _regions_word_ok(words, regions):
            return regions
    return [(0.0, width)]


def _region_table(words: list[dict]) -> list[list[list[str]]]:
    """Reconstruct one region as a single table, folding wrapped continuation
    lines into their nearest data row.

    These tables have multi-line cells (e.g. a buyback-option paragraph) that
    wrap above/below the main data line. Dense lines (>= _ROW_MIN populated
    cells) seed logical rows; every other line attaches to the nearest seed by
    vertical position. Falls back to block-splitting when no dense rows emerge.
    """
    lines = _lines(words)
    if len(lines) < 3:
        return []
    anchors = _anchors(lines)
    if len(anchors) < _MIN_CELLS:
        return []
    cells = [_row_cells(ln, anchors) for ln in lines]
    ys = [sum((w["y0"] + w["y1"]) / 2 for w in ln) / len(ln) for ln in lines]
    seeds = [i for i, (_, pop) in enumerate(cells) if pop >= _ROW_MIN]
    if len(seeds) < 2:
        return extract_page_tables(words)  # no clear rows -> original block logic
    groups: dict[int, list[int]] = {s: [] for s in seeds}
    for i in range(len(lines)):
        groups[min(seeds, key=lambda s: abs(ys[i] - ys[s]))].append(i)
    rows: list[list[str]] = []
    for s in seeds:
        merged = [""] * len(anchors)
        for i in sorted(groups[s], key=lambda i: ys[i]):
            for k, v in enumerate(cells[i][0]):
                if v:
                    merged[k] = (merged[k] + " " + v).strip() if merged[k] else v
        rows.append(merged)
    rows = _trim(rows)
    return [rows] if len(rows) >= 2 else []


def extract_page_tables_regioned(words: list[dict], width: float) -> list[list[list[str]]]:
    """Split the page into side-by-side regions, then reconstruct each separately."""
    tables: list[list[list[str]]] = []
    for x0, x1 in _split_x_regions(words, width):
        region = [w for w in words if x0 <= (w["x0"] + w["x1"]) / 2 < x1]
        tables.extend(_region_table(region))
    return tables


def _pdfplumber_page_words(pdf, page: int) -> tuple[list[dict], float]:
    """Words for a 1-indexed page, shaped like the ground-truth word boxes."""
    pg = pdf.pages[page - 1]
    words = [
        {"t": w["text"], "x0": w["x0"], "x1": w["x1"], "y0": w["top"], "y1": w["bottom"]}
        for w in pg.extract_words(x_tolerance=1.5, use_text_flow=True, keep_blank_chars=False)
    ]
    return words, float(pg.width)


def _region_bbox(words: list[dict], x0: float, x1: float) -> list[float] | None:
    """Bounding box (pts) of the words inside one x-region, so that region's
    table is cropped tightly instead of letting numbers shared with a neighbour
    table balloon the box across the whole page."""
    reg = [w for w in words if x0 <= _cx(w) < x1]
    if not reg:
        return None
    return [min(w["x0"] for w in reg), min(w["y0"] for w in reg),
            max(w["x1"] for w in reg), max(w["y1"] for w in reg)]


# ---- pipeline stage ---------------------------------------------------------

STAGE = "det_tables"


def run(paths, force: bool = False, all_vision: bool = False, image_vision: bool = False) -> dict:
    """Replace OCR tables on suspect pages (text present, OCR numbers disagree)
    with deterministic reconstructions from the digital text layer. High-
    confidence and image-page OCR tables are left untouched.

    With ``all_vision=True``, EVERY table-bearing page that has a digital text
    layer is rebuilt through the Sonnet vision hybrid (structure from vision,
    numbers from the text layer) — for reports where the default extraction is
    messy on single tables too, not just N-up ones. Requires ``ANTHROPIC_API_KEY``.

    With ``image_vision=True``, only the *image-only* table pages — those a table
    was detected on but which carry no digital text layer (rasterised/scanned or
    rotated tables, e.g. a sideways "history of work" insert) — are transcribed
    with vision alone, rotated upright first. These results are **unverifiable**
    (no text layer to reconcile against) and verify flags them as such. The
    text-backed tables are left untouched. Requires ``ANTHROPIC_API_KEY``.
    """
    import csv

    import pdfplumber

    from . import ground_truth as gt
    from .util import mark_stage_done, read_json, write_json
    from .verify_tables import _csv_numbers, _numbers, _page_words

    gt_pages = gt.load(paths).get("pages", [])
    index = read_json(paths.root / "tables_index.json", default=[]) or []
    sections = read_json(paths.sections_json, default=[]) or []

    def page_section(page: int) -> dict:
        for s in sections:
            if s.get("page_start", 0) <= page <= s.get("page_end", 0):
                return s
        return {"number": None, "slug": "section"}

    def page_has_text(page: int) -> bool:
        return len([n for w in _page_words(gt_pages, page) for n in _numbers(w["t"])]) >= 20

    # Which pages to (re)build?
    if image_vision:
        # only image-only table pages (a table was found but there's no text layer
        # to reconcile against) — vision-transcribe them; result is unverifiable
        target_pages = {rec["page"] for rec in index if not page_has_text(rec["page"])}
    elif all_vision:
        # every table-bearing page that has a text layer (so numbers stay checkable)
        target_pages = {rec["page"] for rec in index if page_has_text(rec["page"])}
    else:
        # only pages whose OCR table disagrees with the independent text layer
        target_pages = set()
        for rec in index:
            if rec.get("method") != "mistral_ocr":
                continue
            page = rec["page"]
            if not page_has_text(page):
                continue  # image page -> can't do better deterministically
            csv_nums, _ = _csv_numbers(paths.root / rec["csv"])
            if not csv_nums:
                continue
            page_set = {n for w in _page_words(gt_pages, page) for n in _numbers(w["t"])}
            coverage = sum(1 for n in csv_nums if n in page_set) / len(csv_nums)
            if coverage < 0.95:
                target_pages.add(page)

    pdf = pdfplumber.open(paths.source_pdf) if paths.source_pdf.exists() else None

    from . import hybrid_tables

    use_hybrid = hybrid_tables.available()

    replaced_pages = 0
    hybrid_pages = 0
    new_records = list(index)
    for page in sorted(target_pages):
        tables = None
        method = "digital_text"
        bboxes = None
        crop_imgs = None

        if image_vision:
            # No text layer here: vision-transcribe the page (rotated upright) and
            # save the upright render as the human-check crop. Unverifiable by design.
            res = (hybrid_tables.vision_transcribe_page(paths.page_png(page).read_bytes(), paths.cache)
                   if use_hybrid and paths.page_png(page).exists() else None)
            if not res or not res[0]:
                continue  # leave the existing OCR table if vision is unavailable/failed
            rows_one, upright_png = res
            tables, method, bboxes, crop_imgs = [rows_one], "vision_unverified", [None], [upright_png]
        else:
            try:
                words, width = _pdfplumber_page_words(pdf, page) if pdf else (None, None)
            except Exception:
                words, width = None, None

            if words:
                regions = _split_x_regions(words, width)
                # Side-by-side (N-up) pages defeat the clustering: the columns jam and
                # numbers land under the wrong header. Use vision for the structure and
                # the text layer for the numbers — on N-up pages, or on every page when
                # all_vision is set (for reports the default extraction mangles).
                if use_hybrid and (all_vision or len(regions) >= 2) and paths.page_png(page).exists():
                    truth = {n for w in _page_words(gt_pages, page) for n in _numbers(w["t"])}
                    h = hybrid_tables.hybrid_page_tables(
                        paths.page_png(page).read_bytes(), regions, width, truth, paths.cache
                    )
                    if h:
                        tables, method = h, "vision_hybrid"
                        bboxes = [_region_bbox(words, x0, x1) for x0, x1 in regions]
                        hybrid_pages += 1
                if tables is None:
                    tables = extract_page_tables_regioned(words, width)
            else:  # fall back to the ground-truth (pdftotext) word boxes
                tables = extract_page_tables(_page_words(gt_pages, page))
        if not tables:
            continue  # leave OCR tables if we can't reconstruct
        if not bboxes or len(bboxes) != len(tables):
            bboxes = [None] * len(tables)
        if not crop_imgs or len(crop_imgs) != len(tables):
            crop_imgs = [None] * len(tables)
        # Remove every existing table artifact for this page (covers stale files
        # from a prior run whose segmentation differed), then drop its records.
        for f in paths.tables.glob(f"*__p{page:04d}.*"):
            f.unlink()
        new_records = [r for r in new_records if r["page"] != page]

        sec = page_section(page)
        slug = sec.get("slug", "section")
        for seq, (rows, bbox, crop_img) in enumerate(zip(tables, bboxes, crop_imgs), start=1):
            stem = f"{slug}_t{seq}__p{page:04d}"
            with open(paths.tables / f"{stem}.csv", "w", newline="") as f:
                csv.writer(f).writerows(rows)
            rec = {
                "id": stem,
                "section": sec.get("number"),
                "section_slug": slug,
                "page": page,
                "csv": f"tables/{stem}.csv",
                "n_rows": len(rows),
                "n_cols": max((len(r) for r in rows), default=0),
                "method": method,
            }
            if bbox:  # tight per-region crop box (vision_hybrid); else verify derives it
                rec["bbox"] = bbox
            if crop_img:  # upright vision render -> human-check crop (verify keeps it)
                (paths.tables / f"{stem}.crop.png").write_bytes(crop_img)
                rec["crop_png"] = f"tables/{stem}.crop.png"
            new_records.append(rec)
        replaced_pages += 1
        if image_vision:
            hybrid_pages += 1

    if pdf:
        pdf.close()

    new_records.sort(key=lambda r: (r["page"], r["id"]))
    write_json(paths.root / "tables_index.json", new_records)
    mode = "image_vision" if image_vision else ("all_vision" if all_vision else "suspect")
    info = {"target_pages": len(target_pages), "replaced_pages": replaced_pages,
            "hybrid_pages": hybrid_pages, "mode": mode}
    mark_stage_done(paths, STAGE, info)
    return info
