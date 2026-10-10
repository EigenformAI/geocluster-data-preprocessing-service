"""Physical-page PDF evidence with isolated OCR and replaceable figure discovery.

Native table extraction and caption-page figures deliberately reuse the ideas in
upstream digital_ocr.py, without report-specific cleaning, merged headers, stage
caches, or its provider runners. Detection is evidence, not complete recall.
"""

import json
import math
import re
from importlib.metadata import version
from pathlib import Path
from typing import Protocol

from preprocessing.contracts import Extraction, ExtractionContext
from preprocessing.errors import ServiceError

from .common import (
    TableWriter,
    coverage,
    empty_extraction,
    finding,
    limits_for,
    product,
    stage_outcome,
    write_json,
    write_text,
)


class FigureDetector(Protocol):
    version: str
    limitations: str

    def detect(self, page, text: str) -> list[dict]:
        """Return {bbox: [x0, top, x1, bottom], method, caption?} in page coordinates."""
        ...


class NativeFigureDetector:
    version = "native-images-caption-page-2"
    limitations = ("Embedded image rectangles and pages with Figure/Fig. captions only; "
                   "uncaptioned vector figures may be missed, images can include logos or scanned pages, "
                   "and caption-page bounds are whole-page evidence, not precise figure segmentation. "
                   "Overlapping images are grouped before a configurable minimum displayed dimension filter; "
                   "small standalone images may contain meaningful content. Every image disposition is recorded.")

    def __init__(self, *, min_dimension_points: float = 16, group_overlaps: bool = True, caption_page: bool = True):
        if isinstance(min_dimension_points, bool) or not isinstance(min_dimension_points, (int, float)) or not math.isfinite(min_dimension_points) or min_dimension_points < 0:
            raise ServiceError("INVALID_PROFILE", "Figure minimum dimension must be a nonnegative finite number of PDF points.")
        if not isinstance(group_overlaps, bool) or not isinstance(caption_page, bool):
            raise ServiceError("INVALID_PROFILE", "Figure grouping and caption-page options must be booleans.")
        self.options = {"minDimensionPoints": min_dimension_points, "groupOverlaps": group_overlaps, "captionPage": caption_page}

    @classmethod
    def from_options(cls, options: dict):
        if not isinstance(options, dict) or set(options) - {"minDimensionPoints", "groupOverlaps", "captionPage"}:
            raise ServiceError("INVALID_PROFILE", "Unknown figure detector option.")
        return cls(min_dimension_points=options.get("minDimensionPoints", 16),
                   group_overlaps=options.get("groupOverlaps", True), caption_page=options.get("captionPage", True))

    def detect(self, page, text: str) -> list[dict]:
        return self.inspect(page, text)["candidates"]

    def inspect(self, page, text: str) -> dict:
        """Pure discovery plus an audit of all input rectangles, including exclusions."""
        captions = re.findall(r"^\s*(?:figure|fig\.)\s+\S+[^\n]*", text, flags=re.IGNORECASE | re.MULTILINE)
        audit = {"options": dict(self.options), "images": [], "candidates": [], "excludedCount": 0}
        groups = []
        for index, image in enumerate(page.images, 1):
            raw = [image["x0"], image["top"], image["x1"], image["bottom"]]
            bbox = [max(page.bbox[0], raw[0]), max(page.bbox[1], raw[1]),
                    min(page.bbox[2], raw[2]), min(page.bbox[3], raw[3])]
            record = {"index": index, "rawBBox": raw, "bbox": bbox, "disposition": "included"}
            audit["images"].append(record)
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                record["disposition"] = "excluded-outside-page"
                audit["excludedCount"] += 1
                continue
            if captions and self.options["captionPage"]:
                record["disposition"] = "subsumed-caption-page"
                continue
            group = {"bbox": bbox, "imageIndices": [index]}
            if self.options["groupOverlaps"]:
                # Grow coherent rectangles to a fixed point, including contained
                # and touching image tiles before deciding whether a group is tiny.
                changed = True
                while changed:
                    changed = False
                    for other in list(groups):
                        a, b = group["bbox"], other["bbox"]
                        if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                            group["bbox"] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                            group["imageIndices"].extend(other["imageIndices"])
                            groups.remove(other)
                            changed = True
            groups.append(group)
        if captions and self.options["captionPage"]:
            audit["candidates"] = [{"bbox": list(page.bbox), "method": "caption-page", "caption": "\n".join(captions),
                                     "imageIndices": [r["index"] for r in audit["images"] if r["disposition"] == "subsumed-caption-page"]}]
        else:
            for group in sorted(groups, key=lambda g: min(g["imageIndices"])):
                box = group["bbox"]
                excluded = min(box[2] - box[0], box[3] - box[1]) < self.options["minDimensionPoints"]
                disposition = "excluded-tiny" if excluded else "grouped" if len(group["imageIndices"]) > 1 else "included"
                for index in group["imageIndices"]:
                    audit["images"][index - 1]["disposition"] = disposition
                if excluded:
                    audit["excludedCount"] += len(group["imageIndices"])
                else:
                    audit["candidates"].append(group | {"method": "embedded-image-group" if len(group["imageIndices"]) > 1 else "embedded-image", "caption": None})
        return audit


