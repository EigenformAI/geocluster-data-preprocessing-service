"""Pure PDF table evidence; callers own parsing, rendering, dispatch, and selection.

Bounds use ``[x0, top, x1, bottom]`` in PDF points or an adapter record containing
``bbox``. Missing candidate bounds cannot establish a physical occurrence.
Pages, rows, and columns are one-based. Public helpers do not mutate inputs.
Results use JSON-serializable dictionaries and lists, except ``raw`` from
``validate_visual``, which retains the supplied response even when malformed.

Reuse one base word-occurrence list throughout a page's evidence. Occurrence
identity, not equal text, establishes correspondence. Keep original reads when
the helpers report uncertainty. Geometric tolerances are overridable heuristics
in points, not accuracy guarantees.
"""

import json
import math
import re
from copy import deepcopy
from itertools import pairwise
from statistics import median

__all__ = [
    "aggregate_tiles",
    "assess_geometry",
    "candidate_from_rows",
    "compare_candidates",
    "group_candidates",
    "plan_tiles",
    "propose_regions",
    "recover_geometry",
    "validate_visual",
    "word_occurrences",
]


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value, minimum=1):
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _box(bounds):
    if bounds is None:
        return None
    box = bounds.get("bbox") if isinstance(bounds, dict) else bounds
    if (not isinstance(box, (list, tuple)) or len(box) != 4 or not all(_finite(v) for v in box)
            or box[0] >= box[2] or box[1] >= box[3]):
        raise ValueError("bounds must be a finite positive rectangle")
    return list(box)


def _identity(id, page):
    if not isinstance(id, str) or not id or not _integer(page):
        raise ValueError("candidate identity needs a nonempty id and one-based page")


def word_occurrences(page: int, words: list[dict]) -> list[dict]:
    """Allocate stable page-local IDs in the base parser's occurrence order.

    Call once per page and reuse the list for every candidate and recovery step.
    Each word has ``id``, ``text``, and ``bbox``. Supplied IDs survive unchanged.
    IDs identify occurrences, not text tokens. Retokenized evidence needs a
    separate namespace.
    """
    if not _integer(page):
        raise ValueError("page must be a one-based integer")
    result = []
    for index, word in enumerate(words, 1):
        box = word.get("bbox")
        if box is None:
            box = [word["x0"], word["top"], word["x1"], word["bottom"]]
        result.append({"id": word.get("id", f"p{page:04d}_w{index:06d}"),
                       "text": word["text"], "bbox": _box(box)})
    _check_words(result)
    return result


def _check_words(words):
    ids = set()
    for word in words:
        if not isinstance(word.get("id"), str) or not word["id"] or word["id"] in ids:
            raise ValueError("base word occurrence IDs must be unique nonempty strings")
        if not isinstance(word.get("text"), str):
            raise TypeError("word text must be a string")
        _box(word.get("bbox"))
        if word.get("bbox") is None:
            raise ValueError("word bounds are required")
        ids.add(word["id"])


def _cell(row, column, text, *, bbox=None, row_span=1, column_span=1, reason=None):
    if text is None and reason is None:
        reason = "unavailable reading"
    if isinstance(text, str) and re.search(r"\[(?:unreadable|uncertain)(?:\]|:)", text, re.IGNORECASE):
        reason = reason or "preserved uncertainty marker"
    return {"row": row, "column": column, "text": text, "rowSpan": row_span,
            "columnSpan": column_span, "bbox": bbox, "wordIds": [], "unreadableReason": reason}


def _grid(row_count, column_count, cells):
    grid = [[{"state": "omitted"} for _ in range(column_count)] for _ in range(row_count)]
    for cell in cells:
        r, c, rs, cs = (cell[k] for k in ("row", "column", "rowSpan", "columnSpan"))
        if (not all(_integer(v) for v in (r, c, rs, cs)) or r + rs - 1 > row_count
                or c + cs - 1 > column_count):
            raise ValueError("cell indices or spans are outside the logical grid")
        if cell["text"] is not None and not isinstance(cell["text"], str):
            raise ValueError("cell text must be a string or null")
        state = "unreadable" if cell["unreadableReason"] or cell["text"] is None else "empty" if cell["text"] == "" else "text"
        for row in range(r - 1, r + rs - 1):
            for col in range(c - 1, c + cs - 1):
                if grid[row][col]["state"] != "omitted":
                    raise ValueError("duplicate anchors or overlapping spans")
                grid[row][col] = {"state": state if (row, col) == (r - 1, c - 1) else "covered",
                                  "anchor": [r, c]}
    return grid


def _rows(row_count, column_count, cells):
    rows = [[None for _ in range(column_count)] for _ in range(row_count)]
    for cell in cells:
        rows[cell["row"] - 1][cell["column"] - 1] = cell["text"]
    return rows


def _candidate(id, page, bounds, method, row_count, column_count, cells, *, present=True):
    """Build the shared candidate views from cell anchors and spans.

    ``logicalRows`` contains anchor text, with None for covered, omitted, or
    unavailable readings. ``grid`` distinguishes those meanings. Anchor and
    covered positions have a state and one-based ``anchor: [row, column]``.
    Omitted positions have only their state. An empty cell is ``""``, never a
    missing reading. ``rows`` initially copies the logical view. Raw-row candidates
    replace it with the original evidence. Export the logical view with its grid.

    Native and visual axis intervals describe cells. Recovered row bands may
    describe ink, and inferred columns may describe alignment. These kinds
    determine how comparison interprets the intervals.
    """
    _identity(id, page)
    _box(bounds)
    logical_rows = _rows(row_count, column_count, cells)
    return {"id": id, "page": page, "bounds": deepcopy(bounds), "method": method,
            "tablePresent": present, "rowCount": row_count, "columnCount": column_count,
            "rows": deepcopy(logical_rows), "logicalRows": logical_rows, "cells": cells,
            "grid": _grid(row_count, column_count, cells), "rowBands": [], "columnIntervals": [],
            "rowBandKind": "cell", "columnIntervalKind": "cell",
            "words": [], "unassignedWordIds": [], "conflictingWordIds": [], "issues": [],
            "outsideWordIds": [], "model": None, "parentId": None}


def _axis_intervals(rectangles, count, axis):
    edges = sorted({b[i] for b in rectangles for i in (axis, axis + 2)})
    return [[a, b] for a, b in pairwise(edges)] if len(edges) == count + 1 else []


def _native_intervals(rectangles, count, axis):
    intervals = _axis_intervals(list(rectangles.values()), count, axis)
    if intervals:
        return intervals
    # Distinct native columns can have real gutters. Do not extend rectangles
    # through them just because a single edge array cannot describe the layout.
    buckets = [[box for position, box in rectangles.items() if position[1 if axis == 0 else 0] == index]
               for index in range(1, count + 1)]
    if not buckets or any(not bucket for bucket in buckets):
        return []
    starts = [min(box[axis] for box in bucket) for bucket in buckets]
    if any(a >= b for a, b in pairwise(starts)):
        return []
    for index, bucket in enumerate(buckets):
        end = min(box[axis + 2] for box in bucket)
        if index + 1 < count:
            end = min(end, starts[index + 1])
        intervals.append([starts[index], end])
    return intervals


def _contains(box, word_box, tolerance=0):
    # Require the entire occurrence, not just its centre, to avoid gutter leakage.
    return (box[0] - tolerance <= word_box[0] and word_box[2] <= box[2] + tolerance
            and box[1] - tolerance <= word_box[1] and word_box[3] <= box[3] + tolerance)


