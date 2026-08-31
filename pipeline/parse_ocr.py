"""Stage 4 PARSE: OCR markdown -> sections, text files, table CSVs, figure images.

- Sections come from numbered ATX headers in the OCR markdown (e.g. '# 1 Summary').
- Tables are GitHub-flavoured markdown tables, one CSV per table block.
- Figures are the images Mistral OCR extracted in Stage 3.
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path

from .util import Paths, mark_stage_done, read_json, slugify, write_json

STAGE = "parse"

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")


# ---- markdown table parsing -------------------------------------------------

def _is_row(line: str) -> bool:
    return line.strip().startswith("|")


def _is_separator(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells)


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", s)]


def _parse_tables(md: str) -> list[list[list[str]]]:
    lines = md.splitlines()
    tables: list[list[list[str]]] = []
    i = 0
    while i < len(lines):
        if _is_row(lines[i]) and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            rows = [_split_row(lines[i])]
            k = i + 2
            while k < len(lines) and _is_row(lines[k]):
                rows.append(_split_row(lines[k]))
                k += 1
            tables.append(rows)
            i = k
        else:
            i += 1
    return tables


# ---- sections ---------------------------------------------------------------

def _top_section(num: str | None, level: int) -> bool:
    """A top-level section: a whole-number heading (e.g. '1', '17') or a level-1 header."""
    if num is not None and re.fullmatch(r"\d+", num):
        return True
    return level == 1


def _first_top_header(md: str) -> tuple[str | None, str] | None:
    for line in md.splitlines():
        m = _HEADER_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        text = m.group(2)
        nm = _NUMBERED_RE.match(text)
        num = nm.group(1) if nm else None
        if _top_section(num, level):
            return num, text
    return None


# ---- main -------------------------------------------------------------------

def run(paths: Paths, n_pages: int, force: bool = False) -> dict:
    ocr_dir = paths.root / "ocr"
    paths.text.mkdir(parents=True, exist_ok=True)
    paths.tables.mkdir(parents=True, exist_ok=True)
    paths.images.mkdir(parents=True, exist_ok=True)

    # Build the per-page -> active-section assignment.
    sections: list[dict] = [
        {"idx": 0, "number": None, "title": "Front Matter", "page_start": 1, "source": "ocr_markdown"}
    ]
    page_section: dict[int, int] = {}
    page_md: dict[int, str] = {}

    for page in range(1, n_pages + 1):
        md_path = ocr_dir / f"page_{page:04d}.md"
        md = md_path.read_text() if md_path.exists() else ""
        page_md[page] = md
        hdr = _first_top_header(md)
        if hdr is not None:
            num, title = hdr
            sections.append(
                {
                    "idx": len(sections),
                    "number": num,
                    "title": title.strip("# ").strip(),
                    "page_start": page,
                    "source": "ocr_markdown",
                }
            )
        page_section[page] = len(sections) - 1

    for s, nxt in zip(sections, sections[1:] + [None]):
        s["page_end"] = (nxt["page_start"] - 1) if nxt else n_pages

    # Write per-section text files.
    page_class: list[dict] = []
    table_records: list[dict] = []

    for s in sections:
        # OCR header titles already include the section number (e.g. '1 Summary').
        s["slug"] = "front_matter" if s["idx"] == 0 else (slugify(s["title"]) or f"section_{s['idx']}")
        s["text_file"] = f"text/{s['idx']:02d}_{s['slug']}.md"
        body = "\n\n".join(
            page_md.get(p, "") for p in range(s["page_start"], s["page_end"] + 1)
        )
        header = (
            f"---\nsection: {s['number'] or ''}\ntitle: {s['title']}\n"
            f"pages: {s['page_start']}-{s['page_end']}\n---\n\n"
        )
        (paths.root / s["text_file"]).write_text(header + body)

    # Tables + figures, per page.
    for page in range(1, n_pages + 1):
        md = page_md.get(page, "")
        sec = sections[page_section[page]]
        tables = _parse_tables(md)
        page_json = read_json(ocr_dir / f"page_{page:04d}.json", default={}) or {}
        method = page_json.get("method", "mistral_ocr")
        table_meta = page_json.get("tables") or []
        for seq, rows in enumerate(tables, start=1):
            stem = f"{sec['slug']}_t{seq}__p{page:04d}"
            csv_path = paths.tables / f"{stem}.csv"
            with open(csv_path, "w", newline="") as f:
                csv.writer(f).writerows(rows)
            record = {
                "id": stem,
                "section": sec["number"],
                "section_slug": sec["slug"],
                "page": page,
                "csv": f"tables/{stem}.csv",
                "n_rows": len(rows),
                "n_cols": max((len(r) for r in rows), default=0),
                "method": method,
            }
            if seq <= len(table_meta) and table_meta[seq - 1].get("bbox"):
                record["bbox"] = table_meta[seq - 1]["bbox"]
            table_records.append(record)

        figs = []
        for seq, img in enumerate(page_json.get("images", []), start=1):
            rel = img.get("file")
            if not rel:
                continue
            src = ocr_dir / rel
            if not src.exists():
                continue
            ext = src.suffix.lstrip(".") or "png"
            fig_name = f"fig_p{page:04d}_{seq}.{ext}"
            shutil.copy2(src, paths.images / fig_name)
            figs.append(f"images/{fig_name}")

        n_chars = len(md.strip())
        cls = "table" if tables else ("figure" if figs and n_chars < 400 else "text")
        page_class.append(
            {
                "page": page,
                "n_chars": n_chars,
                "n_tables": len(tables),
                "n_figures": len(figs),
                "class": cls,
                "section": sec["number"],
            }
        )

    if any((read_json(ocr_dir / f"page_{page:04d}.json", default={}) or {}).get("method") == "digital_text" for page in range(1, n_pages + 1)):
        numbered = [section for section in sections if str(section.get("number") or "").isdigit()]
        if not numbered:
            raise ValueError("digital extraction produced no numbered report sections")

    write_json(paths.sections_json, sections)
    write_json(paths.page_class_json, page_class)
    write_json(paths.root / "tables_index.json", table_records)
    mark_stage_done(
        paths, STAGE, {"n_sections": len(sections), "n_tables": len(table_records)}
    )
    return {"n_sections": len(sections), "n_tables": len(table_records)}