def bounds(page, bbox) -> dict:
    return {"bbox": [float(bbox[0] - page.bbox[0]), float(bbox[1] - page.bbox[1]),
                     float(bbox[2] - page.bbox[0]), float(bbox[3] - page.bbox[1])],
            "units": "pt", "origin": "top-left", "pageWidth": float(page.width), "pageHeight": float(page.height),
            "rotation": page.rotation, "coordinateSpace": "rotated-media-box",
            "transform": "pdfplumber coordinates translated by media-box top-left; rotation already applied"}


class Renderer:
    def __init__(self, source, pixel_limit):
        self.source, self.pixel_limit, self.document = source, pixel_limit, None

    def render(self, page_index: int, target: Path, bbox=None, media_box=None):
        import pypdfium2
        if self.document is None:
            self.document = pypdfium2.PdfDocument(self.source)
        page = self.document[page_index]
        try:
            # Native evidence uses MediaBox. PDFium defaults to CropBox, which
            # would otherwise shift crops and silently change image scale.
            page.set_cropbox(*(media_box if media_box is not None else page.get_mediabox()))
            width, height = page.get_size()
            if self.pixel_limit < 1 or width <= 0 or height <= 0:
                raise ServiceError("RESOURCE_LIMIT", "Page cannot be rendered within the configured pixel limit.")
            scale = min(2.0, math.sqrt(self.pixel_limit / ((width + 1) * (height + 1))))
            while math.ceil(width * scale) * math.ceil(height * scale) > self.pixel_limit:
                scale *= 0.95
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
                try:
                    if bbox is not None:
                        rectangle = (max(0, math.floor(bbox[0] * scale)), max(0, math.floor(bbox[1] * scale)),
                                     min(image.width, math.ceil(bbox[2] * scale)), min(image.height, math.ceil(bbox[3] * scale)))
                        if rectangle[0] >= rectangle[2] or rectangle[1] >= rectangle[3]:
                            raise ServiceError("INVALID_INPUT", "Figure bounds do not intersect the rendered page.")
                        cropped = image.crop(rectangle)
                        image.close()
                        image = cropped
                    target.parent.mkdir(parents=True, exist_ok=True)
                    image.save(target, "PNG")
                    return {"width": image.width, "height": image.height, "pixelsPerPoint": scale}
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()

    def close(self):
        if self.document is not None:
            self.document.close()
            self.document = None

    def render_region(self, page_index, target, bbox, *, dpi=300, orientation=0, media_box=None):
        """Render a MediaBox-relative region directly; affine arrays use [a,b,c,d,e,f]."""
        import pypdfium2
        from PIL import Image
        if self.document is None:
            self.document = pypdfium2.PdfDocument(self.source)
        page = self.document[page_index]
        try:
            media = list(media_box if media_box is not None else page.get_mediabox())
            page.set_cropbox(*media)
            width, height = page.get_size()
            x0, y0, x1, y1 = bbox
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise ServiceError("INVALID_INPUT", "Table crop lies outside the MediaBox.")
            scale = dpi / 72
            if math.ceil((x1 - x0) * scale) * math.ceil((y1 - y0) * scale) > self.pixel_limit:
                raise ServiceError("RESOURCE_LIMIT", "Table crop requires tiling at the requested DPI.")
            bitmap = page.render(scale=scale, crop=(x0, height - y1, width - x1, y0))
            try:
                image = bitmap.to_pil()
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    original = None
                    w, h = image.size
                    converter = bitmap.get_posconv(page)
                    rotation = page.get_rotation()
                    def source_point(px, py):
                        x, y = converter.to_page(px, py)
                        return {0: (x - media[0], media[3] - y),
                                90: (y - media[1], x - media[0]),
                                180: (media[2] - x, y - media[1]),
                                270: (media[3] - y, media[2] - x)}[rotation]

                    def invert(matrix):
                        a, b, c, d, e, f = matrix
                        determinant = a * d - b * c
                        return [d / determinant, -b / determinant, -c / determinant, a / determinant,
                                (c * f - d * e) / determinant, (b * e - a * f) / determinant]

                    origin, right, bottom = source_point(0, 0), source_point(w, 0), source_point(0, h)
                    base_inverse = [(right[0] - origin[0]) / w, (right[1] - origin[1]) / w,
                                    (bottom[0] - origin[0]) / h, (bottom[1] - origin[1]) / h, *origin]
                    forward = invert(base_inverse)
                    actual_box = [origin[0], origin[1], right[0], bottom[1]]
                    source_render = {"width": w, "height": h, "pixelsPerPoint": scale, "dpi": dpi,
                                     "orientation": 0, "cropMargin": 0, "pageToImage": list(forward),
                                     "imageToPage": base_inverse, "bbox": actual_box, "requestedBBox": list(bbox)}
                    if orientation:
                        original = target.with_name(target.stem + ".source.png")
                        image.save(original, "PNG")
                        rotated = image.transpose({90: Image.Transpose.ROTATE_270,
                                                   180: Image.Transpose.ROTATE_180,
                                                   270: Image.Transpose.ROTATE_90}[orientation])
                        image.close()
                        image = rotated
                        a, b, c, d, e, f = forward
                        forward = {90: [-b, a, -d, c, h - f, e],
                                   180: [-a, -b, -c, -d, w - e, h - f],
                                   270: [b, -a, d, -c, f, w - e]}[orientation]
                    inverse = invert(forward)
                    image.save(target, "PNG")
                    return {"width": image.width, "height": image.height, "pixelsPerPoint": scale,
                            "dpi": dpi, "orientation": orientation, "cropMargin": 0,
                            "pageToImage": forward, "imageToPage": inverse,
                            "sourceImage": original.name if original else None,
                            "sourceRender": source_render if original else None,
                            "bbox": actual_box, "requestedBBox": list(bbox)}
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()