def _assign(candidate, words):
    _check_words(words)
    candidate["words"] = deepcopy(words)
    candidate["unassignedWordIds"] = []
    candidate["conflictingWordIds"] = []
    candidate["outsideWordIds"] = []
    region = _box(candidate["bounds"])
    for cell in candidate["cells"]:
        cell["wordIds"] = []
    for word in words:
        if region is not None and _intersection(region, word["bbox"]) == 0:
            candidate["outsideWordIds"].append(word["id"])
            continue
        matches = [cell for cell in candidate["cells"] if cell["bbox"] is not None
                   and _contains(cell["bbox"], word["bbox"])]
        if len(matches) == 1:
            matches[0]["wordIds"].append(word["id"])
        else:
            candidate["unassignedWordIds"].append(word["id"])
            if len(matches) > 1:
                candidate["conflictingWordIds"].append(word["id"])


def candidate_from_rows(id: str, page: int, bounds, rows: list[list], method: str,
                        cell_bounds=None, words=None) -> dict:
    """Retain raw rows and native rectangles, including explicit span coverage.

    ``cell_bounds`` follows the raw row and column matrix. Shared rectangles or
    rectangles covering later positions represent spans, not repeated text.
    For native extraction, a blank ruled cell is ``""``. A null without span
    coverage stays unavailable. OCR nulls also stay unavailable.
    ``outsideWordIds`` separates unrelated page words from intersecting words
    that cannot be assigned to a cell.
    """
    if not isinstance(rows, (list, tuple)) or any(not isinstance(r, (list, tuple)) for r in rows):
        raise ValueError("rows must be arrays of cell arrays")
    if any(v is not None and not isinstance(v, str) for row in rows for v in row):
        raise ValueError("cell text must be a string or null")
    if not isinstance(method, str) or not method:
        raise ValueError("method must be a nonempty string")
    nr, nc = len(rows), max(map(len, rows), default=0)
    rectangles = {}
    if cell_bounds is not None:
        if len(cell_bounds) != nr or any(len(row) > nc for row in cell_bounds):
            raise ValueError("cell bounds must follow the raw row/column matrix")
        rectangles = {(r + 1, c + 1): _box(b) for r, row in enumerate(cell_bounds)
                      for c, b in enumerate(row) if b is not None}
    row_bands, columns = _native_intervals(rectangles, nr, 1), _native_intervals(rectangles, nc, 0)
    cells, covered, issues = [], set(), []
    for r, row in enumerate(rows, 1):
        for c, value in enumerate(row, 1):
            if (r, c) in covered:
                continue
            box = rectangles.get((r, c))
            rs = cs = 1
            if box is not None:
                while c + cs <= nc and rectangles.get((r, c + cs)) == box:
                    cs += 1
                while r + rs <= nr and all(rectangles.get((r + rs, cc)) == box for cc in range(c, c + cs)):
                    rs += 1
                if row_bands and box[1] == row_bands[r - 1][0]:
                    rs = max(rs, sum(box[1] <= top and bottom <= box[3] for top, bottom in row_bands[r - 1:]))
                if columns and box[0] == columns[c - 1][0]:
                    cs = max(cs, sum(box[0] <= left and right <= box[2] for left, right in columns[c - 1:]))
                rs, cs = max(1, rs), max(1, cs)
                # Geometry may cover only None/shared-rectangle positions. Otherwise
                # retain competing cells and let word conflicts expose the overlap.
                positions = [(rr, cc) for rr in range(r, r + rs) for cc in range(c, c + cs)
                             if (rr, cc) != (r, c)]
                if any(rr > nr or cc > nc or (cc <= len(rows[rr - 1])
                       and rows[rr - 1][cc - 1] is not None and rectangles.get((rr, cc)) != box)
                       for rr, cc in positions):
                    rs = cs = 1
                else:
                    covered.update(positions)
                    for rr, cc in positions:
                        if cc <= len(rows[rr - 1]) and rows[rr - 1][cc - 1] not in (None, value):
                            issues.append({"kind": "native-span-reading-conflict", "row": rr, "column": cc, "anchor": [r, c]})
            cells.append(_cell(r, c, value, bbox=box, row_span=rs, column_span=cs))
    result = _candidate(id, page, bounds, method, nr, nc, cells)
    result["rows"] = deepcopy([list(row) for row in rows])
    result["issues"] = issues
    result["rowBands"], result["columnIntervals"] = row_bands, columns
    _assign(result, words or [])
    return result


def _edges(value, count):
    return (isinstance(value, list) and len(value) == count + 1 and all(_finite(v) and 0 <= v <= 1000 for v in value)
            and all(a < b for a, b in pairwise(value)))


def validate_visual(response, id: str, page: int, bounds, model=None, *,
                    max_rows: int | None = None, max_columns: int | None = None,
                    max_grid_cells: int | None = None) -> dict:
    """Validate transcript independently of optional, estimated image geometry.

    Accept a decoded response or JSON string. Invalid transcripts return
    ``valid=False`` and no candidate, retaining ``raw`` and ``errors``. Missing
    readings remain omitted. Invalid auxiliary edges go to ``discardedEdges``
    without invalidating the transcript. Absent edges stay absent.
    Edge mapping assumes an unrotated image of exactly the supplied bounds.
    For rotated or padded images, callers must remap intervals through the
    actual inverse image transform before comparison or aggregation.

    Limits are strict and checked before dense allocation. Zero means exhausted.
    None means unbounded. Invalid limit configuration raises ValueError.
    ``resourceLimit`` is None unless a cap is exceeded, when it records
    ``{name: "rows" | "columns" | "gridCells", requested, limit}``.
    Callers must map that outcome to RESOURCE_LIMIT, supply remaining axis
    budgets, and bound grid positions from working memory. Axis caps alone do
    not provide a practical memory bound. Uncapped allocation can exhaust it.
    """
    _identity(id, page)
    box = _box(bounds)
    for name, limit in (("max_rows", max_rows), ("max_columns", max_columns), ("max_grid_cells", max_grid_cells)):
        if limit is not None and not _integer(limit, 0):
            raise ValueError(f"{name} must be a nonnegative integer or None")
    result = {"valid": False, "candidate": None, "raw": deepcopy(response), "errors": [],
              "discardedEdges": [], "resourceLimit": None}
    try:
        data = json.loads(response) if isinstance(response, str) else response
        if not isinstance(data, dict) or not isinstance(data.get("tablePresent"), bool):
            raise TypeError("tablePresent must be a boolean")
        nr, nc = data.get("rowCount"), data.get("columnCount")
        if not _integer(nr, 0) or not _integer(nc, 0):
            raise ValueError("rowCount and columnCount must be nonnegative integers")
        present = data["tablePresent"]
        if present and (nr == 0 or nc == 0):
            raise ValueError("a present table must have positive axis counts")
        if not isinstance(data.get("cells"), list):
            raise TypeError("cells must be an array")
        if not present and (nr or nc or data["cells"]):
            raise ValueError("a non-table response must have zero counts and no cells")
        for name, requested, limit in (("rows", nr, max_rows), ("columns", nc, max_columns),
                                       ("gridCells", nr * nc, max_grid_cells)):
            if limit is not None and requested > limit:
                result["resourceLimit"] = {"name": name, "requested": requested, "limit": limit}
                result["errors"].append(f"Visual table {name} count {requested} exceeds configured limit {limit}.")
                return result
        cells = []
        for raw_cell in data["cells"]:
            if not isinstance(raw_cell, dict) or not {"row", "column", "text"} <= raw_cell.keys():
                raise ValueError("every cell needs row, column and text")
            reason = raw_cell.get("unreadableReason")
            if reason is not None and (not isinstance(reason, str) or not reason.strip()):
                raise ValueError("unreadableReason must be a nonempty string")
            cells.append(_cell(raw_cell["row"], raw_cell["column"], raw_cell["text"],
                               row_span=raw_cell.get("rowSpan", 1), column_span=raw_cell.get("columnSpan", 1), reason=reason))
        candidate = _candidate(id, page, bounds, "visual", nr, nc, cells, present=present)
        candidate["model"] = deepcopy(model)
        for key, count, axis, target in (("rowEdges", nr, 1, "rowBands"), ("columnEdges", nc, 0, "columnIntervals")):
            if key not in data:
                continue
            if not _edges(data[key], count):
                result["discardedEdges"].append({"axis": key, "reason": "invalid count, range or monotonic positions"})
                continue
            candidate[key] = list(data[key])
            if box is not None:
                mapped = [box[axis] + value / 1000 * (box[axis + 2] - box[axis]) for value in data[key]]
                candidate[target] = [[a, b] for a, b in pairwise(mapped)]
        candidate["issues"] = deepcopy(result["discardedEdges"])
        result.update(valid=True, candidate=candidate)
    except (ValueError, TypeError, KeyError) as exc:
        result["errors"].append(str(exc))
    return result


