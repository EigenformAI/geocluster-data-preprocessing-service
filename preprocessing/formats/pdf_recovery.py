"""Effectful PDF recovery orchestration around the pure occurrence/grid algorithms."""

import copy
import hashlib
import json
import math
from itertools import pairwise
from pathlib import Path
from statistics import median

from preprocessing.errors import ServiceError
from preprocessing.table_recovery import (
    RECOVERY_VERSIONS,
    resolve_policy,
    semantic_fingerprint,
    validate_page_range,
)

from .common import coverage, finding, product, stage_outcome, write_json, write_text
from .pdf_tables import (
    aggregate_reads,
    assess_geometry,
    candidate_from_rows,
    compare_candidates,
    group_candidates,
    plan_tiles,
    propose_regions,
    recover_geometry,
    validate_visual,
    word_occurrences,
)

READ_PROMPT = (
    'Independently transcribe this source table image. Do not infer, correct or normalize values. '
    'Include all visible headers and sparse rows. Preserve numeric spelling, signs, units and leading zeros. '
    'Return only JSON: {"tablePresent":true,"rowCount":integer,"columnCount":integer,'
    '"cells":[{"row":1,"column":1,"text":"exact text or null","rowSpan":1,"columnSpan":1,'
    '"unreadableReason":"only when unavailable"}],"rowEdges":[numbers],"columnEdges":[numbers]}. '
    'Indices are one-based. Include each logical anchor, including empty cells as ""; '
    'use null and an unreadableReason for unavailable readings. Spans anchor once. '
    'Edges, if readable, are normalized 0..1000 in this actual image with top-left origin. '
    'Do not invent edges. Report non-table content with tablePresent false, zero counts and cells []. '
    'Rows touching image edges may be clipped: never complete a half-row from context.'
)
SCOUT_PROMPT = (
    'Locate every visible table on this page, including modest image tables among prose, '
    'side-by-side and vertically stacked tables. Return only JSON {"tables":['
    '{"bounds":[x0,top,x1,bottom],"orientation":0}]}. Bounds are normalized 0..1000 '
    'in this input image, top-left origin. Orientation is clockwise correction: 0,90,180,270. '
    'Exclude logos, prose and figures with no table. Do not transcribe cell values.'
)


def _box(value):
    return value.get("bbox") if isinstance(value, dict) else value


def _contains(a, b):
    return a[0] <= b[0] and a[1] <= b[1] and b[2] <= a[2] and b[3] <= a[3]


