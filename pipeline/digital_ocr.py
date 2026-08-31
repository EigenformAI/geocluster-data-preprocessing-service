"""Digital-text adapter for tagged PDFs with a usable embedded text layer."""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from .util import Paths, mark_stage_done, write_json

STAGE = "ocr"
BACKEND = "digital"
_FOOTER_RE = re.compile(r"\bPage\s+(\d{1,2})-1\b", re.I)
_FIGURE_RE = re.compile(r"^\s*Figure\s+\d{1,2}-\d+\s*:\s*.+$", re.I | re.M)
_NUMBER_RE = re.compile(r"(?<!\w)-?\d[\d,.]*")
_TEXT_ROW_TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "intersection_tolerance": 3,
    "min_words_horizontal": 1,
}
_EXCLUDED_HEADINGS = {
    "BEARTRACK-ARNETT HEAP LEACH PROJECT",
    "PRELIMINARY FEASIBILITY STUDY",
}


def _normalized_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _section_heading(layout_text: str) -> str | None:
    footer = _FOOTER_RE.search(layout_text)
    if not footer:
        return None
    section = footer.group(1)
    for raw_line in layout_text.splitlines():
        line = _normalized_line(raw_line)
        letters = [char for char in line if char.isalpha()]
        if (
            2 < len(line) <= 100
            and letters
            and line == line.upper()
            and line not in _EXCLUDED_HEADINGS
            and not line.startswith(("TABLE ", "FIGURE ", "PAGE "))
        ):
            return f"# {section} {line}"
    return f"# {section} Section {section}"


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return _normalized_line(text).replace("|", r"\|")


def _cell_lines(value: object) -> list[str]:
    lines = [_normalized_line(line) for line in str(value or "").splitlines()]
    return [line for line in lines if line] or [""]


def _expand_multiline_data_rows(rows: list[list[object]]) -> list[list[object]]:
    """Split borderless visual rows that pdfplumber merged into multiline cells."""
    expanded: list[list[object]] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        lines_by_column = [_cell_lines(cell) for cell in row]
        numeric_multiline = [
            lines
            for lines in lines_by_column
            if len(lines) > 1 and sum(bool(_NUMBER_RE.search(line)) for line in lines) > 1
        ]
        if len(numeric_multiline) < 2:
            expanded.append(row)
            index += 1
            continue

        lane_count = max(len(lines) for lines in numeric_multiline)
        multiline_columns = {column for column, lines in enumerate(lines_by_column) if len(lines) > 1}
        continuation_rows: list[list[object]] = []
        cursor = index + 1
        while cursor < len(rows) and len(continuation_rows) < lane_count - 1:
            candidate = rows[cursor]
            if not any(_normalized_line(str(cell or "")) for cell in candidate):
                break
            if any(column < len(candidate) and _normalized_line(str(candidate[column] or "")) for column in multiline_columns):
                break
            continuation_rows.append(candidate)
            cursor += 1

        width = max([len(row), *(len(candidate) for candidate in continuation_rows)])
        logical_rows = [["" for _ in range(width)] for _ in range(lane_count)]
        for column, lines in enumerate(lines_by_column):
            numeric_line_count = sum(bool(_NUMBER_RE.search(line)) for line in lines)
            if (
                len(lines) > 1
                and len(lines) != lane_count
                and len(set(lines)) > 1
                and numeric_line_count < 2
            ):
                logical_rows[0][column] = " ".join(lines)
                continue
            for lane, value in enumerate(lines):
                if lane >= lane_count:
                    break
                logical_rows[lane][column] = value

        for lane, continuation in enumerate(continuation_rows, start=1):
            for column, cell in enumerate(continuation):
                value = _normalized_line(str(cell or ""))
                if not value:
                    continue
                if column == 0 and logical_rows[0][0] and len(lines_by_column[0]) == 1:
                    logical_rows[0][0] = f"{logical_rows[0][0]} {value}"
                elif not logical_rows[lane][column]:
                    logical_rows[lane][column] = value

        expanded.extend(logical_rows)
        index = cursor if continuation_rows else index + 1
    return expanded