def _baselines(words):
    """Cluster bottoms using each occurrence's local font height, not line text."""
    bands = []
    for word in sorted(words, key=lambda w: (w["bbox"][3], w["bbox"][0], w["id"])):
        box = word["bbox"]
        matches = [band for band in bands if abs(median(w["bbox"][3] for w in band) - box[3])
                   <= min(median(w["bbox"][3] - w["bbox"][1] for w in band), box[3] - box[1]) * .35]
        if len(matches) == 1:
            matches[0].append(word)
        else:
            bands.append([word])
    return sorted(bands, key=lambda band: min(w["bbox"][1] for w in band))


def _band_box(band):
    return [min(w["bbox"][1] for w in band), max(w["bbox"][3] for w in band)]


def _text_runs(band, gap_factor=.65):
    """Group word geometry for alignment, retaining the original occurrence IDs.

    Runs are not a new occurrence list. Their tuple keys are used only while
    proposing boundaries; recovered cells always reference the base words.
    """
    groups = []
    for word in sorted(band, key=lambda w: (w["bbox"][0], w["id"])):
        if groups:
            previous = groups[-1][-1]
            height = min(previous["bbox"][3] - previous["bbox"][1], word["bbox"][3] - word["bbox"][1])
            gap = word["bbox"][0] - previous["bbox"][2]
            if 0 <= gap <= height * gap_factor:
                groups[-1].append(word)
                continue
        groups.append([word])
    return [{"id": tuple(w["id"] for w in group), "words": group,
             "bbox": [min(w["bbox"][0] for w in group), min(w["bbox"][1] for w in group),
                      max(w["bbox"][2] for w in group), max(w["bbox"][3] for w in group)]} for group in groups]


def _alignment_lanes(bands, tolerance, min_rows):
    # Cluster all three alignments, choosing maximal nonoverlapping word lanes.
    # Continuation lines cannot manufacture more support than complete row bands.
    bands = [band for band in bands if len(band) >= 2]
    clusters = []
    for axis in ("left", "centre", "right"):
        aligned = []
        for r, band in enumerate(bands):
            for word in band:
                b = word["bbox"]
                x = b[0] if axis == "left" else b[2] if axis == "right" else (b[0] + b[2]) / 2
                matches = [lane for lane in aligned if abs(median(item[0] for item in lane) - x) <= tolerance]
                if matches:
                    matches[0].append((x, r, word))
                else:
                    aligned.append([(x, r, word)])
        for lane in aligned:
            if len({r for _, r, _ in lane}) >= min_rows:
                clusters.append({"ids": {w["id"] for _, _, w in lane},
                                 "left": min(w["bbox"][0] for _, _, w in lane),
                                 "right": max(w["bbox"][2] for _, _, w in lane), "rows": len({r for _, r, _ in lane})})
    lanes = []
    minimum_support = max(min_rows, max((c["rows"] for c in clusters), default=0) / 2)
    for cluster in sorted(clusters, key=lambda c: (-c["rows"], -len(c["ids"]), c["left"])):
        if cluster["rows"] < minimum_support:
            continue
        if any(cluster["ids"] & lane["ids"] or min(cluster["right"], lane["right"]) > max(cluster["left"], lane["left"])
               for lane in lanes):
            continue
        lanes.append(cluster)
    lanes.sort(key=lambda c: c["left"])
    return lanes


def _aligned_columns(bands, tolerance, min_rows):
    lanes = _alignment_lanes(bands, tolerance, min_rows)
    if not lanes:
        return []
    cuts = [(a["right"] + b["left"]) / 2 for a, b in pairwise(lanes)]
    edges = [lanes[0]["left"], *cuts, lanes[-1]["right"]]
    return [[a, b] for a, b in pairwise(edges)]


def _region_words(candidate, words):
    box = _box(candidate["bounds"])
    # A parser's outer rule can bisect glyph height too. Keep the whole word
    # when its centre belongs to the region, rather than clipping its spelling.
    return [w for w in words if box is not None
            and box[0] <= (w["bbox"][0] + w["bbox"][2]) / 2 <= box[2]
            and box[1] <= (w["bbox"][1] + w["bbox"][3]) / 2 <= box[3]]


def _word_identity(words):
    return {w["id"]: (w["text"], tuple(w["bbox"])) for w in words}


def _column_plan(candidate, bands, tolerance=2, gap_factor=.65):
    native = candidate["columnIntervals"]
    runs = [_text_runs(band, gap_factor) for band in bands]
    dense = [band for band in runs if len(band) >= 2]
    lanes = _alignment_lanes(dense, tolerance, max(2, min(3, len(dense))))
    words = [w for band in bands for w in band]
    spans = [c for c in candidate["cells"] if c["bbox"] is not None and c["columnSpan"] > 1]
    violated = {edge for interval in native for edge in interval
                if any(w["bbox"][0] < edge < w["bbox"][2]
                       and not any(_contains(c["bbox"], w["bbox"]) for c in spans) for w in words)}
    height = median(w["bbox"][3] - w["bbox"][1] for w in words) if words else 0
    redundant = any(right - left < height * 2.5 and not any(left <= w["bbox"][0] and w["bbox"][2] <= right for w in words)
                    for left, right in native)
    reasons = []
    if violated:
        reasons.append("word-crosses-column")
    if native and (len(lanes) > len(native)
                   or any(sum(left <= lane["left"] and lane["right"] <= right for lane in lanes) >= 2 for left, right in native)):
        reasons.append("repeated-subcolumns")
    if native and redundant and 2 <= len(lanes) < len(native):
        reasons.append("redundant-column-edges")
    intervals = deepcopy(native)
    kind = candidate["columnIntervalKind"]
    if len(lanes) >= 2 and (not native or reasons):
        for left, right in native:
            if any(min(right, lane["right"]) > max(left, lane["left"]) for lane in lanes):
                continue
            retained = [w for w in words if left <= w["bbox"][0] and w["bbox"][2] <= right]
            declared = any(c["bbox"] is not None and c["columnSpan"] == 1
                           and abs(c["bbox"][0] - left) <= tolerance and abs(c["bbox"][2] - right) <= tolerance
                           for c in candidate["cells"])
            # A real spanning label need not repeat on every row. Likewise a
            # wide declared empty field survives refinement in another lane.
            if declared and (retained or right - left >= height * 2.5):
                lanes.append({"left": min(w["bbox"][0] for w in retained) if retained else left,
                              "right": max(w["bbox"][2] for w in retained) if retained else right})
        lanes.sort(key=lambda lane: lane["left"])
        cuts = []
        for a, b in pairwise(lanes):
            midpoint = (a["right"] + b["left"]) / 2
            safe = [edge for interval in native for edge in interval if a["right"] <= edge <= b["left"]]
            cuts.append(min(safe, key=lambda edge: abs(edge - midpoint)) if safe else midpoint)
        box = _box(candidate["bounds"])
        edges = [box[0] if box else lanes[0]["left"], *cuts, box[2] if box else lanes[-1]["right"]]
        intervals, kind = [[a, b] for a, b in pairwise(edges)], "alignment"
    elif violated and native:
        # A single band can still disprove a cut through a base word. Removing
        # only that cut needs no invented repetition or text-fragment pairing.
        intervals = []
        for left, right in native:
            if intervals and left in violated and intervals[-1][1] == left:
                intervals[-1][1] = right
            else:
                intervals.append([left, right])
        kind = "alignment"
    return {"intervals": intervals, "kind": kind, "reasons": reasons,
            "changed": intervals != native, "runs": runs}