class PDFAdapter:
    extensions = (".pdf",)
    version = "4"

    def __init__(self, figure_detector: FigureDetector | None = None):
        self.figure_detector = figure_detector

    def extract(self, snapshot: dict, profile: dict, output_dir: Path, context: ExtractionContext) -> Extraction:
        import pdfplumber
        from pdfminer.pdfdocument import PDFPasswordIncorrect
        from pdfminer.pdfparser import PDFSyntaxError

        result = empty_extraction("pdf", self.version)
        limits = limits_for(profile)
        detector = self.figure_detector or NativeFigureDetector.from_options(profile.get("options", {}).get("figureDetector", {}))
        result["provenance"].update({"parsers": {name: version(name) for name in ("pdfplumber", "pypdfium2")},
                                     "figureDetector": detector.version,
                                     "figureDetectionLimitations": detector.limitations,
                                     "promptVersion": profile.get("promptVersion", "1")})
        if isinstance(detector, NativeFigureDetector):
            result["provenance"]["figureDetectorOptions"] = dict(detector.options)
        renderer = Renderer(snapshot["path"], limits["pixels"])
        from .pdf_recovery import PDFRecovery
        recovery = PDFRecovery(snapshot, profile, output_dir, context, result, renderer, limits)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "text").mkdir(exist_ok=True)
        completed = failed = scan_completed = 0
        table_rows = 0
        excluded_images = 0
        stage_counts = {name: {"completed": 0, "failed": 0, "partial": 0} for name in ("native-evidence", "page-ocr", "figure-detection", "figure-ocr", "figure-description")}

        def call_model(kind, source, image_path, prompt):
            recovery.check_work()
            try:
                response = context.model({"kind": kind, "sourceUnit": source, "prompt": prompt},
                                         [{"path": str((output_dir / image_path).resolve()), "mediaType": "image/png"}])
            except ServiceError as exc:
                recovery.pause_for_usage(exc)
                if exc.code == "WORK_BUDGET_EXHAUSTED":
                    recovery.cutoff = True
                raise
            text = response.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ServiceError("MODEL_OUTPUT_INVALID", "Model returned no transcription or interpretation.")
            if len(text.encode("utf-8")) > limits["responseBytes"]:
                raise ServiceError("RESOURCE_LIMIT", "Model output exceeded the configured response-byte limit.")
            return text, {key: response[key] for key in ("requestedModel", "returnedModel", "sessionId", "usage", "operationId") if key in response}

        def failure(stage, page_number, code, message, *, partial=False):
            stage_counts[stage]["partial" if partial else "failed"] += 1
            finding(result, code, message, sourcePages=[page_number])

        def interrupted(exc, stage, page_number, code, message):
            _raise_if_fatal(exc)
            if recovery.pause_for_usage(exc) or isinstance(exc, ServiceError) and exc.code == "WORK_BUDGET_EXHAUSTED":
                recovery.cutoff = True
                stage_counts[stage]["partial"] += 1
                return "pending"
            failure(stage, page_number, code, message)
            return "failed"

        def pdf_table(rows, locator, method, model=None, *, table_id=None):
            nonlocal table_rows
            table_rows = sum(t["rows"] for t in result["tables"])
            if table_rows + len(rows) > limits["rows"]:
                raise ServiceError("RESOURCE_LIMIT", "PDF tables exceed the configured document row limit.")
            with TableWriter(result, output_dir, table_id or f"table_{len(result['tables']) + 1:04d}", locator, limits) as writer:
                writer.table.update({"sourcePages": [locator["page"]], "evidenceBasis": method})
                if model is not None:
                    writer.table["model"] = model
                for row_index, values in enumerate(rows, 1):
                    table_rows += 1
                    if table_rows > limits["rows"]:
                        raise ServiceError("RESOURCE_LIMIT", "PDF tables exceed the configured document row limit.")
                    if not isinstance(values, list) or any(v is not None and not isinstance(v, str) for v in values):
                        raise ServiceError("MODEL_OUTPUT_INVALID", "PDF table cells must be strings or null.")
                    writer.add([({"type": "null"} if value is None else {"type": "text", "value": value}) |
                                {"source": {"page": locator["page"], "row": row_index, "column": col}, "method": method}
                                for col, value in enumerate(values, 1)], locator | {"row": row_index})

        try:
            with pdfplumber.open(snapshot["path"]) as document, (output_dir / "text/pages.jsonl").open("w", encoding="utf-8") as inventory:
                total = len(document.pages)
                recovery.enumerate_pages(total)
                if total > limits["pages"]:
                    raise ServiceError("RESOURCE_LIMIT", "PDF exceeds the configured physical-page limit.")
                for page_number, page in enumerate(document.pages, 1):
                    if recovery.reuse_page(page_number, inventory, stage_counts):
                        completed += 1
                        scan_completed += 1
                        page.close()
                        continue
                    if not recovery.available():
                        inventory.write(json.dumps({"id": f"page_{page_number:04d}", "source": {"page": page_number}, "outcome": "not_processed"}) + "\n")
                        page.close()
                        continue
                    recovery.begin_page(page_number)
                    source = {"page": page_number, "bounds": bounds(page, page.bbox)}
                    page_record = {"id": f"page_{page_number:04d}", "source": source, "outcome": "succeeded"}
                    page_ok = True
                    text = ""
                    try:
                        text_path = f"text/page_{page_number:04d}.native.txt"
                        if recovery.reused_native():
                            text = (output_dir / text_path).read_text(encoding="utf-8")
                        else:
                            text = page.extract_text() or ""
                            recovery.replace_product(text_path)
                            write_text(result, output_dir, text_path, text, "page-text", source=source, method="native")
                        words = recovery.words(page, page_number)
                        if not recovery.reused_native():
                            recovery.replace_product(f"text/page_{page_number:04d}.json")
                            write_json(result, output_dir, f"text/page_{page_number:04d}.json", {"source": source, "words": words}, "page-evidence", source=source)
                        page_record["nativeTextPath"] = text_path
                        recovery.native(page, words)
                        recovery.complete_native()
                        page_record["nativeStatus"] = "succeeded"
                        stage_counts["native-evidence"]["completed"] += 1
                    except Exception as exc:  # noqa: BLE001 — isolate malformed native pages, retaining earlier evidence
                        page_ok = False
                        page_record["nativeStatus"] = interrupted(exc, "native-evidence", page_number, "PAGE_EXTRACTION_FAILED", f"Native evidence on page {page_number} could not be extracted.")
                        recovery.units[f"p{page_number:04d}:native"].update(state=page_record["nativeStatus"], reason=getattr(exc, "code", type(exc).__name__))

                    # An image-heavy page can contain only a native footer/caption;
                    # text presence alone is insufficient evidence of complete text.
                    try:
                        image_area = sum(max(0, i["x1"] - i["x0"]) * max(0, i["bottom"] - i["top"]) for i in page.images)
                        needs_ocr = not text.strip() or image_area >= page.width * page.height * 0.5
                    except Exception as exc:  # noqa: BLE001 — unknown image coverage requires OCR
                        _raise_if_fatal(exc)
                        needs_ocr = True
                    if needs_ocr and recovery.reused_ocr(page_record):
                        stage_counts["page-ocr"]["completed"] += 1
                    elif needs_ocr:
                        image_path = f"text/page_{page_number:04d}.png"
                        ocr_path = f"text/page_{page_number:04d}.ocr.txt"
                        try:
                            recovery.check_work()
                            rendered = renderer.render(page_number - 1, output_dir / image_path, media_box=page.page_obj.mediabox)
                            recovery.adopt_product(image_path, "page-image", source=source, render=rendered)
                            transcription, model = call_model("page-ocr", source, image_path,
                                'Transcribe all visible text, preserving numbers and uncertain/unreadable spans. '
                                'Return only a JSON object, without Markdown fences or commentary: '
                                 '{"text":"...","tables":[{"bounds":[0,0,1000,1000],"rows":[["cell",null]]}]}. '
                                 'Bounds are normalized 0..1000 coordinates in this input image, top-left origin. '
                                'Use text "" if no text is visible and tables [] if no table is visible. '
                                'Include every visible table, retaining headers as rows. All non-null cells must be JSON strings, '
                                'including numbers with their original spelling; use "" for an empty cell, null for an unavailable cell, '
                                'and [unreadable] within strings for unreadable spans. Mark doubtful readings [uncertain: ...]. '
                                 'Do not infer names or numbers from context or prior knowledge. Do not interpret or correct values.')
                            raw_ocr_path = f"text/page_{page_number:04d}.ocr.response.txt"
                            recovery.replace_product(raw_ocr_path)
                            write_text(result, output_dir, raw_ocr_path, transcription, "page-ocr-response", source=source, model=model)
                            try:
                                structured = json.loads(transcription)
                            except json.JSONDecodeError:
                                structured = None
                            if isinstance(structured, dict) and isinstance(structured.get("text"), str) and isinstance(structured.get("tables"), list):
                                for table in structured["tables"]:
                                    if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
                                        raise ServiceError("MODEL_OUTPUT_INVALID", "Page OCR returned an invalid table.")
                                    recovery.ocr_candidate(page, table, model)
                                text_ocr = structured["text"]
                                page_record["ocrStatus"] = "succeeded"
                                stage_counts["page-ocr"]["completed"] += 1
                            else:
                                text_ocr = transcription
                                page_ok = False
                                page_record["ocrStatus"] = "partial"
                                failure("page-ocr", page_number, "OCR_TABLE_COVERAGE_UNKNOWN", f"Page {page_number} has OCR text but no structured table inspection result.", partial=True)
                            recovery.replace_product(ocr_path)
                            write_text(result, output_dir, ocr_path, text_ocr, "page-ocr", source=source, method="model-transcription", model=model)
                        except Exception as exc:  # noqa: BLE001 — model/render failure is a per-page outcome
                            page_ok = False
                            page_record["ocrStatus"] = interrupted(exc, "page-ocr", page_number, "PAGE_OCR_FAILED", f"OCR on page {page_number} failed; native evidence is retained.")
                        page_record["ocrPath"] = ocr_path if (output_dir / ocr_path).is_file() else None

                    recovery.resolve_page(page, needs_ocr and page_record.get("ocrStatus") == "succeeded")

                    try:
                        recovery.check_work()
                        if type(detector) is NativeFigureDetector:
                            audit = detector.inspect(page, text)
                            detected = audit["candidates"]
                            audit_path = f"text/page_{page_number:04d}.figure-detection.json"
                            recovery.replace_product(audit_path)
                            write_json(result, output_dir, audit_path, {"source": source, **audit}, "figure-detection", source=source)
                            page_record["figureDetectionPath"] = audit_path
                            excluded_images += audit["excludedCount"]
                            if audit["excludedCount"]:
                                finding(result, "FIGURE_DETECTOR_EXCLUSIONS",
                                        f"Page {page_number}: {audit['excludedCount']} embedded images excluded by detector geometry rules; inspect the detector evidence and override options if needed.",
                                        sourcePages=[page_number], evidencePath=audit_path)
                        else:
                            detected = detector.detect(page, text)
                        scan_completed += 1
                        page_record["figureDetectionStatus"] = "succeeded"
                        stage_counts["figure-detection"]["completed"] += 1
                    except Exception as exc:  # noqa: BLE001 — detector plugins can fail independently
                        detected = []
                        page_ok = False
                        page_record["figureDetectionStatus"] = interrupted(exc, "figure-detection", page_number, "FIGURE_DETECTION_FAILED", f"Figure discovery on page {page_number} failed.")
                    for figure_index, candidate in enumerate(detected, 1):
                        figure_id = f"figure_p{page_number:04d}_{figure_index:04d}"
                        previous_figure = next((f for f in recovery.previous.get("figures", []) if f["id"] == figure_id), None)
                        figure_ocr_unit = recovery.units.get(figure_id + ":figure-ocr", {})
                        figure_description_unit = recovery.units.get(figure_id + ":figure-description", {})
                        if previous_figure and figure_ocr_unit.get("state") == "completed" and (
                                figure_description_unit.get("state") == "completed" or not profile.get("options", {}).get("figureDescriptions", True)):
                            result["figures"].append(previous_figure)
                            continue
                        figure_source = {"page": page_number, "bounds": bounds(page, candidate["bbox"])}
                        figure = {"id": figure_id, "source": figure_source, "caption": candidate.get("caption"),
                                  "detection": candidate["method"], "outcome": "succeeded", "ocrStatus": "failed",
                                  "imagePath": None, "ocrPath": f"figures/{figure_id}.ocr.txt", "interpretationPath": None}
                        reuse_ocr = previous_figure and figure_ocr_unit.get("state") == "completed"
                        if reuse_ocr:
                            figure.update(previous_figure)
                            figure["outcome"] = "succeeded"
                        result["figures"].append(figure)
                        # Failed OCR has a listed empty transcription plus an explicit
                        # failure status, never a fabricated transcription of an error.
                        if not reuse_ocr:
                            recovery.replace_product(figure["ocrPath"])
                            write_text(result, output_dir, figure["ocrPath"], "", "figure-ocr", source=figure_source, figureId=figure_id)
                            ocr_product = result["products"][-1]
                            ocr_product.update({"status": "failed", "method": "model-transcription"})
                        image_path = f"figures/{figure_id}.png"
                        try:
                            if not reuse_ocr:
                                recovery.check_work()
                                rendered = renderer.render(page_number - 1, output_dir / image_path, figure_source["bounds"]["bbox"], page.page_obj.mediabox)
                                recovery.replace_product(image_path)
                                product(result, image_path, "figure-image", "image/png", source=figure_source, figureId=figure_id, render=rendered)
                                figure["imagePath"] = image_path
                                ocr, model = call_model("figure-ocr", figure_source | {"figureId": figure_id}, image_path,
                                    "Transcribe visible figure text, including legends, axes, annotations, numbers and units. "
                                    "Copy only characters you can actually read; do not infer names or numbers from context or prior knowledge. "
                                    "Preserve original spelling, punctuation, and numeric precision. Mark uncertain spans [uncertain: ...] "
                                    "and illegible spans [unreadable]; do not guess. If no text is visible state [no visible text]. "
                                    "Return plain text only. Do not describe, interpret, normalize or invent content.")
                                (output_dir / figure["ocrPath"]).write_text(ocr, encoding="utf-8")
                                figure.update({"ocrStatus": "succeeded", "ocrModel": model})
                                ocr_product.update({"status": "succeeded", "model": model})
                            stage_counts["figure-ocr"]["completed"] += 1
                        except Exception as exc:  # noqa: BLE001 — retain figure identity even if rendering/OCR fails
                            figure["outcome"] = "partial"
                            page_ok = False
                            figure["ocrStatus"] = interrupted(exc, "figure-ocr", page_number, "FIGURE_OCR_FAILED", f"OCR for {figure_id} failed; the detected figure record is retained.")
                            ocr_product["status"] = figure["ocrStatus"]
                        if profile.get("options", {}).get("figureDescriptions", True):
                            if figure["imagePath"]:
                                try:
                                    interpretation, model = call_model("figure-description", figure_source | {"figureId": figure_id}, image_path,
                                        "Provide a concise factual interpretation of this figure. Distinguish observation from uncertainty. "
                                        "Return plain text in at most 120 words. This is an interpretation, not a transcription of its visible text.")
                                    path = f"figures/{figure_id}.interpretation.txt"
                                    recovery.replace_product(path)
                                    write_text(result, output_dir, path, interpretation, "figure-interpretation", source=figure_source, figureId=figure_id, model=model)
                                    figure.update({"interpretationPath": path, "interpretationStatus": "succeeded"})
                                    stage_counts["figure-description"]["completed"] += 1
                                except Exception as exc:  # noqa: BLE001 — interpretation cannot discard transcription
                                    page_ok = False
                                    figure.update(outcome="partial", interpretationStatus=interrupted(exc, "figure-description", page_number, "FIGURE_DESCRIPTION_FAILED", f"Interpretation for {figure_id} failed; its OCR is separate."))
                            else:
                                if recovery.cutoff:
                                    figure["interpretationStatus"] = "pending"
                                    stage_counts["figure-description"]["partial"] += 1
                                else:
                                    figure["interpretationStatus"] = "not_processed"
                                    failure("figure-description", page_number, "FIGURE_IMAGE_UNAVAILABLE", f"Interpretation for {figure_id} could not run because its image is unavailable.")
                        else:
                            figure["interpretationStatus"] = "not_applicable"
                        recovery.figure_units(figure)
                    if page_ok:
                        completed += 1
                    else:
                        failed += 1
                        page_record["outcome"] = "partial"
                    inventory.write(json.dumps(page_record, ensure_ascii=False) + "\n")
                    recovery.end_page(page_record)
                    page.close()
                result["coverage"] = coverage("page", total, completed, failed,
                                               figureDiscovery={"pagesScanned": scan_completed, "totalPages": total,
                                                                "excludedEmbeddedImages": excluded_images,
                                                                "limitations": detector.limitations})
        except (PDFPasswordIncorrect, PDFSyntaxError) as exc:
            raise ServiceError("INVALID_INPUT", "PDF is encrypted or malformed; provide an unencrypted readable PDF.") from exc
        finally:
            renderer.close()
        product(result, "text/pages.jsonl", "page-inventory", "application/x-ndjson")
        try:
            recovery.finish(pdf_table)
        finally:
            renderer.close()
        reasons = {
            "native-evidence": "The PDF contains no physical pages.",
            "page-ocr": "No pages required OCR under native-text and image-coverage inspection.",
            "figure-detection": "The PDF contains no physical pages.",
            "figure-ocr": "No detected figures were available for OCR; consult figure-detection coverage for discovery failures.",
            "figure-description": (
                "Figure descriptions are disabled in the resolved profile." if not profile.get("options", {}).get("figureDescriptions", True)
                else "No detected figures were available for interpretation; consult figure-detection coverage for discovery failures."
            ),
        }
        for name, count in stage_counts.items():
            attempted = sum(count.values())
            if not attempted:
                result["stages"].append(stage_outcome(name, "not_applicable", reason=reasons[name]))
                continue
            incomplete = count["failed"] + count["partial"]
            state = ("partial" if count["partial"] or (incomplete and count["completed"])
                     else "failed" if incomplete else "completed")
            stage_coverage = coverage("figure" if name in {"figure-ocr", "figure-description"} else "page",
                                      attempted, count["completed"], incomplete, scope=name)
            result["stages"].append(stage_outcome(name, state, stage_coverage))
        recovery.reconcile_stages()
        return result


def _raise_if_fatal(exc: Exception) -> None:
    if isinstance(exc, (OSError, MemoryError)) or (
        isinstance(exc, ServiceError) and exc.code in {"RESOURCE_LIMIT", "INPUT_LIMIT", "CANCELLED", "JOB_CANCELLED"}
    ):
        raise exc