def _looks_like_assay_table(rows: list[list[object]]) -> bool:
    header = " ".join(_normalized_line(str(cell or "")) for row in rows[:4] for cell in row).casefold()
    return "hole" in header and "from" in header and "to" in header and ("grade" in header or " au " in f" {header} ")


def _numeric_signature(row: list[object]) -> tuple[str, ...]:
    return tuple(token for cell in row for token in _NUMBER_RE.findall(str(cell or "")))


def _prepare_assay_text_rows(default_rows: list[list[object]], text_rows: list[list[object]]) -> list[list[object]]:
    """Use text baselines for sparse assay qualifiers and line-grid cells for group labels."""
    width = max((len(row) for row in default_rows), default=0)
    group_column = None
    for column in range(width):
        header = " ".join(
            _normalized_line(str(row[column] or ""))
            for row in default_rows[:4]
            if column < len(row)
        ).casefold()
        if re.search(r"\b(area|zone)\b", header):
            group_column = column
            break

    group_labels: dict[tuple[str, ...], str] = {}
    if group_column is not None:
        for row in _expand_multiline_data_rows(default_rows):
            signature = _numeric_signature(row)
            label = _normalized_line(str(row[group_column] or "")) if group_column < len(row) else ""
            if len(signature) >= 3:
                group_labels[signature] = label
        for source_row in default_rows:
            if group_column >= len(source_row):
                continue
            label_lines = _cell_lines(source_row[group_column])
            if len(label_lines) <= 1 or len(set(label_lines)) <= 1 or any(any(char.isdigit() for char in line) for line in label_lines):
                continue
            signatures = [
                signature
                for row in _expand_multiline_data_rows([source_row])
                if len(signature := _numeric_signature(row)) >= 3
            ]
            if signatures:
                group_labels[signatures[0]] = " ".join(label_lines)
                for signature in signatures[1:]:
                    group_labels[signature] = ""

    prepared: list[list[object]] = []
    for source_row in text_rows:
        row = list(source_row)
        nonempty = [column for column, cell in enumerate(row) if _normalized_line(str(cell or ""))]
        if not nonempty:
            continue
        signature = _numeric_signature(row)
        if group_column is not None and len(signature) >= 3:
            while len(row) <= group_column:
                row.append("")
            if signature in group_labels:
                row[group_column] = group_labels[signature]
        if group_column is not None and nonempty == [group_column]:
            continue
        prepared.append(row)
    return prepared