def assess_geometry(candidate: dict, words=None, *, alignment_tolerance: float = 2,
                    word_gap_factor: float = .65) -> dict:
    """Record suspicion without treating multiline spellings as proof of rows.

    ``suspect`` summarizes the recorded ``triggers``, not proof of a bad table.
    Supplied words must retain the candidate's base occurrence identities.
    """
    words = candidate["words"] if words is None else words
    _check_words(words)
    if candidate["words"] and _word_identity(candidate["words"]) != _word_identity(words):
        raise ValueError("geometry assignments must use the same base occurrence list")
    if not _finite(alignment_tolerance) or alignment_tolerance < 0 or not _finite(word_gap_factor) or word_gap_factor < 0:
        raise ValueError("alignment tolerances must be finite and nonnegative")
    bands = _baselines(_region_words(candidate, words))
    triggers = []
    multiline = [cell for cell in candidate["cells"] if isinstance(cell["text"], str) and "\n" in cell["text"]]
    if multiline:
        triggers.append("multiline-lanes")
    if candidate["bounds"] is None:
        triggers.append("missing-bounds")
    if len(bands) != candidate["rowCount"]:
        triggers.append("incompatible-baseline-row-count")
    if any(p["state"] == "omitted" for row in candidate["grid"] for p in row):
        triggers.append("ragged-rows")
    if any(p["state"] == "unreadable" for row in candidate["grid"] for p in row):
        triggers.append("unreadable-cells")
    assigned = deepcopy(candidate)
    _assign(assigned, words)
    if assigned["unassignedWordIds"]:
        triggers.append("unassigned-words")
    if assigned["conflictingWordIds"]:
        triggers.append("conflicting-assignments")
    plan = _column_plan(candidate, bands, alignment_tolerance, word_gap_factor)
    columns = plan["intervals"]
    triggers.extend(plan["reasons"])
    if any(sum(any(_contains([left, -math.inf, right, math.inf], w["bbox"]) for w in band)
               for left, right in columns) == 1 for band in bands) and len(bands) > candidate["rowCount"]:
        triggers.append("sparse-row-absorption")
    return {"suspect": bool(triggers), "triggers": triggers, "baselineBands": [_band_box(b) for b in bands],
            "columnIntervals": deepcopy(columns), "columnIntervalKind": plan["kind"],
            "unassignedWordIds": assigned["unassignedWordIds"],
            "conflictingWordIds": assigned["conflictingWordIds"]}


def _word_text(words):
    return "\n".join(" ".join(w["text"] for w in sorted(band, key=lambda w: (w["bbox"][0], w["id"])))
                     for band in _baselines(words))


def _occupied(band, columns):
    return {c for c, (left, right) in enumerate(columns, 1)
            if any(left <= w["bbox"][0] and w["bbox"][2] <= right for w in band)}


def _continuation_scope(original, previous, band, lane, columns):
    """Require row-extent support, not just a missing repeated group label.

    A genuine first-column rowspan does not make each following numeric row a
    continuation. Other outer anchors, the cell itself, or redundant padding
    strips must support the same row extent across the proposed continuation.
    """
    centre = median((w["bbox"][1] + w["bbox"][3]) / 2 for w in band)
    first_left, first_right = columns[0]
    key_ids = {w["id"] for w in previous if first_left <= w["bbox"][0] and w["bbox"][2] <= first_right}
    key_scopes = [c["bbox"] for c in original["cells"] if c["bbox"] is not None and key_ids & set(c["wordIds"])]
    if not any(scope[1] <= centre <= scope[3] for scope in key_scopes):
        return False
    left, right = columns[lane - 1]
    field_ids = {w["id"] for w in previous if left <= w["bbox"][0] and w["bbox"][2] <= right}
    last_left, last_right = columns[-1]
    last_ids = {w["id"] for w in previous if last_left <= w["bbox"][0] and w["bbox"][2] <= last_right}
    for cell in original["cells"]:
        box = cell["bbox"]
        if box is None or not box[1] <= centre <= box[3]:
            continue
        ids = set(cell["wordIds"])
        if field_ids & ids or (lane != len(columns) and last_ids & ids):
            return True
        if (not ids and cell["text"] == "" and cell["rowSpan"] > 1
                and left <= box[0] < box[2] <= right and box[2] - box[0] < (right - left) / 4):
            return True
    return False


def _restore_spans(recovered, original, words):
    """Project source spans onto the new grid only when they do not absorb other text."""
    by_id = {w["id"]: w for w in words}
    for source in original["cells"]:
        if source["bbox"] is None or source["rowSpan"] == source["columnSpan"] == 1:
            continue
        box = source["bbox"]
        rows = [r for r, (top, bottom) in enumerate(recovered["rowBands"], 1) if box[1] <= (top + bottom) / 2 <= box[3]]
        cols = [c for c, (left, right) in enumerate(recovered["columnIntervals"], 1) if box[0] <= (left + right) / 2 <= box[2]]
        if not rows or not cols or len(rows) == len(cols) == 1:
            continue
        positions = {(r, c) for r in rows for c in cols}
        cells = [cell for cell in recovered["cells"] if (cell["row"], cell["column"]) in positions]
        if len(cells) != len(positions) or any(c["rowSpan"] > 1 or c["columnSpan"] > 1 for c in cells):
            continue
        source_ids = set(source["wordIds"])
        if any(set(cell["wordIds"]) - source_ids for cell in cells):
            continue
        anchor = next(c for c in cells if c["row"] == rows[0] and c["column"] == cols[0])
        new_box = [recovered["columnIntervals"][cols[0] - 1][0], recovered["rowBands"][rows[0] - 1][0],
                   recovered["columnIntervals"][cols[-1] - 1][1], recovered["rowBands"][rows[-1] - 1][1]]
        if any(not _contains(new_box, by_id[id]["bbox"]) for id in source_ids):
            continue
        anchor.update(rowSpan=len(rows), columnSpan=len(cols), bbox=new_box,
                      text=_word_text([by_id[id] for id in source["wordIds"]]) if source_ids else source["text"],
                      unreadableReason=source["unreadableReason"])
        recovered["cells"] = [c for c in recovered["cells"] if (c["row"], c["column"]) not in positions or c is anchor]
    _assign(recovered, words)
    for source in original["cells"]:
        if source["bbox"] is None or not source["unreadableReason"] or source["wordIds"]:
            continue
        for cell in recovered["cells"]:
            box = cell["bbox"]
            if (box is not None and not cell["wordIds"]
                    and source["bbox"][0] <= (box[0] + box[2]) / 2 <= source["bbox"][2]
                    and source["bbox"][1] <= (box[1] + box[3]) / 2 <= source["bbox"][3]):
                # No base words supplied a new reading. A geometrically placed
                # unavailable source value remains unavailable, not empty.
                cell.update(text=source["text"], unreadableReason=source["unreadableReason"])
    recovered["grid"] = _grid(recovered["rowCount"], recovered["columnCount"], recovered["cells"])
    recovered["logicalRows"] = _rows(recovered["rowCount"], recovered["columnCount"], recovered["cells"])
    recovered["rows"] = deepcopy(recovered["logicalRows"])