def _intersection(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _point(transform, x, y):
    a, b, c, d, e, f = transform
    return [a * x + c * y + e, b * x + d * y + f]


def _map_box(transform, box):
    points = [_point(transform, x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _upright_transforms(size, orientation):
    width, height = size
    forward = {0: [1, 0, 0, 1, 0, 0], 90: [0, 1, -1, 0, height, 0],
               180: [-1, 0, 0, -1, width, height], 270: [0, -1, 1, 0, 0, width]}[orientation]
    a, b, c, d, e, f = forward
    det = a * d - b * c
    inverse = [d / det, -b / det, -c / det, a / det, (c * f - d * e) / det, (b * e - a * f) / det]
    return forward, inverse


def _whole_read(candidate):
    scope = candidate.get("transcriptScope", {})
    return scope.get("complete", True) and scope.get("kind") != "tile" and not candidate.get("aggregationIssues")


def _same_occurrence(a, b):
    a, b = _box(a), _box(b)
    if a is None or b is None:
        return False
    intersection = _intersection(a, b)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return all(abs(x - y) <= 2 for x, y in zip(a, b)) or intersection / union >= .8


def _normalized_box(value, page):
    if (not isinstance(value, list) or len(value) != 4 or
            any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        return None
    return [round(value[0] * page.width / 1000, 6), round(value[1] * page.height / 1000, 6),
            round(value[2] * page.width / 1000, 6), round(value[3] * page.height / 1000, 6)]


class PDFRecovery:
    """Own model effects, listed evidence, stable work units and deferred dispute reads."""

    def __init__(self, snapshot, profile, output, context, result, renderer, limits):
        self.output, self.context, self.result, self.renderer, self.limits = output, context, result, renderer, limits
        self.figure_descriptions = profile.get("options", {}).get("figureDescriptions", True)
        self.policy = resolve_policy(profile.get("options", {}).get("tableRecovery"),
                                     job_seconds=limits["jobSeconds"], override=profile.get("_tableRecoveryOverride"))
        # Allow 1 KiB per dense position for grid/anchor dictionaries and lists.
        # Reserve three quarters for parser/render memory and simultaneous views.
        # Capacity grows with workerMemoryBytes; this is no transcription-quality cap.
        self.visual_limits = {"max_rows": limits["rows"], "max_columns": limits["columns"],
                              "max_grid_cells": limits["workerMemoryBytes"] // (4 * 1024)}
        # Direct adapter fixtures sometimes omit snapshot metadata; retained service snapshots always have it.
        snapshot = dict(snapshot)
        if "sha256" not in snapshot:
            snapshot["sha256"] = hashlib.sha256(Path(snapshot["path"]).read_bytes()).hexdigest()
        from .pdf import PDFAdapter
        self.fingerprint = semantic_fingerprint(snapshot, profile, adapter_version=PDFAdapter.version)
        self.units = {}
        self.records = {}
        self.pending = []
        self.calls = self.passes = 0
        self._cutoff = False
        self.figure_discovery = {"pagesScanned": 0, "excludedEmbeddedImages": 0}
        self.resume = getattr(context, "resume", None) or {}
        invalidated = set(self.resume.get("invalidatedUnitIds", []))
        self.old_units = {u["id"]: copy.deepcopy(u) for u in self.resume.get("workPlan", {}).get("units", [])
                          if u["id"] not in invalidated}
        for unit in self.resume.get("completedUnits", []):
            if unit["id"] not in invalidated:
                self.units[unit["id"]] = copy.deepcopy(unit) | {"reusedFromJob": self.resume.get("reusedFromJob")}
        for identity, unit in self.old_units.items():
            if unit["state"] == "not_applicable":
                self.units[identity] = copy.deepcopy(unit)
        for item in self.resume.get("products", []):
            if item["kind"] not in {"work-plan", "page-inventory", "navigation-index", "table-preview", "figure-index", "agent-guide"} and (output / item["path"]).is_file():
                result["products"].append(copy.deepcopy(item))
        self.previous = self.resume.get("previousExtraction", {})
        self.invalidated_pages = set(self.resume.get("forcePages", []))
        result["provenance"].update(tableRecovery=self.policy, recoveryVersions=RECOVERY_VERSIONS)
        result["provenance"]["visualGridAllocation"] = {"maxGridCells": self.visual_limits["max_grid_cells"],
                                                      "estimatedBytesPerCell": 1024, "memoryReserveFraction": .75}
        self.discovery = {"inspectedPages": 0, "scoutedPages": 0, "forcedPages": 0, "rejectedProposals": 0,
                          "unresolvedRegions": 0, "limitations": "Alignment and image scouting are proposals; discovery is not complete recall."}

    def emit(self, name, **data):
        event = getattr(self.context, "event", None)
        if callable(event):
            event("table." + name, **data)

    def available(self):
        remaining = getattr(self.context, "work_remaining", None)
        if self._cutoff or (callable(remaining) and remaining() <= 0):
            self._cutoff = True
            return False
        return True

    def check_work(self):
        if not self.available():
            raise ServiceError("WORK_BUDGET_EXHAUSTED", "PDF soft work window exhausted.")

    @property
    def work_exhausted(self):
        return self._cutoff

    def interruption_state(self, exc):
        """A real failure stays failed even when the clock expires during it."""
        if isinstance(exc, ServiceError) and exc.code == "WORK_BUDGET_EXHAUSTED":
            self._cutoff = True
            return "pending"
        return "failed"

    def record_native_failure(self, state, reason):
        self.units[f"p{self.page:04d}:native"].update(state=state, reason=reason)

    def record_figure_discovery(self, excluded=0):
        self.figure_discovery["pagesScanned"] += 1
        self.figure_discovery["excludedEmbeddedImages"] += excluded

    def validate_transcript(self, response, identity, page, bounds, model=None):
        validation = validate_visual(response, identity, page, bounds, model, **self.visual_limits)
        if validation["resourceLimit"]:
            limit = validation["resourceLimit"]
            raise ServiceError("RESOURCE_LIMIT", f"Visual table {limit['name']} count {limit['requested']} exceeds configured capacity {limit['limit']}.")
        return validation

    def unit(self, identity, kind, page, *, dependencies=(), paths=(), state="pending", **extra):
        if identity not in self.units:
            self.units[identity] = {"id": identity, "kind": kind, "source": {"page": page}, "state": state,
                                    "dependencies": list(dependencies), "evidencePaths": list(paths),
                                    "fingerprint": self.fingerprint, **extra}
        return self.units[identity]

    def enumerate_pages(self, total, *, figure_detection_limitations):
        validate_page_range(self.policy, total)
        self.total = total
        self.figure_detection_limitations = figure_detection_limitations
        for page in range(1, total + 1):
            self.unit(f"p{page:04d}:native", "native-evidence", page)
            self.unit(f"p{page:04d}:discovery", "table-discovery", page, dependencies=[f"p{page:04d}:native"])
            self.unit(f"p{page:04d}:page-ocr", "page-ocr", page, dependencies=[f"p{page:04d}:native"])
            self.unit(f"p{page:04d}:figure-detection", "figure-detection", page, dependencies=[f"p{page:04d}:native"])

    def reuse_page(self, page, inventory):
        complete = self.units.get(f"p{page:04d}:complete")
        if not complete or complete["state"] != "completed":
            return False
        path = self.output / f"tables/evidence/page_{page:04d}.candidates.jsonl"
        if path.is_file():
            self.restore_table_products(page, [json.loads(line) for line in path.read_text().splitlines()])
        for key in ("tables", "figures"):
            self.result[key].extend(copy.deepcopy([r for r in self.previous.get(key, []) if r.get("source", {}).get("page") == page]))
        record = complete.get("pageRecord", {"id": f"page_{page:04d}", "source": {"page": page}, "outcome": "succeeded"})
        inventory.write(json.dumps(record) + "\n")
        self.records[page] = record
        self.record_figure_discovery()
        self.discovery["inspectedPages"] += 1
        return True

    def reuse_figure(self, identity):
        """Return retained OCR and whether the entire figure can be reused."""
        previous = next((f for f in self.previous.get("figures", []) if f["id"] == identity), None)
        if not previous or self.units.get(identity + ":figure-ocr", {}).get("state") != "completed":
            return None, False
        complete = (self.units.get(identity + ":figure-description", {}).get("state") == "completed"
                    or not self.figure_descriptions)
        return copy.deepcopy(previous), complete

    def begin_page(self, page):
        self.page = page
        self.candidates = []
        self.base_words = []
        self.images = []
        self.page_tables = []
        self.candidate_path = f"tables/evidence/page_{page:04d}.candidates.jsonl"
        self.serial = 0
        prefix = f"table_p{page:04d}_"
        prior_ids = [t["id"] for t in self.previous.get("tables", []) if t.get("source", {}).get("page") == page]
        prior_ids.extend(u.get("regionId", "") for u in self.resume.get("workPlan", {}).get("units", [])
                         if u.get("source", {}).get("page") == page and u["kind"] == "table-confirmation")
        self.table_serial = max((int(identity[len(prefix):]) for identity in prior_ids
                                 if identity.startswith(prefix) and identity[len(prefix):].isdigit()), default=0)
        self.claimed_table_ids = set()
        if page not in self.invalidated_pages and (self.output / self.candidate_path).is_file():
            self.candidates = [json.loads(line) for line in (self.output / self.candidate_path).read_text().splitlines()]
            self.serial = max((int(c["id"].rsplit("_", 1)[-1]) for c in self.candidates), default=0)
            self.restore_table_products(page, self.candidates)

    def restore_table_products(self, page, candidates):
        """Coordinator copies may contain only path/kind/type; evidence retains metadata."""
        by_id = {c["id"]: c for c in candidates}
        products = {p["path"]: p for p in self.result["products"]}
        for unit in self.units.values():
            if unit["source"]["page"] != page or unit["state"] != "completed":
                continue
            candidate = by_id.get(unit.get("candidateId"), {})
            render = candidate.get("imageTransform", unit.get("render"))
            model = candidate.get("model", unit.get("model"))
            for path in unit["evidencePaths"]:
                item = products.get(path)
                if item is None:
                    continue
                if item["kind"] == "table-image" and render:
                    metadata = render.get("sourceRender") if path.endswith(".source.png") else render
                    if metadata:
                        item.update(render=copy.deepcopy(metadata), source={"page": page, "bounds": metadata.get("bbox", unit["source"].get("bounds"))})
                elif item["kind"] == "table-model-response" and model:
                    item.update(model=copy.deepcopy(model), source={"page": page})

    def next_id(self):
        self.serial += 1
        return f"candidate_p{self.page:04d}_{self.serial:06d}"

    def reused_native(self):
        return self.units[f"p{self.page:04d}:native"]["state"] == "completed"

    def replace_product(self, path):
        self.result["products"][:] = [p for p in self.result["products"] if p["path"] != path]

    def reused_ocr(self, record):
        unit = self.units[f"p{self.page:04d}:page-ocr"]
        if unit["state"] != "completed":
            return False
        record.update(ocrStatus="succeeded", ocrPath=f"text/page_{self.page:04d}.ocr.txt")
        return True

    def add(self, candidate):
        self.candidates.append(candidate)
        self.persist_candidates()
        return candidate

    def persist_candidates(self):
        target = self.output / self.candidate_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in self.candidates), encoding="utf-8")
        if not any(p["path"] == self.candidate_path for p in self.result["products"]):
            product(self.result, self.candidate_path, "table-candidates", "application/x-ndjson", source={"page": self.page})

    def words(self, page, number):
        evidence = self.output / f"text/page_{number:04d}.json"
        if self.units[f"p{number:04d}:native"]["state"] == "completed" and evidence.is_file():
            self.base_words = json.loads(evidence.read_text())["words"]
        else:
            self.check_work()
            self.base_words = word_occurrences(number, [{"text": w["text"], "bbox": [w["x0"] - page.bbox[0],
                w["top"] - page.bbox[1], w["x1"] - page.bbox[0], w["bottom"] - page.bbox[1]]} for w in page.extract_words()])
        return self.base_words

    def native(self, page, words):
        from .pdf import bounds
        self.media_box = list(page.page_obj.mediabox)
        if any(c["method"] == "native-text-layer" for c in self.candidates):
            return
        self.check_work()
        for table in page.find_tables():
            rows = table.extract() or []
            if rows:
                cells = [[None if cell is None else bounds(page, cell)["bbox"] for cell in row.cells] for row in table.rows]
                self.add(candidate_from_rows(self.next_id(), self.page, bounds(page, table.bbox), rows,
                                             "native-text-layer", cell_bounds=cells, words=words))
        if self.policy["geometry"] != "off":
            # Alternate horizontal text rules use the native vertical grid and
            # bounded crops. This avoids losing outer columns to text extent.
            initial = list(self.candidates)
            for parent in initial:
                if parent["method"] != "native-text-layer" or not parent["columnIntervals"]:
                    continue
                forced = self.policy["geometry"] == "force" or self.page in self.policy["forcePages"]
                if not forced and not assess_geometry(parent, words)["suspect"]:
                    continue
                self.check_work()
                rectangle = _box(parent["bounds"])
                absolute = [rectangle[0] + page.bbox[0], rectangle[1] + page.bbox[1],
                            rectangle[2] + page.bbox[0], rectangle[3] + page.bbox[1]]
                try:
                    crop = page.crop(absolute)
                    edges = sorted({v + page.bbox[0] for interval in parent["columnIntervals"] for v in interval})
                    for table in crop.find_tables({"vertical_strategy": "explicit", "explicit_vertical_lines": edges,
                                                  "horizontal_strategy": "text"}):
                        rows = table.extract() or []
                        if not rows:
                            continue
                        cells = [[None if cell is None else bounds(page, cell)["bbox"] for cell in row.cells] for row in table.rows]
                        candidate = candidate_from_rows(self.next_id(), self.page, parent["bounds"], rows,
                                                        "line-text", cell_bounds=cells, words=words)
                        candidate["parentId"] = parent["id"]
                        self.add(candidate)
                except Exception as exc:  # Alternate parsing cannot discard native evidence.
                    from .pdf import _raise_if_fatal
                    _raise_if_fatal(exc)
                    if isinstance(exc, ServiceError) and exc.code == "WORK_BUDGET_EXHAUSTED":
                        raise
                    parent["issues"].append({"kind": "alternate-extraction-failed", "reason": getattr(exc, "code", type(exc).__name__)})
                    finding(self.result, "TABLE_GEOMETRY_FAILED", "Alternate table geometry extraction failed; native evidence is retained.", sourcePages=[self.page])
        if self.policy["geometry"] != "off":
            proposals = propose_regions(words, self.page)
            for rectangle in self.ruled_regions(page, words):
                # Closed subdivisions wholly inside one native row band are
                # cell/batch framing, not independent panel evidence.
                row_framing = any(c["bounds"] and _contains(_box(c["bounds"]), rectangle) and
                                  any(top - 2 <= rectangle[1] and rectangle[3] <= bottom + 2 for top, bottom in c["rowBands"])
                                  for c in initial if c["method"] == "native-text-layer")
                local = [w for w in words if _contains(rectangle, w["bbox"])]
                for proposal in propose_regions(local, self.page):
                    proposal = dict(proposal, regionBounds=rectangle, regionEvidence="closed-rulings-and-repeated-alignment")
                    first_top, first_bottom = proposal["rowBands"][0]
                    # Alignment starts at repeated data lanes. A title above
                    # those lanes is still a source row inside this closed panel.
                    heading = sorted((w for w in local if w["bbox"][3] < first_top),
                                     key=lambda w: (w["bbox"][0], w["id"]))
                    if heading:
                        heading_top = min(w["bbox"][1] for w in heading)
                        heading_bottom = max(w["bbox"][3] for w in heading)
                        height = median(w["bbox"][3] - w["bbox"][1] for w in heading)
                        tight_run = all(b["bbox"][0] - a["bbox"][2] <= height * .65 for a, b in pairwise(heading))
                        crosses_lane = any(w["bbox"][0] < right and left < w["bbox"][2]
                                           for w in heading for left, right in proposal["columnIntervals"][1:])
                        crosses_rule = any(edge.get("orientation") == "v" and rectangle[0] + 2 < edge["x0"] - page.bbox[0] < rectangle[2] - 2
                                           and edge["top"] - page.bbox[1] < heading_bottom and heading_top < edge["bottom"] - page.bbox[1]
                                           for edge in page.edges)
                        if (tight_run and heading_bottom - heading_top <= height * 1.35
                                and first_top - heading_bottom <= height * 2 and crosses_lane and not crosses_rule):
                            proposal["rowBands"] = [[heading_top, heading_bottom], *proposal["rowBands"]]
                            proposal["spanningHeading"] = " ".join(w["text"] for w in heading)
                            proposal["wordIds"] = [w["id"] for w in heading] + proposal["wordIds"]
                            proposal["bounds"][1] = heading_top
                            proposals.append(proposal)
                            continue
                    if row_framing:
                        continue
                    first_words = [w for w in local if first_top <= w["bbox"][1] and w["bbox"][3] <= first_bottom]
                    if len(first_words) == 1 and any(first_words[0]["bbox"][0] < right and left < first_words[0]["bbox"][2]
                                                   for left, right in proposal["columnIntervals"][1:]):
                        crosses_rule = any(edge.get("orientation") == "v" and rectangle[0] + 2 < edge["x0"] - page.bbox[0] < rectangle[2] - 2
                                           and edge["top"] - page.bbox[1] < first_bottom and first_top < edge["bottom"] - page.bbox[1]
                                           for edge in page.edges)
                        if not crosses_rule:
                            proposal["spanningHeading"] = first_words[0]["text"]
                    proposals.append(proposal)
            seen = []
            for proposal in proposals:
                box = proposal["bounds"]
                region = proposal.get("regionBounds", box)
                if any(_same_occurrence(region, other) for other in seen):
                    continue
                seen.append(region)
                containing = [c for c in self.candidates if c["bounds"] and _contains(_box(c["bounds"]), box)]
                # A tight alignment inside an otherwise coherent native grid is
                # another view of that occurrence. A bounded panel inside a
                # suspect merged grid is independent discovery evidence.
                if containing and any(not assess_geometry(c, words)["suspect"] for c in containing):
                    continue
                candidate = self.alignment_candidate(proposal, words, region=region)
                if proposal.get("regionEvidence"):
                    candidate["regionEvidence"] = proposal["regionEvidence"]
                if containing:
                    candidate["containedInCandidateIds"] = [c["id"] for c in containing]
                    candidate["issues"].append({"kind": "contained-region-proposal"})
                self.add(candidate)

    def ruled_regions(self, page, words):
        """Propose closed subregions from all four sides, not from a gutter alone."""
        if not words:
            return []
        height = median(w["bbox"][3] - w["bbox"][1] for w in words)
        horizontal, vertical = [], []
        for edge in page.edges:
            x0, x1 = edge["x0"] - page.bbox[0], edge["x1"] - page.bbox[0]
            top, bottom = edge["top"] - page.bbox[1], edge["bottom"] - page.bbox[1]
            if edge.get("orientation") == "h" and x1 > x0:
                horizontal.append((x0, x1, (top + bottom) / 2))
            elif edge.get("orientation") == "v" and bottom > top:
                vertical.append(((x0 + x1) / 2, top, bottom))
        rectangles = []
        for left, right, bottom in horizontal:
            self.check_work()
            if right - left < height * 3:
                continue
            sides = [[(begin, finish) for x, begin, finish in vertical if abs(x - side) <= 2 and bottom <= finish + 2]
                     for side in (left, right)]
            if any(not side for side in sides):
                continue
            starts = []
            for start, end, top in horizontal:
                if not (start <= left + 2 and right <= end + 2 and bottom - top >= height * 3):
                    continue
                if all(any(begin <= top + 2 for begin, finish in side) for side in sides):
                    starts.append(top)
            if starts:
                rectangle = [left, max(starts), right, bottom]
                if not any(_same_occurrence(rectangle, other) for other in rectangles):
                    rectangles.append(rectangle)
        return rectangles

    def alignment_candidate(self, proposal, words, region=None):
        rows, cells = [], []
        for top, bottom in proposal["rowBands"]:
            rows.append([])
            cells.append([])
            for left, right in proposal["columnIntervals"]:
                rectangle = [left, top, right, bottom]
                assigned = sorted((w for w in words if _contains(rectangle, w["bbox"])), key=lambda w: (w["bbox"][0], w["id"]))
                rows[-1].append(" ".join(w["text"] for w in assigned))
                cells[-1].append(rectangle)
        if proposal.get("spanningHeading") and region:
            top, bottom = proposal["rowBands"][0]
            rows[0] = [proposal["spanningHeading"]] + [None] * (len(rows[0]) - 1)
            cells[0] = [[region[0], top, region[2], bottom] for _ in cells[0]]
        candidate = candidate_from_rows(self.next_id(), self.page, region or proposal["bounds"], rows,
                                        "text-alignment", cell_bounds=cells, words=words)
        candidate.update(weakProposal=True, rowBandKind="ink", columnIntervalKind="alignment")
        return candidate

    def complete_native(self):
        paths = [f"text/page_{self.page:04d}.native.txt", f"text/page_{self.page:04d}.json"]
        if self.candidates:
            paths.append(self.candidate_path)
        self.units[f"p{self.page:04d}:native"].update(state="completed", evidencePaths=paths)

    def ocr_candidate(self, page, table, model):
        box = _normalized_box(table.get("bounds", table.get("bbox")), page)
        candidate = candidate_from_rows(self.next_id(), self.page, box, table["rows"], "model-transcription")
        candidate["model"] = model
        self.add(candidate)

    def model(self, kind, source, image, prompt, unit):
        self.check_work()
        cap = self.policy["maxModelCalls"]
        if cap is not None and self.calls >= cap:
            unit["reason"] = "table-call-budget"
            raise ServiceError("TABLE_CALL_BUDGET", "Table call cap exhausted.")
        self.calls += 1
        response = self.context.model({"kind": kind, "sourceUnit": source, "prompt": prompt,
                                       "promptVersion": RECOVERY_VERSIONS["confirmationPrompt"]},
                                      [{"path": str((self.output / image).resolve()), "mediaType": "image/png"}])
        text = response.get("text")
        if not isinstance(text, str):
            raise ServiceError("MODEL_OUTPUT_INVALID", "Table response must contain text.")
        if len(text.encode()) > self.limits["responseBytes"]:
            raise ServiceError("RESOURCE_LIMIT", "Table response exceeds the response-byte limit.")
        metadata = {k: response[k] for k in ("requestedModel", "returnedModel", "sessionId", "usage", "operationId") if k in response}
        return text, metadata

    def handle_error(self, exc, unit):
        from .pdf import _raise_if_fatal
        _raise_if_fatal(exc)
        code = getattr(exc, "code", type(exc).__name__)
        unit.update(state="pending" if code in {"WORK_BUDGET_EXHAUSTED", "TABLE_CALL_BUDGET", "TABLE_EVIDENCE_HEADROOM"} else "failed", reason=code)
        if code == "WORK_BUDGET_EXHAUSTED":
            self._cutoff = True
        self.emit("budget-exhausted" if unit["state"] == "pending" else "confirmation-failed", page=unit["source"]["page"], reason=code)

    def scout(self, page, reasons):
        from .pdf import bounds
        unit = self.unit(f"p{self.page:04d}:scout", "table-scout", self.page, dependencies=[f"p{self.page:04d}:native"])
        unit["triggers"] = reasons
        if unit["state"] == "completed":
            return
        path = f"tables/evidence/page_{self.page:04d}.scout.png"
        raw = path.replace(".png", ".response.txt")
        try:
            self.check_work()
            if self.policy["maxModelCalls"] is not None and self.calls >= self.policy["maxModelCalls"]:
                raise ServiceError("TABLE_CALL_BUDGET", "Table call cap exhausted before scouting.")
            rendered = self.renderer.render(self.page - 1, self.output / path, media_box=page.page_obj.mediabox)
            source = {"page": self.page, "bounds": bounds(page, page.bbox)}
            unit.update(render=rendered, source=source, evidencePaths=[path])
            self.adopt_product(path, "table-image", source=source, render=rendered)
            text, model = self.model("table-scout", {"page": self.page}, path, SCOUT_PROMPT, unit)
            unit["model"] = model
            self.replace_product(raw)
            write_text(self.result, self.output, raw, text, "table-model-response", source={"page": self.page}, model=model)
            unit["evidencePaths"] = [path, raw]
            data = json.loads(text)
            if not isinstance(data.get("tables"), list):
                raise TypeError("Scout tables must be an array")
            regions = []
            for region in data["tables"]:
                box = _normalized_box(region.get("bounds"), page)
                orientation = region.get("orientation", 0)
                if box is None or type(orientation) is not int or orientation not in (0, 90, 180, 270):
                    raise ValueError("Invalid scout bounds/orientation")
                if not any(all(abs(a - b) <= 2 for a, b in zip(box, other["box"])) and orientation == other["orientation"] for other in regions):
                    regions.append({"box": box, "orientation": orientation})
            if any(_intersection(a["box"], b["box"]) > 0 for index, a in enumerate(regions) for b in regions[index + 1:]):
                raise ValueError("Scout returned overlapping physical occurrences")
            regions.sort(key=lambda region: (region["box"][1], region["box"][0], region["box"][3], region["box"][2]))
            self.refine_regions(page, regions)
            unit.update(state="completed")
            unit["regions"] = data["tables"]
            self.discovery["scoutedPages"] += 1
        except Exception as exc:  # noqa: BLE001 — isolate model/parser failures; fatal errors propagate
            self.handle_error(exc, unit)

    def refine_regions(self, page, regions):
        """Scout occurrences replace overlapping proposals; original readings remain evidence."""
        from .pdf import bounds
        originals = [c for c in self.candidates if not c.get("superseded") and c["method"] not in {"visual", "tile-aggregation", "geometry"}]
        replacements = {}
        staged = []
        for index, region in enumerate(regions, 1):
            self.check_work()
            box, orientation = region["box"], region["orientation"]
            related = [c for c in originals if c["bounds"] is not None and _intersection(_box(c["bounds"]), box) > 0]
            unlocated = [c for c in originals if c["bounds"] is None]
            region_id = f"scout_p{self.page:04d}_{index:04d}"
            candidates = []
            if orientation == 0 and self.base_words:
                crop_box = [max(page.bbox[0], box[0] + page.bbox[0] - 1), max(page.bbox[1], box[1] + page.bbox[1] - 1),
                            min(page.bbox[2], box[2] + page.bbox[0] + 1), min(page.bbox[3], box[3] + page.bbox[1] + 1)]
                for table in page.crop(crop_box).find_tables():
                    rows = table.extract() or []
                    if not rows:
                        continue
                    cells = [[None if cell is None else bounds(page, cell)["bbox"] for cell in row.cells] for row in table.rows]
                    candidate = candidate_from_rows(self.next_id(), self.page, bounds(page, table.bbox), rows,
                                                    "native-text-layer", cell_bounds=cells, words=self.base_words)
                    candidates.append(candidate)
                if not candidates:
                    local_words = [w for w in self.base_words if _contains(box, w["bbox"])]
                    for proposal in propose_regions(local_words, self.page, min_rows=2):
                        candidates.append(self.alignment_candidate(proposal, self.base_words, region=box))
            if not candidates and len(regions) == 1 and len(unlocated) == 1:
                candidate = copy.deepcopy(unlocated[0])
                candidate.update(id=self.next_id(), bounds=box, parentId=unlocated[0]["id"])
                candidates.append(candidate)
            if not candidates:
                # Reuse an exactly located native/OCR proposal only as a baseline;
                # a merged proposal cannot be copied into each child table.
                exact = [c for c in related if all(abs(a - b) <= 2 for a, b in zip(_box(c["bounds"]), box))]
                for parent in exact:
                    candidate = copy.deepcopy(parent)
                    candidate.update(id=self.next_id(), parentId=parent["id"])
                    candidates.append(candidate)
            if not candidates:
                candidate = candidate_from_rows(self.next_id(), self.page, box, [], "scout")
                candidate["weakProposal"] = True
                candidates.append(candidate)
            multiple_physical_regions = any(_intersection(_box(a["bounds"]), _box(b["bounds"])) == 0
                for index, a in enumerate(candidates) for b in candidates[index + 1:])
            for candidate in candidates:
                candidate.update(orientation=orientation, physicalRegionId=region_id, occurrenceBounds=box,
                                 relatedCandidateIds=[c["id"] for c in related + unlocated])
                if multiple_physical_regions:
                    candidate["issues"].append({"kind": "scout-region-still-merges-tables"})
                staged.append(candidate)
            for parent in related + unlocated:
                replacements.setdefault(parent["id"], []).extend(c["id"] for c in candidates)
        for candidate in staged:
            self.add(candidate)
        for parent in originals:
            if parent["id"] in replacements:
                parent.update(superseded=True, refinedCandidateIds=replacements[parent["id"]],
                              refinementReason="scout-physical-occurrences")
        self.persist_candidates()

    def groups(self):
        active = [c for c in self.candidates if not c.get("superseded") and c["method"] not in {"visual", "tile-aggregation", "geometry"}]
        unrefined = [c for c in active if not c.get("physicalRegionId")]
        by_id = {c["id"]: c for c in unrefined}
        roots = {}
        for candidate in unrefined:
            root, seen = candidate, {candidate["id"]}
            while root.get("parentId") in by_id and root["parentId"] not in seen:
                parent = by_id[root["parentId"]]
                if parent["page"] != candidate["page"] or not _same_occurrence(parent["bounds"], candidate["bounds"]):
                    break
                root = parent
                seen.add(root["id"])
            roots[candidate["id"]] = root["id"]
        groups = group_candidates([c for c in unrefined if roots[c["id"]] == c["id"]])
        for candidate in unrefined:
            if roots[candidate["id"]] != candidate["id"]:
                group = next(g for g in groups if roots[candidate["id"]] in g["candidateIds"])
                group["candidateIds"].append(candidate["id"])
        refined = {}
        for candidate in active:
            identity = candidate.get("physicalRegionId")
            if identity:
                group = refined.setdefault(identity, {"id": identity, "page": self.page, "bounds": candidate["occurrenceBounds"],
                    "candidateIds": [], "ambiguousWith": [], "issues": []})
                group["candidateIds"].append(candidate["id"])
                if any(i["kind"] == "scout-region-still-merges-tables" for i in candidate["issues"]):
                    group["ambiguousWith"] = [identity]
        return groups + list(refined.values())

    def occurrence_identity(self, group):
        """Resume identity follows physical bounds, never a new proposal's list index."""
        matches = []
        current = _box(group["bounds"])
        if self.page not in self.invalidated_pages and current:
            for table in self.previous.get("tables", []):
                prior = _box(table.get("source", {}).get("bounds"))
                if table.get("source", {}).get("page") != self.page or table["id"] in self.claimed_table_ids or not prior:
                    continue
                intersection = _intersection(current, prior)
                union = ((current[2] - current[0]) * (current[3] - current[1]) +
                         (prior[2] - prior[0]) * (prior[3] - prior[1]) - intersection)
                if all(abs(a - b) <= 2 for a, b in zip(current, prior)) or intersection / union >= .8:
                    matches.append(table["id"])
        if len(matches) == 1:
            identity = matches[0]
        else:
            self.table_serial += 1
            identity = f"table_p{self.page:04d}_{self.table_serial:04d}"
            if matches:
                group["ambiguousWith"].append("ambiguous-resume-occurrence")
        self.claimed_table_ids.add(identity)
        return identity

    def adopt_product(self, path, kind, **extra):
        existing = next((p for p in self.result["products"] if p["path"] == path), None)
        if existing:
            existing.update(kind=kind, mediaType="image/png", **extra)
        else:
            product(self.result, path, kind, "image/png", **extra)

    def resolve_page(self, page, ocr_inspected):
        from .pdf import bounds
        self.media_box = list(page.page_obj.mediabox)
        work_available = self.available()
        force = self.page in self.policy["forcePages"] or self.policy["geometry"] == "force"
        groups = self.groups()
        reasons = []
        if force:
            reasons.append("forced-page")
            self.discovery["forcedPages"] += 1
        if any(c["bounds"] is None for c in self.candidates):
            reasons.append("missing-bounds")
        if any(g["ambiguousWith"] for g in groups):
            reasons.append("overlapping-regions")
        if self.policy["geometry"] != "off" and any(c.get("containedInCandidateIds") for c in self.candidates if not c.get("superseded")):
            reasons.append("contained-region-proposals")
        if work_available and any(c["method"] == "native-text-layer" and
                                 (assess_geometry(c)["suspect"] or any(i["kind"] == "alternate-extraction-failed" for i in c["issues"]))
                                 for c in self.candidates if not c.get("superseded")):
            reasons.append("suspect-native-geometry")
        for image in page.images:
            box = bounds(page, [image["x0"], image["top"], image["x1"], image["bottom"]])["bbox"]
            inspected = ocr_inspected and not any(c["bounds"] is None and not c.get("superseded") for c in self.candidates)
            planned_read = self.policy["vision"] == "confirm-all" and any(c["bounds"] and _contains(_box(c["bounds"]), box) for c in self.candidates if not c.get("superseded"))
            self.images.append({"bounds": box, "disposition": "inspected" if inspected else "pending", "plannedTableRead": planned_read})
        if any(i["disposition"] == "pending" and not i["plannedTableRead"] for i in self.images):
            reasons.append("unclaimed-images")
        if not self.candidates and getattr(page, "edges", []):
            reasons.append("ruling-without-region")
        if reasons and self.policy["vision"] != "off":
            self.scout(page, reasons)
            scout = self.units[f"p{self.page:04d}:scout"]
            for image in self.images:
                if image["disposition"] == "pending" and scout["state"] == "completed":
                    image["disposition"] = "inspected" if any(_intersection(image["bounds"], _normalized_box(region["bounds"], page)) > 0 for region in scout.get("regions", [])) else "non-table"
        elif self.policy["vision"] == "off":
            for image in self.images:
                if image["disposition"] == "pending":
                    image["disposition"] = "opted-out"
        self.discovery["inspectedPages"] += 1
        work_available = self.available()
        groups = self.groups()
        by_id = {c["id"]: c for c in self.candidates}
        for group in groups:
            identity = self.occurrence_identity(group)
            reused = next((t for t in self.previous.get("tables", []) if t["id"] == identity and
                           self.units.get(identity + ":confirmation", {}).get("state") == "completed"), None)
            if reused:
                self.result["tables"].append(copy.deepcopy(reused))
                continue
            candidates = [by_id[c] for c in group["candidateIds"]]
            baseline = next((c for c in candidates if c["method"] == "native-text-layer"), candidates[0])
            geometry_frame = {"pageSize": [page.width, page.height], "orientation": baseline.get("orientation", 0)}
            baseline_view = self.comparison_view(baseline, geometry_frame)
            assessment = assess_geometry(baseline_view) if work_available else {"suspect": True, "triggers": ["work-budget-exhausted"], "baselineBands": []}
            recovered = {"state": "unchanged", "candidate": baseline_view, "triggers": assessment["triggers"]}
            if work_available and self.policy["geometry"] != "off" and baseline["words"]:
                recovered = recover_geometry(baseline_view, id=self.next_id())
            if work_available and self.policy["geometry"] != "off" and baseline["method"] in {"native-text-layer", "line-text", "text-alignment"} and baseline["rowCount"] and not assessment["baselineBands"]:
                recovered["state"] = "unresolved"
                recovered["triggers"].append("missing-native-occurrence-backing")
            if any(issue["kind"] == "alternate-extraction-failed" for c in candidates for issue in c["issues"]):
                recovered["state"] = "unresolved"
                recovered["triggers"].append("alternate-extraction-failed")
            if baseline.get("weakProposal") and baseline["method"] == "text-alignment":
                recovered["state"] = "unresolved"
                recovered["triggers"].append("low-evidence-alignment-proposal")
            if geometry_frame["orientation"] and baseline["rowCount"]:
                if recovered["candidate"]["id"] == baseline["id"]:
                    recovered["candidate"].update(id=self.next_id(), parentId=baseline["id"], method="orientation-native")
                if recovered["state"] == "unchanged":
                    recovered["state"] = "recovered"
                recovered["triggers"].append("quarter-turn-logical-grid")
            recovered["candidate"] = self.physical_candidate(recovered["candidate"], geometry_frame)
            if recovered["candidate"]["id"] != baseline["id"]:
                self.add(recovered["candidate"])
                candidates.append(recovered["candidate"])
            selected = recovered["candidate"]
            required = self.policy["vision"] == "confirm-all" or (self.policy["vision"] == "on-demand" and (assessment["suspect"] or force or baseline["method"] == "scout" or baseline.get("weakProposal")))
            old = self.old_units.get(identity + ":confirmation", {})
            dependencies = [f"p{self.page:04d}:native"]
            if baseline.get("physicalRegionId"):
                dependencies.append(f"p{self.page:04d}:scout")
            unit = self.unit(identity + ":confirmation", "table-confirmation", self.page,
                             dependencies=dependencies, passesSpent=old.get("passesSpent", 0),
                             activePass=old.get("activePass"), regionId=identity,
                             readPasses=copy.deepcopy(old.get("readPasses", {})))
            unit["evidencePaths"] = [self.candidate_path]
            item = {"id": identity, "page": self.page, "bounds": group["bounds"], "mediaBox": self.media_box,
                    "candidates": candidates, "selected": selected, "recovery": recovered, "comparisons": [],
                    "confirmation": {"state": "pending" if required else "not_checked", "basis": None},
                    "required": required, "unit": unit, "orientation": baseline.get("orientation", 0),
                    "paths": [], "ambiguity": group["ambiguousWith"], "pageSize": [page.width, page.height]}
            previous_table = next((t for t in self.previous.get("tables", []) if t["id"] == identity), None)
            if previous_table and old:
                decision_path = previous_table.get("evidencePaths", {}).get("decisions")
                if decision_path and (self.output / decision_path).is_file():
                    history = json.loads((self.output / decision_path).read_text())
                    item["comparisons"] = history.get("comparisons", [])
                    item["paths"] = history.get("evidencePaths", [])
            item["weakProposal"] = baseline.get("weakProposal", False)
            self.pending.append(item)
            self.page_tables.append(item)
            if not required:
                unit.update(state="not_applicable", reason="vision-policy")
                continue
            if group["bounds"] is None:
                unit.update(state="pending", reason="missing-bounds")
                continue
            if not work_available:
                unit.update(state="pending", reason="WORK_BUDGET_EXHAUSTED")
                continue
            if selected["rowCount"] == 0:
                first = self.read(item, "extraction", "table-extraction")
                if first:
                    selected = first
                    item["selected"] = first
                    if not first["tablePresent"] and baseline.get("weakProposal"):
                        item["rejected"] = True
                        unit.update(state="completed", reason="explicit-non-table")
                        self.discovery["rejectedProposals"] += 1
            if not item.get("rejected") and (selected["rowCount"] or baseline["method"] == "scout"):
                visual = self.read(item, "confirmation", "table-confirmation")
                if visual:
                    self.compare(item, visual)
            if unit["state"] in {"pending", "failed"} and item["confirmation"]["state"] != "agreed":
                item["confirmation"]["state"] = unit["state"]
        self.persist_candidates()
        for image in self.images:
            for candidate in self.candidates:
                render = candidate.get("imageTransform")
                if render and _whole_read(candidate) and _contains(render["bbox"], image["bounds"]):
                    rejected = any(item.get("rejected") and item["bounds"] and _contains(_box(item["bounds"]), image["bounds"]) for item in self.page_tables)
                    image["disposition"] = "covered-by-table-read" if candidate["tablePresent"] else "non-table" if rejected else "inspected"
                    break
        audit_path = f"tables/evidence/page_{self.page:04d}.discovery.json"
        scout_unit = self.units.get(f"p{self.page:04d}:scout")
        unresolved = bool(any(c["bounds"] is None and not c.get("superseded") for c in self.candidates) or
                          any(g["ambiguousWith"] for g in groups) or scout_unit and scout_unit["state"] != "completed")
        self.replace_product(audit_path)
        write_json(self.result, self.output, audit_path, {"source": {"page": self.page}, "groups": groups,
                   "images": self.images, "triggers": reasons}, "table-discovery", source={"page": self.page})
        discovery_unit = self.units[f"p{self.page:04d}:discovery"]
        discovery_unit.update(state="unresolved" if unresolved else "completed", evidencePaths=[audit_path] + ([self.candidate_path] if self.candidates else []))
        if not work_available:
            discovery_unit.update(state="pending", reason="WORK_BUDGET_EXHAUSTED")
        if scout_unit:
            discovery_unit["dependencies"] = list(dict.fromkeys(discovery_unit["dependencies"] + [scout_unit["id"]]))
        self.discovery["unresolvedRegions"] += int(unresolved)
        self.emit("discovery", page=self.page, count=len(groups))

    def comparison_view(self, candidate, item):
        """Compare logical grids in a common upright, page-sized point space."""
        view = copy.deepcopy(candidate)
        forward, _ = _upright_transforms(item["pageSize"], item["orientation"])
        geometry = candidate.get("uprightGeometry")
        if geometry:
            view.update(bounds=geometry["bounds"], rowBands=geometry["rowBands"],
                        columnIntervals=geometry["columnIntervals"])
            for cell in view["cells"]:
                cell["bbox"] = cell.get("uprightBBox")
        else:
            if view["bounds"] is not None:
                view["bounds"] = _map_box(forward, _box(view["bounds"]))
            for cell in view["cells"]:
                if cell["bbox"] is not None:
                    cell["bbox"] = _map_box(forward, cell["bbox"])
            orientation = item["orientation"]
            if orientation and view["rowCount"]:
                nr, nc = view["rowCount"], view["columnCount"]
                cells = []
                for cell in view["cells"]:
                    r, c, rs, cs = (cell[k] for k in ("row", "column", "rowSpan", "columnSpan"))
                    position = {90: (c, nr - r - rs + 2), 180: (nr - r - rs + 2, nc - c - cs + 2),
                                270: (nc - c - cs + 2, r)}[orientation]
                    cells.append(cell | {"row": position[0], "column": position[1],
                                         "rowSpan": cs if orientation in (90, 270) else rs,
                                         "columnSpan": rs if orientation in (90, 270) else cs})
                validated = self.validate_transcript({"tablePresent": view["tablePresent"],
                    "rowCount": nc if orientation in (90, 270) else nr,
                    "columnCount": nr if orientation in (90, 270) else nc, "cells": cells},
                    view["id"], view["page"], view["bounds"])
                if not validated["valid"]:
                    raise ServiceError("MODEL_OUTPUT_INVALID", "Native anchors cannot be rotated into a logical grid.")
                grid = validated["candidate"]
                for key in ("rowCount", "columnCount", "rows", "logicalRows", "grid"):
                    view[key] = grid[key]
                view["cells"] = cells
                row_bands, columns = candidate["rowBands"], candidate["columnIntervals"]
                width, height = item["pageSize"]
                if orientation == 90:
                    view["rowBands"] = copy.deepcopy(columns)
                    view["columnIntervals"] = [[height - end, height - start] for start, end in reversed(row_bands)]
                elif orientation == 180:
                    view["rowBands"] = [[height - end, height - start] for start, end in reversed(row_bands)]
                    view["columnIntervals"] = [[width - end, width - start] for start, end in reversed(columns)]
                else:
                    view["rowBands"] = [[width - end, width - start] for start, end in reversed(columns)]
                    view["columnIntervals"] = copy.deepcopy(row_bands)
        for word in view["words"]:
            word["bbox"] = _map_box(forward, word["bbox"])
        return view

    def physical_candidate(self, view, item):
        """Keep upright occurrence bands and materialize physical cell/row references."""
        candidate = copy.deepcopy(view)
        _, inverse = _upright_transforms(item["pageSize"], item["orientation"])
        region = _box(view["bounds"])
        if region is None:
            return candidate
        candidate["uprightGeometry"] = {"bounds": region, "rowBands": copy.deepcopy(view["rowBands"]),
            "columnIntervals": copy.deepcopy(view["columnIntervals"]), "orientation": item["orientation"],
            "uprightToPage": inverse}
        candidate["bounds"] = _map_box(inverse, region)
        candidate["physicalRowBands"] = [_map_box(inverse, [region[0], start, region[2], end]) for start, end in view["rowBands"]]
        for cell in candidate["cells"]:
            cell["uprightBBox"] = cell["bbox"]
            if cell["bbox"] is not None:
                cell["bbox"] = _map_box(inverse, cell["bbox"])
        for word in candidate["words"]:
            word["bbox"] = _map_box(inverse, word["bbox"])
        if item["orientation"]:
            candidate["rowBands"], candidate["columnIntervals"] = [], []
        return candidate

    def support_bands(self, item, maximum):
        """Propose new complete-row frames from positions, never from equal cell text."""
        observations = []
        for candidate in item["candidates"]:
            if candidate["method"] not in {"visual", "tile-aggregation"}:
                continue
            view = self.comparison_view(candidate, item)
            tile = candidate.get("transcriptScope", {}).get("uprightTileBounds")
            for band in view["rowBands"]:
                clipped = bool(tile and (band[0] <= tile[1] + .01 or band[1] >= tile[3] - .01))
                observations.append({"band": band, "clipped": clipped})
        proposals = [o["band"] for o in observations if not o["clipped"]]
        for index, first in enumerate(observations):
            if not first["clipped"]:
                continue
            for second in observations[index + 1:]:
                a, b = first["band"], second["band"]
                if second["clipped"] and min(a[1], b[1]) > max(a[0], b[0]):
                    proposals.append([min(a[0], b[0]), max(a[1], b[1])])
        # Oversized unions are uncertain proposals, not proof of an oversized
        # physical row. Retain their original reads; do not downsample or guess.
        return sorted({tuple(b) for b in proposals if b[1] - b[0] + 2 < maximum})

    def read_plan(self, item, pass_id, kind):
        stored = item["unit"].setdefault("readPasses", {})
        if pass_id in stored:
            return stored[pass_id]
        box = _box(item["bounds"])
        if kind == "table-reread":
            box = _union([box] + [_box(c["bounds"]) for c in item["candidates"] if c["bounds"] is not None])
        margin = 3 if pass_id != "extraction" else 5
        width, height = item["pageSize"]
        physical_frame = [max(0, box[0] - margin), max(0, box[1] - margin), min(width, box[2] + margin), min(height, box[3] + margin)]
        forward, inverse = _upright_transforms(item["pageSize"], item["orientation"])
        framed = _map_box(forward, physical_frame)
        scale = self.policy["renderDpi"] / 72
        max_height = math.floor(self.limits["pixels"] / math.ceil((framed[2] - framed[0]) * scale)) / scale - 1
        if max_height <= 0:
            raise ServiceError("RESOURCE_LIMIT", "Table is too wide for target DPI.")
        if framed[3] - framed[1] > max_height:
            try:
                bands = self.comparison_view(item["selected"], item)["rowBands"]
                if kind == "table-reread" and self.support_bands(item, max_height):
                    bands = []
                plan = plan_tiles(framed, max_height=max_height, overlap=min(36, max_height / 4), row_bands=bands)
                if kind == "table-reread":
                    for index, (start, end) in enumerate(self.support_bands(item, max_height), 1):
                        if any(tile["bounds"][1] + .5 < start and end < tile["bounds"][3] - .5 for tile in plan["tiles"]):
                            continue
                        top = max(framed[1], min((start + end - max_height) / 2, framed[3] - max_height))
                        plan["tiles"].append({"id": f"support_{index:04d}", "bounds": [framed[0], top, framed[2], top + max_height]})
                    plan["tiles"].sort(key=lambda tile: (tile["bounds"][1], tile["bounds"][3], tile["id"]))
                # A row-support tile can fully subsume an original strip. Omit
                # only contained strips: coverage and complete row reads survive,
                # and opposing clipped fragments cannot form a spurious seam.
                plan["tiles"] = [tile for tile in plan["tiles"] if not any(
                    other["id"] != tile["id"] and _contains(other["bounds"], tile["bounds"])
                    and (other["bounds"] != tile["bounds"] or other["id"] < tile["id"])
                    for other in plan["tiles"])]
                plan["seams"] = []
                for first, second in zip(plan["tiles"], plan["tiles"][1:]):
                    overlap = [max(first["bounds"][1], second["bounds"][1]), min(first["bounds"][3], second["bounds"][3])]
                    plan["seams"].append({"tileIds": [first["id"], second["id"]], "overlap": overlap,
                                          "position": sum(overlap) / 2})
            except ValueError as exc:
                raise ServiceError("RESOURCE_LIMIT", "A complete table row cannot fit at target DPI within the pixel limit.") from exc
        else:
            plan = {"bounds": framed, "tiles": [{"id": "tile_0001", "bounds": framed}], "seams": []}
        for tile in plan["tiles"]:
            tile["sourceBounds"] = _map_box(inverse, tile["bounds"])
        plan.update(margin=margin, physicalFrame=physical_frame, regenerated=kind == "table-reread",
                    supportBands=self.support_bands(item, max_height) if kind == "table-reread" else [])
        stored[pass_id] = plan
        return plan

    def read(self, item, pass_id, kind):
        plan = self.read_plan(item, pass_id, kind)
        reads = []
        units = []
        for tile in plan["tiles"]:
            identity = item["id"] + ":" + pass_id + ":" + tile["id"]
            unit = self.unit(identity, "table-read-tile", item["page"], dependencies=[f"p{item['page']:04d}:native"],
                             passId=pass_id, tile=tile, operationKind=kind)
            units.append(identity)
            if unit["state"] == "completed" and unit.get("candidateId"):
                candidate = next((c for c in self.candidates if c["id"] == unit["candidateId"]), None)
                if candidate:
                    reads.append({"tileId": tile["id"], "candidate": self.comparison_view(candidate, item)})
                    if not any(c["id"] == candidate["id"] for c in item["candidates"]):
                        item["candidates"].append(candidate)
                    continue
            path = f"tables/evidence/{item['id']}.{pass_id}.{tile['id']}.png"
            raw = path.replace(".png", ".response.txt")
            try:
                self.check_work()
                if self.policy["maxModelCalls"] is not None and self.calls >= self.policy["maxModelCalls"]:
                    raise ServiceError("TABLE_CALL_BUDGET", "Table call cap exhausted before rendering.")
                current_bytes = sum((self.output / p["path"]).stat().st_size for p in self.result["products"] if (self.output / p["path"]).is_file())
                if current_bytes >= self.limits["outputBytes"]:
                    raise ServiceError("RESOURCE_LIMIT", "No output headroom remains for required table evidence.")
                if kind == "table-reread" and current_bytes + self.limits["responseBytes"] + self.limits["pixels"] * 4 > self.limits["outputBytes"]:
                    raise ServiceError("TABLE_EVIDENCE_HEADROOM", "Optional read deferred for predictable evidence growth.")
                render = self.renderer.render_region(item["page"] - 1, self.output / path, tile.get("requestedSourceBounds", tile["sourceBounds"]),
                    dpi=self.policy["renderDpi"], orientation=item["orientation"], media_box=item["mediaBox"])
                tile.setdefault("requestedBounds", tile["bounds"])
                tile.setdefault("requestedSourceBounds", tile["sourceBounds"])
                tile["sourceBounds"] = render["bbox"]
                forward, _ = _upright_transforms(item["pageSize"], item["orientation"])
                tile["bounds"] = _map_box(forward, render["bbox"])
                render["cropMargin"] = plan["margin"]
                render["uprightBounds"] = tile["bounds"]
                self.adopt_product(path, "table-image", source={"page": item["page"], "bounds": tile["sourceBounds"]}, render=render)
                paths = [path]
                if render["sourceImage"]:
                    original = str(Path(path).with_name(render["sourceImage"]))
                    render["sourceRender"]["cropMargin"] = plan["margin"]
                    self.adopt_product(original, "table-image", source={"page": item["page"], "bounds": tile["sourceBounds"]}, render=render["sourceRender"])
                    paths.append(original)
                unit["evidencePaths"] = paths + [self.candidate_path]
                item["paths"].extend(paths)
                text, model = self.model(kind, {"page": item["page"], "regionId": item["id"], "bounds": tile["sourceBounds"],
                    "requestedBounds": tile["requestedSourceBounds"], "uprightBounds": tile["bounds"],
                    "orientation": item["orientation"]}, path, READ_PROMPT, unit)
                self.replace_product(raw)
                write_text(self.result, self.output, raw, text, "table-model-response", source={"page": item["page"]}, model=model)
                paths.append(raw)
                unit["evidencePaths"] = paths + [self.candidate_path]
                item["paths"].append(raw)
                validation = self.validate_transcript(text, self.next_id(), item["page"], tile["bounds"], model)
                unit["validation"] = {k: validation[k] for k in ("errors", "discardedEdges")}
                if not validation["valid"]:
                    raise ServiceError("MODEL_OUTPUT_INVALID", "Malformed visual table transcript retained as raw evidence.")
                view = validation["candidate"]
                forward, _ = _upright_transforms(item["pageSize"], item["orientation"])
                for edge_key, axis, pixels, target in (("rowEdges", 1, render["height"], "rowBands"),
                                                       ("columnEdges", 0, render["width"], "columnIntervals")):
                    if edge_key not in view:
                        continue
                    mapped = []
                    for edge in view[edge_key]:
                        x, y = (render["width"] / 2, edge / 1000 * pixels) if axis == 1 else (edge / 1000 * pixels, render["height"] / 2)
                        page_point = _point(render["imageToPage"], x, y)
                        mapped.append(_point(forward, *page_point)[axis])
                    view[target] = [[round(a, 8), round(b, 8)] for a, b in pairwise(mapped)]
                if len(plan["tiles"]) == 1:
                    view["bounds"] = _map_box(forward, _box(item["bounds"]))
                if view["rowBands"] and view["columnIntervals"]:
                    view["bounds"] = [view["columnIntervals"][0][0], view["rowBands"][0][0],
                                      view["columnIntervals"][-1][1], view["rowBands"][-1][1]]
                for cell in view["cells"]:
                    if view["rowBands"] and view["columnIntervals"]:
                        r, c, rs, cs = (cell[k] for k in ("row", "column", "rowSpan", "columnSpan"))
                        cell["bbox"] = [view["columnIntervals"][c - 1][0], view["rowBands"][r - 1][0],
                                        view["columnIntervals"][c + cs - 2][1], view["rowBands"][r + rs - 2][1]]
                clipped = any(b[0] <= tile["bounds"][1] + .01 or b[1] >= tile["bounds"][3] - .01 for b in view["rowBands"])
                view.update(operationKind=kind, imageTransform=render,
                            transcriptScope={"kind": "region" if len(plan["tiles"]) == 1 else "tile",
                                "complete": len(plan["tiles"]) == 1 and not clipped,
                                "passId": pass_id, "uprightTileBounds": tile["bounds"], "requestedRegion": item["bounds"]})
                candidate = self.add(self.physical_candidate(view, item))
                item["candidates"].append(candidate)
                unit.update(state="completed", candidateId=candidate["id"])
                reads.append({"tileId": tile["id"], "candidate": view})
            except Exception as exc:  # noqa: BLE001 — isolate model/render failures; fatal errors propagate
                self.handle_error(exc, unit)
                item["unit"].update(state=unit["state"], reason=unit.get("reason"))
        item["unit"]["dependencies"] = list(dict.fromkeys(item["unit"]["dependencies"] + units))
        if len(reads) != len(plan["tiles"]):
            return None
        plan["operationsCompleted"] = True
        plan["seams"] = []
        for first, second in pairwise(plan["tiles"]):
            overlap = [max(first["bounds"][1], second["bounds"][1]), min(first["bounds"][3], second["bounds"][3])]
            plan["seams"].append({"tileIds": [first["id"], second["id"]], "overlap": overlap,
                                  "position": sum(overlap) / 2})
        if len(reads) == 1:
            view = reads[0]["candidate"]
            aggregate = {"state": "agreed" if _whole_read(view) else "unresolved", "candidate": view,
                         "issues": [] if _whole_read(view) else [{"kind": "edge-only-row"}], "rowSources": []}
        else:
            candidate_id = self.next_id()
            forward, _ = _upright_transforms(item["pageSize"], item["orientation"])
            fallback_bounds = _map_box(forward, _box(item["bounds"]))
            aggregate = aggregate_reads(plan, reads, candidate_id, item["page"], fallback_bounds, self.visual_limits)
        for seam in plan["seams"]:
            if seam["overlap"][0] >= seam["overlap"][1]:
                aggregate["issues"].append({"kind": "rendered-tiles-have-no-overlap", "tileIds": seam["tileIds"]})
                aggregate["state"] = "unresolved"
        seam_units = []
        for index, seam in enumerate(plan["seams"], 1):
            unit = self.unit(item["id"] + ":" + pass_id + f":seam{index:04d}", "tile-seam", item["page"],
                             dependencies=units, state="completed" if aggregate["state"] == "agreed" else "unresolved", seam=seam)
            unit.update(state="completed" if aggregate["state"] == "agreed" else "unresolved", issues=aggregate["issues"])
            seam_units.append(unit["id"])
        aggregation = self.unit(item["id"] + ":" + pass_id + ":aggregation", "table-read-aggregation", item["page"],
                                dependencies=units + seam_units, state="completed" if aggregate["state"] == "agreed" else "unresolved")
        aggregation.update(state="completed" if aggregate["state"] == "agreed" else "unresolved", dependencies=units + seam_units,
                           issues=aggregate["issues"], evidencePaths=[self.candidate_path])
        item["unit"]["dependencies"] = list(dict.fromkeys(item["unit"]["dependencies"] + [aggregation["id"]]))
        item["unit"]["seamIssues"] = aggregate["issues"]
        if aggregate["candidate"]:
            view = aggregate["candidate"]
            if len(reads) > 1:
                view.update(transcriptScope={"kind": "aggregate", "complete": aggregate["state"] == "agreed", "passId": pass_id,
                    "requestedRegion": item["bounds"]}, aggregationIssues=aggregate["issues"], rowSources=aggregate["rowSources"])
            view["readAggregationId"] = aggregation["id"]
            if len(reads) > 1:
                candidate = self.add(self.physical_candidate(view, item))
                item["candidates"].append(candidate)
            else:
                candidate = next(c for c in self.candidates if c["id"] == view["id"])
                candidate["readAggregationId"] = aggregation["id"]
                for entry in item["candidates"]:
                    if entry["id"] == candidate["id"]:
                        entry["readAggregationId"] = aggregation["id"]
                self.persist_candidates()
        if aggregate["state"] != "agreed":
            item["confirmation"]["state"] = "unresolved"
            item["unit"]["state"] = "unresolved"
        return candidate if aggregate["candidate"] else None

    def compare(self, item, visual):
        # A false weak proposal may be rejected only with explicit visual non-table evidence.
        if not visual["tablePresent"] and item["weakProposal"]:
            item["rejected"] = True
            item["unit"].update(state="completed", reason="explicit-non-table")
            self.discovery["rejectedProposals"] += 1
            return
        for baseline in item["candidates"]:
            if baseline["id"] == visual["id"] or not baseline["rowCount"] or not _whole_read(baseline) or not _whole_read(visual):
                continue
            comparison = compare_candidates(self.comparison_view(baseline, item), self.comparison_view(visual, item))
            item["comparisons"].append({"leftCandidateId": baseline["id"], "rightCandidateId": visual["id"], **comparison})
            native = baseline["method"] in {"geometry", "orientation-native", "native-text-layer", "line-text", "text-alignment"}
            stale_native = native and item["recovery"]["state"] == "recovered" and baseline["id"] != item["recovery"]["candidate"]["id"]
            visual_pair = baseline["method"] in {"visual", "tile-aggregation"} and visual["method"] in {"visual", "tile-aggregation"}
            prior_pass = baseline.get("transcriptScope", {}).get("passId")
            current_pass = visual.get("transcriptScope", {}).get("passId")
            fresh_pair = visual_pair and (not prior_pass or not current_pass or prior_pass != current_pass)
            weak_only = native and item.get("weakProposal") and item["recovery"]["triggers"] == ["low-evidence-alignment-proposal"]
            resolvable = item["recovery"]["state"] != "unresolved" or fresh_pair or weak_only
            if comparison["agreed"] and not stale_native and resolvable and not item["ambiguity"] and not item["unit"].get("seamIssues"):
                basis = "native-vision" if native else "ocr-vision" if baseline["method"] == "model-transcription" else "repeat-vision"
                if fresh_pair:
                    item.setdefault("recoveryHistory", copy.deepcopy(item["recovery"]))
                    item["nativeMismatchHistory"] = [c for c in item["comparisons"] if not c["agreed"]]
                    item["recovery"] = {"state": "recovered", "candidate": baseline,
                                        "triggers": ["visual-structure-correction"]}
                elif weak_only:
                    item["recovery"] = {"state": "recovered", "candidate": baseline,
                                        "triggers": ["visually-supported-alignment"]}
                item["selected"] = baseline
                item["confirmation"] = {"state": "agreed", "basis": basis}
                item["unit"]["state"] = "completed"
                decisive = {c["readAggregationId"] for c in (baseline, visual) if c.get("readAggregationId")}
                item["unit"]["dependencies"] = [d for d in item["unit"].get("dependencies", []) if d.endswith((":native", ":scout"))] + sorted(decisive)
                for unit in self.units.values():
                    if unit["id"].startswith(item.get("id", "\0") + ":") and unit["kind"] in {"table-read-aggregation", "tile-seam", "table-read-tile"} and unit["state"] not in {"completed", "superseded"}:
                        unit.update(state="superseded", reason="Resolved by complete independent region read", resolvedByUnits=sorted(decisive))
                self.emit("confirmation", page=item["page"], candidateId=baseline["id"], state="agreed")
                return
        item["confirmation"] = {"state": "unresolved", "basis": None}
        item["unit"]["state"] = "unresolved"
        if visual["rowCount"] and (not item["selected"]["rowCount"] or
                                   not _whole_read(item["selected"]) and _whole_read(visual)):
            item["selected"] = visual
        self.emit("disagreement", page=item["page"], candidateId=visual["id"])

    def figure_units(self, figure):
        for kind, status, key in (("figure-ocr", "ocrStatus", "ocrPath"), ("figure-description", "interpretationStatus", "interpretationPath")):
            state = figure.get(status, "not_processed")
            paths = [figure[k] for k in ("imagePath", key) if figure.get(k)]
            self.unit(figure["id"] + ":" + kind, kind, figure["source"]["page"],
                      dependencies=[f"p{figure['source']['page']:04d}:figure-detection"], paths=paths,
                      state="completed" if state == "succeeded" else "not_applicable" if state == "not_applicable" else "pending" if state == "pending" else "failed")

    def end_page(self, record):
        self.records[self.page] = record
        # Deferred passes retain paths, not the document's complete word/cell
        # payloads. Finalization loads one affected occurrence at a time.
        deferred = {}
        for item in self.page_tables:
            path = f"tables/evidence/{item['id']}.work.json"
            self.replace_product(path)
            write_json(self.result, self.output, path, item, "table-work-evidence", source={"page": self.page})
            deferred[item["id"]] = {"id": item["id"], "page": self.page, "workPath": path}
            item["unit"]["evidencePaths"].append(path)
        self.pending = [deferred.get(item["id"], item) for item in self.pending]
        self.page_tables = []
        for kind in ("page-ocr", "figure-detection"):
            unit = self.units[f"p{self.page:04d}:" + kind]
            paths = [p["path"] for p in self.result["products"] if p.get("source", {}).get("page") == self.page and
                     p["kind"] in ({"page-image", "page-ocr", "page-ocr-response"} if kind == "page-ocr" else {"figure-detection"})]
            unit.update(evidencePaths=paths)
            status = record.get("ocrStatus") if kind == "page-ocr" else record.get("figureDetectionStatus", "failed")
            unit["state"] = "completed" if status == "succeeded" else "not_applicable" if status is None else "pending" if status == "pending" else "failed"
            if status == "pending":
                unit["reason"] = "WORK_BUDGET_EXHAUSTED"

    def finish(self, write_table):
        # First reads for all pages have already run. Only now spend optional allowances.
        for deferred in self.pending:
            item = json.loads((self.output / deferred["workPath"]).read_text())
            item["unit"] = self.units[item["unit"]["id"]]
            self.page = item["page"]
            self.candidate_path = f"tables/evidence/page_{self.page:04d}.candidates.jsonl"
            self.candidates = [json.loads(line) for line in (self.output / self.candidate_path).read_text().splitlines()] if (self.output / self.candidate_path).is_file() else []
            self.serial = max((int(c["id"].rsplit("_", 1)[-1]) for c in self.candidates), default=0)
            unit = item["unit"]
            if item["confirmation"]["state"] == "unresolved" and not item.get("rejected"):
                while unit.get("activePass") or unit.get("passesSpent", 0) < self.policy["maxAdditionalPasses"]:
                    if not self.available() or self.policy["maxModelCalls"] is not None and self.calls >= self.policy["maxModelCalls"]:
                        break
                    if not unit.get("activePass"):
                        unit["passesSpent"] = unit.get("passesSpent", 0) + 1
                        unit["activePass"] = "dispute_" + str(unit["passesSpent"])
                        self.passes += 1
                    visual = self.read(item, unit["activePass"], "table-reread")
                    if visual:
                        unit["activePass"] = None
                        self.compare(item, visual)
                    else:
                        if unit["readPasses"][unit["activePass"]].get("operationsCompleted"):
                            unit["activePass"] = None
                            continue
                        break
                    if item["confirmation"]["state"] == "agreed":
                        break
            if item.get("rejected"):
                continue
            selected = item["selected"]
            if not selected["rowCount"]:
                unit["state"] = "unresolved" if unit["state"] == "completed" else unit["state"]
                self.discovery["unresolvedRegions"] += 1
                continue
            # Candidate validation precedes any canonical writer; rows are never counted for alternatives.
            rows = selected["logicalRows"] if selected["method"] != "native-text-layer" or any(
                c["rowSpan"] > 1 or c["columnSpan"] > 1 for c in selected["cells"]) else selected["rows"]
            if any(not isinstance(row, list) or any(v is not None and not isinstance(v, str) for v in row) for row in rows):
                raise ServiceError("MODEL_OUTPUT_INVALID", "Selected table rows are invalid.")
            self.persist_candidates()
            for path in (f"tables/{item['id']}.jsonl", f"tables/{item['id']}.csv"):
                self.replace_product(path)
            source_bounds = copy.deepcopy(item["bounds"])
            if isinstance(source_bounds, dict):
                source_bounds["bbox"] = _box(selected["bounds"])
            else:
                source_bounds = _box(selected["bounds"])
            table = write_table(rows, {"page": item["page"], "bounds": source_bounds}, selected["method"], selected.get("model"), table_id=item["id"])
            structure = f"tables/evidence/{item['id']}.structure.json"
            decisions = f"tables/evidence/{item['id']}.decisions.json"
            recovery = {"state": item["recovery"]["state"], "reason": ",".join(item["recovery"]["triggers"]) or "original-structure"}
            table.update(selectedCandidateId=selected["id"], recovery=recovery, confirmation=item["confirmation"],
                         evidencePaths={"candidates": self.candidate_path, "structure": structure, "decisions": decisions})
            common = {"tableId": table["id"], "selectedCandidateId": selected["id"], "source": {"page": item["page"]}}
            self.replace_product(structure)
            self.replace_product(decisions)
            write_json(self.result, self.output, structure, common | {"grid": selected["grid"], "cells": selected["cells"],
                       "uprightGeometry": selected.get("uprightGeometry"), "physicalRowBands": selected.get("physicalRowBands", []),
                       "rowSources": selected.get("rowSources", []),
                       "selectedRows": rows, "candidates": [c["id"] for c in item["candidates"]]}, "table-structure", source={"page": item["page"]})
            write_json(self.result, self.output, decisions, common | {"comparisons": item["comparisons"], "recovery": recovery,
                       "recoveryHistory": item.get("recoveryHistory"), "nativeMismatchHistory": item.get("nativeMismatchHistory", []),
                       "confirmation": item["confirmation"], "passesSpent": unit.get("passesSpent", 0),
                       "occurrenceBounds": item["bounds"],
                       "evidencePaths": item["paths"], "selectionReason": "geometry-first usable fallback; agreement is consistency"},
                       "table-recovery-audit", source={"page": item["page"]})
            unit["evidencePaths"] = list(dict.fromkeys([self.candidate_path, structure, decisions, table["typedPath"], table["csvPath"], deferred["workPath"]] + item["paths"]))
            self.emit("selection", page=item["page"], tableId=table["id"], candidateId=selected["id"], state=item["confirmation"]["state"])
        selected_ids = {t["id"] for t in self.result["tables"]}
        for previous_table in self.previous.get("tables", []):
            if previous_table["id"] not in selected_ids:
                for path in (previous_table["typedPath"], previous_table["csvPath"],
                             previous_table.get("evidencePaths", {}).get("structure"),
                             previous_table.get("evidencePaths", {}).get("decisions")):
                    if path:
                        for item in self.result["products"]:
                            if item["path"] == path:
                                item.update(kind="superseded-table-evidence", reason="Occurrence absent after invalidated discovery")
        for page, record in self.records.items():
            dependencies = [u["id"] for u in self.units.values() if u["source"]["page"] == page and u["kind"] != "page-complete" and u["state"] not in {"not_applicable", "superseded"}]
            complete = all(self.units[d]["state"] in {"completed", "not_applicable"} for d in dependencies) and record["outcome"] == "succeeded"
            self.unit(f"p{page:04d}:complete", "page-complete", page, dependencies=dependencies,
                      state="completed" if complete else "pending", pageRecord=record)
        self._summarize()
        if self._cutoff:
            finding(self.result, "WORK_BUDGET_EXHAUSTED", "PDF soft cutoff: remaining stages are pending in the work plan.")
            self.emit("budget-exhausted", pendingUnits=sum(u["state"] == "pending" for u in self.units.values()))
        write_json(self.result, self.output, "work-plan.json", {"version": 1, "fingerprint": self.fingerprint,
                   "units": list(self.units.values())}, "work-plan")

    def _summarize(self):
        """Project final unit states and generation-local observations once."""
        page_units = [self.units.get(f"p{p:04d}:complete", {}) for p in range(1, self.total + 1)]
        completed = sum(u.get("state") == "completed" for u in page_units)
        self.result["coverage"] = coverage("page", self.total, completed, len(self.records) - completed,
                                          figureDiscovery={**self.figure_discovery, "totalPages": self.total,
                                                           "limitations": self.figure_detection_limitations})
        states = [t["confirmation"]["state"] for t in self.result["tables"]]
        summary = {state: states.count(state) for state in ("agreed", "unresolved", "failed", "pending", "not_checked")}
        summary.update(discoveredTables=len(states), modelCalls=self.calls, additionalPasses=self.passes,
                       callBudgetExhausted=self.policy["maxModelCalls"] is not None and self.calls >= self.policy["maxModelCalls"])
        required_units = [u for u in self.units.values() if u["kind"] == "table-confirmation" and
                          u["state"] != "not_applicable" and u.get("reason") != "explicit-non-table"]
        summary["requiredOccurrences"] = len(required_units)
        self.discovery["acceptedOccurrences"] = len(states)
        self.result["coverage"].update(tableDiscovery=self.discovery, tableConfirmation=summary)
        for kind, policy in (("table-recovery", self.policy["geometry"]), ("table-confirmation", self.policy["vision"])):
            if policy == "off":
                stage = stage_outcome(kind, "not_applicable", reason="Disabled by resolved table recovery policy.")
            else:
                bad = (self.discovery["unresolvedRegions"] + sum(t["recovery"]["state"] == "unresolved" for t in self.result["tables"])) if kind == "table-recovery" else sum(u["state"] != "completed" for u in required_units)
                bad += sum(u["state"] not in {"completed", "not_applicable"} for u in self.units.values() if u["kind"] == "table-discovery")
                if not states and not bad:
                    stage = stage_outcome(kind, "not_applicable", reason="No table occurrences discovered; discovery is not complete recall.")
                else:
                    stage = stage_outcome(kind, "partial" if bad else "completed", coverage("table", len(states), len(states) - sum(summary[k] for k in ("pending", "failed", "unresolved"))))
            self.result["stages"].append(stage)
        reasons = {
            "native-evidence": "The PDF contains no physical pages.",
            "page-ocr": "No pages required OCR under native-text and image-coverage inspection.",
            "figure-detection": "The PDF contains no physical pages.",
            "figure-ocr": "No detected figures were available for OCR; consult figure-detection coverage for discovery failures.",
            "figure-description": (
                "Figure descriptions are disabled in the resolved profile." if not self.figure_descriptions
                else "No detected figures were available for interpretation; consult figure-detection coverage for discovery failures."
            ),
        }
        for kind, reason in reasons.items():
            units = [u for u in self.units.values() if u["kind"] == kind and u["state"] != "not_applicable"]
            if not units:
                self.result["stages"].append(stage_outcome(kind, "not_applicable", reason=reason))
                continue
            done = sum(u["state"] == "completed" for u in units)
            pending = sum(u["state"] == "pending" for u in units)
            state = "completed" if done == len(units) else "partial" if done or pending else "failed"
            self.result["stages"].append(stage_outcome(kind, state,
                coverage("figure" if kind in {"figure-ocr", "figure-description"} else "page",
                         len(units), done, len(units) - done - pending)))