def _bbox_overlap_ratio(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    return intersection / area if area else 0.0


def _collapse_table_headers(rows: list[list[object]]) -> list[list[object]]:
    data_start = next(
        (
            index
            for index, row in enumerate(rows)
            if sum(bool(_NUMBER_RE.search(str(cell or ""))) for cell in row) >= 2
        ),
        None,
    )
    if data_start is None or data_start <= 1:
        return rows
    width = max((len(row) for row in rows), default=0)
    header = []
    for column in range(width):
        parts = []
        for row in rows[:data_start]:
            value = _normalized_line(str(row[column] or "")) if column < len(row) else ""
            if value and value not in parts:
                parts.append(value)
        header.append(" / ".join(parts))
    return [header, *rows[data_start:]]


def _table_markdown(rows: list[list[object]]) -> str:
    if not _looks_like_assay_table(rows):
        rows = _expand_multiline_data_rows(rows)
    rows = _collapse_table_headers(rows)
    width = max((len(row) for row in rows), default=0)
    if width < 2:
        return ""
    normalized = [[_cell(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
    normalized = [row for row in normalized if any(row)]
    if len(normalized) < 2:
        return ""
    if not any(normalized[0]):
        normalized[0] = [f"Column {index}" for index in range(1, width + 1)]
    lines = [
        "| " + " | ".join(normalized[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
    return "\n".join(lines)


def _clean_layout_text(layout_text: str) -> str:
    lines = []
    pending_paragraph = False
    for raw_line in layout_text.splitlines():
        line = _normalized_line(raw_line)
        if line.upper() in _EXCLUDED_HEADINGS or line == "Kappes, Cassiday & Associates":
            continue
        if re.fullmatch(r"June 2023\s+Page\s+\d{1,2}-\d+", line, re.I):
            continue
        if not line:
            pending_paragraph = bool(lines and lines[-1] and re.search(r"[.!?;:)\]”\"]$", lines[-1]))
            continue
        if pending_paragraph:
            lines.append("")
        lines.append(line)
        pending_paragraph = False
    return "\n".join(lines).strip()


def build_page_markdown(*, text: str, layout_text: str, tables: list[list[list[object]]]) -> str:
    """Build the per-page Markdown contract consumed by ``parse_ocr``."""
    blocks = []
    heading = _section_heading(layout_text)
    if heading:
        blocks.append(heading)
    if text.strip():
        blocks.append(text.strip())
    blocks.extend(markdown for rows in tables if (markdown := _table_markdown(rows)))
    return "\n\n".join(blocks).strip() + "\n"


def _figure_image(paths: Paths, page_number: int, text: str, width: float, height: float) -> list[dict]:
    if not _FIGURE_RE.search(text):
        return []
    page_png = paths.page_png(page_number)
    if not page_png.exists():
        return []
    image_dir = paths.root / "ocr" / "img"
    image_dir.mkdir(parents=True, exist_ok=True)
    target = image_dir / f"p{page_number:04d}_figure.jpg"
    with Image.open(page_png) as image:
        image = image.convert("RGB")
        if image.width > 1600:
            new_height = round(image.height * 1600 / image.width)
            image = image.resize((1600, new_height), Image.Resampling.LANCZOS)
        image.save(target, "JPEG", quality=88, optimize=True)
    return [{
        "id": target.stem,
        "x0": 0.0,
        "y0": 0.0,
        "x1": width,
        "y1": height,
        "file": f"img/{target.name}",
    }]


def run(paths: Paths, n_pages: int, force: bool = False) -> dict:
    import pdfplumber

    ocr_dir = paths.root / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    pages_written = 0
    table_count = 0
    figure_pages = 0
    with pdfplumber.open(paths.source_pdf) as pdf:
        if len(pdf.pages) != n_pages:
            raise ValueError(f"digital OCR expected {n_pages} pages, found {len(pdf.pages)}")
        for page_number, page in enumerate(pdf.pages, start=1):
            md_path = ocr_dir / f"page_{page_number:04d}.md"
            json_path = ocr_dir / f"page_{page_number:04d}.json"
            if not force and md_path.exists() and json_path.exists():
                continue
            raw_text = page.extract_text() or ""
            layout_text = page.extract_text(layout=True) or raw_text
            text = _clean_layout_text(layout_text) or raw_text
            extracted_tables = []
            table_meta = []
            text_row_tables = None
            for table in page.find_tables():
                rows = table.extract() or []
                if _looks_like_assay_table(rows):
                    if text_row_tables is None:
                        text_row_tables = page.find_tables(_TEXT_ROW_TABLE_SETTINGS)
                    candidate = max(text_row_tables, key=lambda item: _bbox_overlap_ratio(table.bbox, item.bbox), default=None)
                    if candidate is not None and _bbox_overlap_ratio(table.bbox, candidate.bbox) >= 0.5:
                        rows = _prepare_assay_text_rows(rows, candidate.extract() or [])
                if not _table_markdown(rows):
                    continue
                extracted_tables.append(rows)
                table_meta.append({"bbox": [round(float(value), 3) for value in table.bbox]})
            images = _figure_image(paths, page_number, raw_text, float(page.width), float(page.height))
            md_path.write_text(build_page_markdown(text=text, layout_text=layout_text, tables=extracted_tables))
            write_json(json_path, {
                "page": page_number,
                "method": "digital_text",
                "dimensions": {"width": float(page.width), "height": float(page.height)},
                "tables": table_meta,
                "images": images,
            })
            pages_written += 1
            table_count += len(extracted_tables)
            figure_pages += bool(images)
    info = {
        "backend": BACKEND,
        "n_pages": n_pages,
        "pages_written": pages_written,
        "n_tables": table_count,
        "n_figure_pages": figure_pages,
    }
    mark_stage_done(paths, STAGE, info)
    return info