def recover_geometry(candidate: dict, words=None, id: str | None = None, *,
                     alignment_tolerance: float = 2, word_gap_factor: float = .65) -> dict:
    """Generate a positioned alternative while retaining uncertain evidence.

    The result pairs a candidate with ``state``: unchanged, recovered, or
    unresolved. ``competingRowBands`` retains alternative positions when present.
    Recovery uses native cells, whole words, alignment, and local baselines,
    never newline splitting or retokenization. Close single-lane continuation
    needs multiline-cell evidence or repeated outer anchors with source-row
    extent support. Doubtful continuation remains a competing band.
    Source spans cannot absorb unrelated word IDs. Known wordless rows cannot
    disappear. A placed unavailable field stays unavailable without new words.
    """
    words = candidate["words"] if words is None else words
    assessment = assess_geometry(candidate, words, alignment_tolerance=alignment_tolerance, word_gap_factor=word_gap_factor)
    original = deepcopy(candidate)
    _assign(original, words)
    result = {"state": "unchanged", "candidate": original, "triggers": assessment["triggers"],
              "unassignedWordIds": original["unassignedWordIds"], "conflictingWordIds": original["conflictingWordIds"],
              "competingRowBands": []}
    region_words = _region_words(candidate, words)
    bands = _baselines(region_words)
    columns = assessment["columnIntervals"]
    layout_changed = columns != candidate["columnIntervals"]
    if original["conflictingWordIds"] and not layout_changed:
        result["state"] = "unresolved"
        return result
    # Wordless declared rows cannot be deleted by a word-only interpretation.
    if candidate["rowBands"] and len(bands) < candidate["rowCount"] and not layout_changed:
        return result
    if not bands or not columns:
        if assessment["suspect"]:
            result["state"] = "unresolved"
        return result
    groups, ambiguous, supported_continuation = [], False, False
    occupancy = [_occupied(band, columns) for band in bands]
    anchor_rows = sum(1 in occupied and len(occupied) >= 2 for occupied in occupancy)
    for band_index, band in enumerate(bands):
        top, bottom = _band_box(band)
        occupied = occupancy[band_index]
        continuation = False
        height = median(w["bbox"][3] - w["bbox"][1] for w in band)
        distance = (median(w["bbox"][3] for w in band) - median(w["bbox"][3] for w in bands[band_index - 1])) if groups else 0
        # Repeated first-column row anchors and populated columns outside a
        # continuation lane are stronger than background/ruling strip edges.
        # A sparse row occupying the anchor lane remains a row.
        if (groups and anchor_rows >= 2 and len(occupied) == 1 and 1 not in occupied
                and _occupied(groups[-1], columns) >= occupied | {1}
                and distance <= height * 1.6
                and _continuation_scope(original, groups[-1], band, next(iter(occupied)), columns)):
            continuation = True
            supported_continuation = True
        if groups and len(occupied) == 1:
            previous = groups[-1]
            height = median(w["bbox"][3] - w["bbox"][1] for w in previous + band)
            distance = median(w["bbox"][3] for w in band) - median(w["bbox"][3] for w in bands[band_index - 1])
            lane = next(iter(occupied))
            native_cells = [cell for cell in candidate["cells"] if cell["column"] == lane
                            and cell["bbox"] is not None and all(_contains(cell["bbox"], w["bbox"]) for w in previous + band
                                                               if columns[lane - 1][0] <= w["bbox"][0] < columns[lane - 1][1])]
            # A physical ruled boundary explicitly separates even tightly spaced rows.
            crosses_rule = bool(candidate["rowBands"] and any(_band_box(previous)[1] <= edge <= top
                                                               for _, edge in candidate["rowBands"][:-1]))
            multiline = any(isinstance(cell["text"], str) and "\n" in cell["text"] for cell in native_cells)
            # Uniformly tight bands do not distinguish a sparse physical row
            # from wrapped text. Continuation spacing must be locally distinct
            # from the next row's spacing, or end within its native ruled cell.
            next_distance = (median(w["bbox"][3] for w in bands[band_index + 1])
                             - median(w["bbox"][3] for w in band)) if band_index + 1 < len(bands) else None
            spacing_support = next_distance is None or next_distance > height * 1.5
            continuation = continuation or (distance <= height * 1.5 and multiline and not crosses_rule and spacing_support)
            if not crosses_rule and not continuation and distance < height * 2:
                ambiguous = True
        if continuation:
            groups[-1].extend(band)
        else:
            groups.append(list(band))
    physical_bands = [_band_box(group) for group in groups]
    if len(groups) == candidate["rowCount"] and not ambiguous and not layout_changed:
        return result
    unplaced_rows = any(all(p["state"] in {"empty", "covered", "unreadable", "omitted"} for p in row)
                        for row in candidate["grid"])
    unexplained_spans = (not layout_changed and not supported_continuation and len(bands) <= candidate["rowCount"]
                         and any(c["rowSpan"] > 1 or c["columnSpan"] > 1 for c in candidate["cells"]))
    if (len(groups) != candidate["rowCount"] and unplaced_rows) or unexplained_spans:
        result["state"] = "unresolved"
        result["competingRowBands"] = assessment["baselineBands"]
        return result
    cells = []
    for r, (group, (top, bottom)) in enumerate(zip(groups, physical_bands), 1):
        region = _box(candidate["bounds"])
        wide_label = (len(_text_runs(group, word_gap_factor)) == 1 and region is not None
                      and any(c["bbox"] is not None and c["columnSpan"] > 1
                              and c["bbox"][2] - c["bbox"][0] >= (region[2] - region[0]) * .75
                              and c["bbox"][1] <= (top + bottom) / 2 <= c["bbox"][3] for c in candidate["cells"]))
        if wide_label:
            cells.append(_cell(r, 1, _word_text(group), bbox=[columns[0][0], top, columns[-1][1], bottom], column_span=len(columns)))
            continue
        for c, (left, right) in enumerate(columns, 1):
            box = [left, top, right, bottom]
            assigned = [w for w in group if _contains(box, w["bbox"])]
            cells.append(_cell(r, c, _word_text(assigned), bbox=box))
    recovered = _candidate(id or candidate["id"] + ":geometry", candidate["page"], candidate["bounds"],
                           "geometry", len(groups), len(columns), cells)
    recovered["parentId"] = candidate["id"]
    recovered["rowBands"], recovered["columnIntervals"] = physical_bands, deepcopy(columns)
    recovered["rowBandKind"] = "ink"
    recovered["columnIntervalKind"] = assessment["columnIntervalKind"]
    _assign(recovered, words)
    _restore_spans(recovered, original, words)
    unresolved = ambiguous or bool(recovered["unassignedWordIds"] and any(w["id"] in recovered["unassignedWordIds"] for w in region_words))
    result.update(state="unresolved" if unresolved else "recovered", candidate=recovered,
                  unassignedWordIds=recovered["unassignedWordIds"], conflictingWordIds=recovered["conflictingWordIds"],
                  competingRowBands=assessment["baselineBands"] if unresolved else [])
    return result


def _normalize(text, numeric):
    trimmed = text.strip()
    return trimmed if numeric else " ".join(trimmed.split())


def _intervals_equal(a, b, tolerance):
    return len(a) == len(b) and all(abs(x - y) <= tolerance for aa, bb in zip(a, b) for x, y in zip(aa, bb))


def _axis_evidence(candidate, key):
    kind = candidate["rowBandKind" if key == "rowBands" else "columnIntervalKind"]
    if kind != "alignment":
        return candidate[key], kind
    base = {w["id"]: w for w in candidate["words"]}
    extents = []
    for column in range(1, candidate["columnCount"] + 1):
        boxes = [base[id]["bbox"] for cell in candidate["cells"] if cell["column"] == column
                 for id in cell["wordIds"] if id in base]
        if not boxes:
            return [], "ink"
        extents.append([min(b[0] for b in boxes), max(b[2] for b in boxes)])
    return extents, "ink"


def _axis_agrees(a, b, key, tolerance):
    ia, ka = _axis_evidence(a, key)
    ib, kb = _axis_evidence(b, key)
    if len(ia) != len(ib) or not ia:
        return False
    if ka == kb:
        return _intervals_equal(ia, ib, tolerance)
    ink, cells = (ia, ib) if ka == "ink" else (ib, ia)
    for index, (top, bottom) in enumerate(ink):
        matches = [i for i, (start, end) in enumerate(cells) if start - tolerance <= top and bottom <= end + tolerance]
        if matches != [index]:
            return False
    return True


def compare_candidates(a: dict, b: dict, *, geometry_tolerance: float = 2) -> dict:
    """Compare occurrence structure before cell spelling, never sets of text.

    ``basis`` is positioned-grid when both reads have row bands, ordered-grid
    otherwise. Ink must fit unambiguously inside the corresponding visual cell.
    Inferred columns use assigned word extents, not invented grid lines.
    Unicode numeric characters permit outer trim only. Prose permits whitespace
    normalization after structural correspondence. ``normalizations`` records it.
    ``nativeBacking`` records per-cell word IDs and occurrence counts, not
    page-wide numeric presence. Agreement is consistency, not source truth.
    """
    if not _finite(geometry_tolerance) or geometry_tolerance < 0:
        raise ValueError("geometry_tolerance must be finite and nonnegative")
    differences, normalizations, backing = [], [], []
    def differ(kind, **detail):
        differences.append({"kind": kind, **detail})
    if a["page"] != b["page"]:
        differ("page")
    if a["words"] and b["words"] and _word_identity(a["words"]) != _word_identity(b["words"]):
        differ("base-occurrence-list")
    if a["tablePresent"] != b["tablePresent"] or not a["tablePresent"]:
        differ("table-presence")
    for key in ("rowCount", "columnCount"):
        if a[key] != b[key]:
            differ(key, left=a[key], right=b[key])
    boxes = _box(a["bounds"]), _box(b["bounds"])
    if any(box is None for box in boxes):
        differ("missing-bounds")
    elif any(abs(x - y) > geometry_tolerance for x, y in zip(*boxes)):
        differ("table-boundaries", left=boxes[0], right=boxes[1])
    for key, kind in (("rowBands", "row-boundaries"), ("columnIntervals", "column-boundaries")):
        if a[key] and b[key] and not _axis_agrees(a, b, key, geometry_tolerance):
            differ(kind, left=a[key], right=b[key])
    a_cells = {(c["row"], c["column"]): c for c in a["cells"]}
    b_cells = {(c["row"], c["column"]): c for c in b["cells"]}
    structure_agrees = not differences
    for position in sorted(a_cells.keys() | b_cells.keys()):
        ca, cb = a_cells.get(position), b_cells.get(position)
        r, c = position
        if ca is None or cb is None or (ca["rowSpan"], ca["columnSpan"]) != (cb["rowSpan"], cb["columnSpan"]):
            differ("cell-layout", row=r, column=c)
            structure_agrees = False
    for candidate in (a, b):
        for r, row in enumerate(candidate["grid"], 1):
            for c, pos in enumerate(row, 1):
                if pos["state"] in {"omitted", "unreadable"}:
                    differ(pos["state"], candidateId=candidate["id"], row=r, column=c)
                    if pos["state"] == "omitted":
                        structure_agrees = False
        if candidate["unassignedWordIds"] or candidate["conflictingWordIds"]:
            differ("native-evidence", candidateId=candidate["id"], unassignedWordIds=candidate["unassignedWordIds"],
                   conflictingWordIds=candidate["conflictingWordIds"])
        for issue in candidate["issues"]:
            if issue.get("kind") == "native-span-reading-conflict":
                differ("native-span-reading-conflict", candidateId=candidate["id"], issue=issue)
        for cell in candidate["cells"]:
            if cell["wordIds"]:
                backing.append({"candidateId": candidate["id"], "row": cell["row"], "column": cell["column"],
                                "wordIds": list(cell["wordIds"]), "occurrenceCount": len(cell["wordIds"])})
    # Prose normalization is permitted only inside already corresponding grids.
    if structure_agrees:
        for (r, c), ca in a_cells.items():
            cb = b_cells[(r, c)]
            if ca["text"] is None or cb["text"] is None:
                continue
            numeric = any(char.isnumeric() for char in ca["text"] + cb["text"])
            na, nb = _normalize(ca["text"], numeric), _normalize(cb["text"], numeric)
            if na != nb:
                differ("cell-text", row=r, column=c, left=ca["text"], right=cb["text"])
            if na != ca["text"] or nb != cb["text"]:
                normalizations.append({"row": r, "column": c, "rule": "outer-trim" if numeric else "prose-whitespace",
                                       "left": na, "right": nb})
    return {"agreed": not differences, "basis": "positioned-grid" if a["rowBands"] and b["rowBands"] else "ordered-grid",
            "structureAgrees": structure_agrees, "differences": differences, "normalizations": normalizations,
            "nativeBacking": backing}


def _intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _same_region(a, b, tolerance, threshold, related=None):
    ba, bb = _box(a["bounds"]), _box(b["bounds"])
    if a["page"] != b["page"] or ba is None or bb is None:
        return False
    area_a, area_b = (ba[2] - ba[0]) * (ba[3] - ba[1]), (bb[2] - bb[0]) * (bb[3] - bb[1])
    intersection = _intersection(ba, bb)
    same_bounds = all(abs(x - y) <= tolerance for x, y in zip(ba, bb))
    region = same_bounds or intersection / (area_a + area_b - intersection) >= threshold
    layout = a["columnCount"] == b["columnCount"]
    if a["columnIntervals"] and b["columnIntervals"]:
        layout = layout and _intervals_equal(a["columnIntervals"], b["columnIntervals"], tolerance)
    if related == "parent":
        return same_bounds
    return region and (related == "word-occurrences" or layout)


def group_candidates(candidates: list[dict], *, region_tolerance: float = 2, overlap_threshold: float = .8) -> list[dict]:
    """Group alternatives by physical occurrence, never equal cell text.

    Membership requires pairwise-compatible regions, preventing overlap chains.
    Missing bounds and ambiguous matches keep proposals distinct. ``relations``
    explains grouping exceptions: parent ancestry permits different layouts at
    matching bounds. Word-occurrences requires the same nonempty region word IDs
    and base-list identities in compatible regions. Layout disagreement remains
    an issue. ``ambiguousWith`` links distinct groups whose regions overlap.
    """
    if not _finite(region_tolerance) or region_tolerance < 0 or not _finite(overlap_threshold) or not 0 < overlap_threshold <= 1:
        raise ValueError("invalid grouping tolerances")
    by_id = {(c["page"], c["id"]): c for c in candidates}
    families, region_ids, bases = {}, {}, {}
    for candidate in candidates:
        ids, current_id = set(), candidate["id"]
        while current_id is not None and current_id not in ids:
            ids.add(current_id)
            current = by_id.get((candidate["page"], current_id))
            current_id = current.get("parentId") if current is not None else None
        key = (candidate["page"], candidate["id"])
        families[key] = ids
        bases[key] = _word_identity(candidate["words"])
        region_ids[key] = {w["id"] for w in _region_words(candidate, candidate["words"])}

    def connection(a, b):
        ka, kb = (a["page"], a["id"]), (b["page"], b["id"])
        if families[ka] & families[kb] and _same_region(a, b, region_tolerance, overlap_threshold, "parent"):
            return "parent"
        if region_ids[ka] and region_ids[ka] == region_ids[kb] and bases[ka] == bases[kb]:
            return "word-occurrences"
        return None

    groups, members = [], []
    for candidate in candidates:
        matches = [index for index, group in enumerate(members)
                   if all(_same_region(candidate, other, region_tolerance, overlap_threshold,
                                       connection(candidate, other)) for other in group)]
        if len(matches) == 1:
            index = matches[0]
            for other in members[index]:
                relation = connection(candidate, other)
                if relation:
                    record = {"kind": relation, "candidateIds": [other["id"], candidate["id"]]}
                    if relation == "word-occurrences":
                        record["wordIds"] = sorted(region_ids[(candidate["page"], candidate["id"])])
                    groups[index]["relations"].append(record)
            if any(candidate["columnCount"] != other["columnCount"] or candidate["rowCount"] != other["rowCount"]
                   or (candidate["columnIntervals"] and other["columnIntervals"]
                       and not _intervals_equal(candidate["columnIntervals"], other["columnIntervals"], region_tolerance))
                   for other in members[index]) and "layout-disagreement" not in groups[index]["issues"]:
                groups[index]["issues"].append("layout-disagreement")
            members[index].append(candidate)
            groups[index]["candidateIds"].append(candidate["id"])
        else:
            members.append([candidate])
            groups.append({"id": "region:" + candidate["id"], "page": candidate["page"], "bounds": deepcopy(candidate["bounds"]),
                           "candidateIds": [candidate["id"]], "ambiguousWith": [],
                           "issues": ["missing-bounds"] if candidate["bounds"] is None else [], "relations": []})
    for index, group in enumerate(groups):
        for j in range(index + 1, len(groups)):
            other = groups[j]
            if group["page"] != other["page"]:
                continue
            if any(a["bounds"] is not None and b["bounds"] is not None and _intersection(_box(a["bounds"]), _box(b["bounds"])) > 0
                   for a in members[index] for b in members[j]):
                group["ambiguousWith"].append(other["id"])
                other["ambiguousWith"].append(group["id"])
    return groups


def propose_regions(words: list[dict], page: int, *, min_rows: int = 3, min_columns: int = 2,
                    alignment_tolerance: float = 2, row_gap_factor: float = 3,
                    word_gap_factor: float = .65, include_lists: bool = False) -> list[dict]:
    """Propose regions from repeated left, centre, or right word alignment.

    Gutters alone do not split a wide table, and contiguous prose words do not
    supply independent column anchors. ``include_lists`` controls whether bullet
    prefixes with hanging prose indents can qualify. Proposals still need
    extraction and confirmation. Repeated alignment does not prove a table.
    """
    _check_words(words)
    if not _integer(page) or not _integer(min_rows, 2) or not _integer(min_columns, 2):
        raise ValueError("page and minimum axis counts must be integers (at least two rows/columns)")
    if not _finite(alignment_tolerance) or alignment_tolerance < 0 or not _finite(row_gap_factor) or row_gap_factor <= 0:
        raise ValueError("invalid alignment tolerances")
    if not _finite(word_gap_factor) or word_gap_factor < 0:
        raise ValueError("word_gap_factor must be finite and nonnegative")
    if not isinstance(include_lists, bool):
        raise TypeError("include_lists must be a boolean")
    blocks = []
    for band in _baselines(words):
        if blocks:
            previous = blocks[-1][-1]
            height = median(w["bbox"][3] - w["bbox"][1] for w in previous + band)
            if _band_box(band)[0] - _band_box(previous)[1] <= height * row_gap_factor:
                blocks[-1].append(band)
                continue
        blocks.append([band])
    proposals = []
    for bands in blocks:
        if len(bands) < min_rows:
            continue
        runs = [_text_runs(band, word_gap_factor) for band in bands]
        columns = _aligned_columns(runs, alignment_tolerance, min_rows)
        if len(columns) < min_columns:
            continue
        dense = [i for i, band in enumerate(runs)
                 if sum(any(left <= run["bbox"][0] and run["bbox"][2] <= right for run in band) for left, right in columns) >= min_columns]
        if len(dense) < min_rows:
            continue
        if not include_lists:
            prefixes = [run for i in dense for run in runs[i]
                        if columns[0][0] <= run["bbox"][0] and run["bbox"][2] <= columns[0][1]]
            # A bullet prefix plus a hanging prose indent is list formatting,
            # not two fields. Equal codes/numbers at distinct positions survive.
            if (prefixes and all(len(run["words"]) == 1 and len(run["words"][0]["text"]) == 1
                                 and run["words"][0]["text"] in "•●◦▪▫‣⁃∙·\uf0b7" for run in prefixes)
                    and any(len(run["words"]) > 1 or any(len(w["text"].split()) > 1 for w in run["words"])
                            for i in dense for run in runs[i] if run not in prefixes)):
                continue
        included = set(dense)
        # Keep nearby continuations only inside the established column layout,
        # without pulling in captions, footer numbers or neighbouring prose.
        for index in range(dense[0], len(bands)):
            if index in included or index - 1 not in included:
                continue
            band = bands[index]
            height = median(w["bbox"][3] - w["bbox"][1] for w in band)
            distance = _band_box(band)[1] - _band_box(bands[index - 1])[1]
            if distance <= height * 1.6 and all(any(left <= run["bbox"][0] and run["bbox"][2] <= right for left, right in columns)
                                              for run in runs[index]):
                included.add(index)
        retained = [bands[i] for i in sorted(included)]
        claimed = [w for band in retained for w in band if any(left <= w["bbox"][0] and w["bbox"][2] <= right for left, right in columns)]
        proposals.append({"page": page, "bounds": [columns[0][0], min(w["bbox"][1] for w in claimed),
                                                   columns[-1][1], max(w["bbox"][3] for w in claimed)],
                          "method": "text-alignment", "rowBands": [_band_box(b) for b in retained],
                          "columnIntervals": columns, "wordIds": [w["id"] for w in claimed]})
    return proposals


def _check_bands(bands, top, bottom):
    if any(not isinstance(b, (list, tuple)) or len(b) != 2 or not all(_finite(v) for v in b)
           or not top <= b[0] < b[1] <= bottom for b in bands):
        raise ValueError("row bands must be finite intervals inside the source region")
    if any(a[1] > b[0] for a, b in pairwise(bands)):
        raise ValueError("row bands must be ordered without overlaps")


def plan_tiles(bounds, *, max_height: float, overlap: float, row_bands=()) -> dict:
    """Plan gap-free overlapping strips with complete coverage for each row band.

    Height and overlap use PDF points at the caller's chosen rendering scale.
    ``tiles`` assigns IDs to bounds. ``seams`` links adjacent tile IDs to their
    overlap intervals and midpoint positions. Impossible bands raise ValueError
    so callers can adjust limits or framing.
    """
    box = _box(bounds)
    if box is None:
        raise ValueError("tile bounds are required")
    if not _finite(max_height) or not _finite(overlap) or not 0 < overlap < max_height:
        raise ValueError("tile height must be finite and positive with 0 < overlap < height")
    _check_bands(row_bands, box[1], box[3])
    if any(bottom - top > max_height for top, bottom in row_bands):
        raise ValueError("a row band exceeds the maximum tile height")
    intervals = []
    top = box[1]
    while True:
        bottom = min(box[3], top + max_height)
        intervals.append([top, bottom])
        if bottom == box[3]:
            break
        top = bottom - overlap
    for top, bottom in row_bands:
        if not any(t <= top and bottom <= b for t, b in intervals):
            centre = (top + bottom) / 2
            start = max(box[1], min(centre - max_height / 2, box[3] - max_height))
            intervals.append([start, min(box[3], start + max_height)])
    intervals.sort()
    tiles = [{"id": f"tile_{i:04d}", "bounds": [box[0], top, box[2], bottom]}
             for i, (top, bottom) in enumerate(intervals, 1)]
    seams = []
    for a, b in pairwise(tiles):
        top, bottom = max(a["bounds"][1], b["bounds"][1]), min(a["bounds"][3], b["bounds"][3])
        seams.append({"tileIds": [a["id"], b["id"]], "position": (top + bottom) / 2, "overlap": [top, bottom]})
    return {"bounds": deepcopy(bounds), "tiles": tiles, "seams": seams}


def _tile_row(candidate, row, tile_box):
    cells = [deepcopy(c) for c in candidate["cells"] if c["row"] == row]
    for cell in cells:
        cell["row"] = 1
    band = candidate["rowBands"][row - 1]
    row_bounds = [tile_box[0], band[0], tile_box[2], band[1]]
    result = _candidate(candidate["id"], candidate["page"], row_bounds, candidate["method"], 1, candidate["columnCount"], cells)
    result["columnIntervals"] = deepcopy(candidate["columnIntervals"])
    result["columnIntervalKind"] = candidate["columnIntervalKind"]
    result["words"] = deepcopy(candidate["words"])
    return result


def aggregate_tiles(plan: dict, reads: list[dict], id: str, page: int, bounds,
                    *, position_tolerance: float = 2) -> dict:
    """Reconcile mapped row occurrences, never equal row text.

    Reads pair ``tileId`` with ``candidate`` and may mark ``truncated``.
    ``rowSources`` maps each aggregate row to its tile, candidate, original row,
    and clipping status. Complete occurrences supply values when available.
    An edge-only fallback remains unresolved. Disagreement, missing positions,
    or ambiguous seams also leave ``state`` unresolved. ``candidate`` can be
    None when no usable aggregate exists. Callers retain the original reads.
    """
    _identity(id, page)
    region = _box(bounds)
    if region is None or not _finite(position_tolerance) or position_tolerance < 0:
        raise ValueError("aggregation needs bounds and a nonnegative finite position tolerance")
    issues, occurrences = [], []
    tiles = {t["id"]: t for t in plan["tiles"]}
    seen = set()
    for read in reads:
        tile_id, candidate = read["tileId"], read["candidate"]
        if tile_id not in tiles or tile_id in seen:
            issues.append({"kind": "unknown-or-duplicate-tile", "tileId": tile_id})
            continue
        seen.add(tile_id)
        tile_box = _box(tiles[tile_id]["bounds"])
        if candidate["page"] != page or not candidate["tablePresent"]:
            issues.append({"kind": "tile-presence-or-page", "tileId": tile_id})
            continue
        if candidate["unassignedWordIds"] or candidate["conflictingWordIds"]:
            issues.append({"kind": "native-evidence", "tileId": tile_id,
                           "unassignedWordIds": candidate["unassignedWordIds"], "conflictingWordIds": candidate["conflictingWordIds"]})
        bands = candidate["rowBands"]
        if len(bands) != candidate["rowCount"] or not bands:
            issues.append({"kind": "missing-row-positions", "tileId": tile_id})
            continue
        try:
            _check_bands(bands, tile_box[1], tile_box[3])
        except ValueError:
            issues.append({"kind": "invalid-row-positions", "tileId": tile_id})
            continue
        # Cross-row spans cannot be independently reconciled as isolated row reads.
        # Retain the read and ask for a complete span-containing band instead.
        if any(c["rowSpan"] > 1 for c in candidate["cells"]):
            issues.append({"kind": "span-crosses-row-seam", "tileId": tile_id})
            continue
        for row, band in enumerate(bands, 1):
            clipped = bool(read.get("truncated")) or band[0] <= tile_box[1] or band[1] >= tile_box[3]
            occurrences.append({"band": list(band), "tileId": tile_id, "row": row, "clipped": clipped,
                                "candidate": _tile_row(candidate, row, tile_box)})
    for tile_id in tiles.keys() - seen:
        issues.append({"kind": "missing-tile", "tileId": tile_id})
    groups = []
    for occurrence in sorted(occurrences, key=lambda o: (o["band"][0], o["band"][1], o["tileId"])):
        matches = []
        for index, group in enumerate(groups):
            if any(other["tileId"] == occurrence["tileId"] for other in group):
                continue
            def same(other, occurrence=occurrence):
                a, b = other["band"], occurrence["band"]
                if other["clipped"] or occurrence["clipped"]:
                    return abs(a[0] - b[0]) <= position_tolerance or abs(a[1] - b[1]) <= position_tolerance
                return abs(a[0] - b[0]) <= position_tolerance and abs(a[1] - b[1]) <= position_tolerance
            if all(same(other) for other in group):
                matches.append(index)
        if len(matches) == 1:
            groups[matches[0]].append(occurrence)
        else:
            groups.append([occurrence])
            if matches:
                issues.append({"kind": "ambiguous-row-match", "band": occurrence["band"]})
    selected, row_sources = [], []
    for group in groups:
        complete = [o for o in group if not o["clipped"]]
        if not complete:
            issues.append({"kind": "edge-only-row", "band": group[0]["band"]})
        chosen = (complete or group)[0]
        group_tiles = {o["tileId"] for o in group}
        for tile_id in seen - group_tiles:
            box = tiles[tile_id]["bounds"]
            if box[1] < chosen["band"][0] and chosen["band"][1] < box[3]:
                issues.append({"kind": "missing-overlap-row", "tileId": tile_id, "band": chosen["band"]})
        for other in complete:
            comparison = compare_candidates(chosen["candidate"], other["candidate"])
            if not comparison["agreed"]:
                issues.append({"kind": "seam-row-disagreement", "band": chosen["band"], "differences": comparison["differences"]})
        selected.append(chosen)
        row_sources.append([{ "tileId": o["tileId"], "candidateId": o["candidate"]["id"], "row": o["row"],
                              "clipped": o["clipped"]} for o in group])
    # Unmatched overlapping reads must not silently become additional rows.
    for a, b in pairwise(selected):
        if a["band"][1] > b["band"][0]:
            issues.append({"kind": "ambiguous-row-overlap", "bands": [a["band"], b["band"]]})
    for seam in plan["seams"]:
        if not any(set(seam["tileIds"]) <= {o["tileId"] for o in group} for group in groups):
            issues.append({"kind": "unresolved-seam", "tileIds": seam["tileIds"]})
    if not selected:
        return {"state": "unresolved", "candidate": None, "issues": issues, "rowSources": []}
    nc = selected[0]["candidate"]["columnCount"]
    if any(o["candidate"]["columnCount"] != nc for o in selected):
        issues.append({"kind": "incompatible-tile-columns"})
        return {"state": "unresolved", "candidate": None, "issues": issues, "rowSources": row_sources}
    cells = []
    for row, occurrence in enumerate(selected, 1):
        for cell in occurrence["candidate"]["cells"]:
            cells.append(deepcopy(cell) | {"row": row})
    candidate = _candidate(id, page, bounds, "tile-aggregation", len(selected), nc, cells)
    candidate["rowBands"] = [o["band"] for o in selected]
    return {"state": "unresolved" if issues else "agreed", "candidate": candidate,
            "issues": issues, "rowSources": row_sources}
