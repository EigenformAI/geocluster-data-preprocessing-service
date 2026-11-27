"""Extract stored main-body paragraphs and physical table cells without a model."""

import json
from pathlib import Path

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
)
from .word_package import read_body

TEXT_PATH = "document/body.txt"
BLOCKS_PATH = "document/blocks.jsonl"
GAP = "\ufffc"  # Marks skipped inline content, not a character recovered from the source.
MAX_TABLES = 512
RANGE_MARKERS = frozenset({
    "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd",
    "proofErr", "permStart", "permEnd",
})
RANGE_CONTEXTS = frozenset({"body", "p", "hyperlink", "fldSimple", "tbl", "tr", "tc"})


def is_annotation(element, word, parent):
    """Ignore only known empty annotations in contexts that admit them."""
    if not element.tag.startswith(word) or len(element) or (element.text or "").strip():
        return False
    name = element.tag[len(word):]
    return ((parent in RANGE_CONTEXTS and name in RANGE_MARKERS)
            or (parent == "r" and name in {"commentReference", "lastRenderedPageBreak"}))


def omission_kind(element, word):
    # Classify only the encountered element; opaque descendants are not evidence.
    return "drawing" if element.tag == word + "drawing" else "other"


def paragraph_text(paragraph, word):
    """Return stored text, whether it has source text, and bounded omission kinds."""
    fragments = []
    omitted = set()
    has_text = False
    inline = {word + name for name in ("r", "hyperlink", "fldSimple")}
    properties = {"p": {word + "pPr"}, "r": {word + name for name in ("rPr", "instrText", "fldChar")}}
    controls = {word + name: text for name, text in (
        ("tab", "\t"), ("br", "\n"), ("cr", "\n"), ("noBreakHyphen", "\u2011"), ("softHyphen", "\u00ad"),
    )}

    def visit(element):
        nonlocal has_text
        parent = element.tag[len(word):]
        for node in element:
            if node.tag in inline:
                visit(node)
            elif node.tag == word + "t" and not len(node):
                text = node.text or ""
                fragments.append(text)
                has_text |= bool(text.strip())
            elif node.tag in controls:
                fragments.append(controls[node.tag])
            elif node.tag not in properties.get(parent, ()) and not is_annotation(node, word, parent):
                omitted.add(omission_kind(node, word))
                fragments.append(GAP)

    visit(paragraph)
    return "".join(fragments), has_text, omitted


class WordAdapter:
    extensions = (".docx",)
    version = "2"

    def extract(self, snapshot: dict, profile: dict, output_dir: Path, context: ExtractionContext) -> Extraction:
        limits = limits_for(profile)
        part, word, body = read_body(snapshot["path"], limits)
        result = empty_extraction("docx", self.version)
        source = {"kind": "word-body", "part": part}
        remaining = limits["outputBytes"]

        def charge(size):
            nonlocal remaining
            remaining -= size
            if remaining < 0:
                raise ServiceError("RESOURCE_LIMIT", "Word products exceed the output-byte limit.")

        total = unsupported = total_rows = total_tables = text_bytes = 0
        has_text = has_rows = False
        (output_dir / "document").mkdir(parents=True, exist_ok=True)
        with (output_dir / TEXT_PATH).open("wb") as text_file, (output_dir / BLOCKS_PATH).open("wb") as block_file:
            for position, element in enumerate(body, 1):
                if element.tag == word + "sectPr" or is_annotation(element, word, "body"):
                    continue
                total += 1
                locator = source | {"block": position}
                if element.tag == word + "p":
                    text, contains_text, omitted = paragraph_text(element, word)
                    encoded = (text + "\n").encode("utf-8")
                    charge(len(encoded))
                    text_file.write(encoded)
                    record = {"kind": "paragraph", "source": locator,
                              "textOffset": text_bytes, "textBytes": len(encoded) - 1}
                    text_bytes += len(encoded)
                    has_text |= contains_text
                elif element.tag == word + "tbl":
                    total_tables += 1
                    if total_tables > MAX_TABLES:
                        raise ServiceError("RESOURCE_LIMIT", "Word document exceeds its table count limit.")
                    table_id = f"word_table_{position:06d}"
                    with TableWriter(result, output_dir, table_id, locator, limits, charge=charge) as writer:
                        writer.table["columnSemantics"] = "physical-cell-order"
                        rows, omitted, contains_text = extract_table(
                            element, word, writer, locator, limits["rows"] - total_rows)
                    total_rows += rows
                    has_rows |= rows > 0
                    record = {"kind": "table", "source": locator, "tableId": table_id}
                else:
                    record = {"kind": "omitted", "source": locator}
                    omitted = {omission_kind(element, word)}
                    contains_text = False
                if omitted:
                    unsupported += 1
                    record["incomplete"] = True
                    message = ("Drawing content was skipped in this body block." if omitted == {"drawing"}
                               else "Unsupported content was skipped in this body block.")
                    if contains_text:
                        message += " Stored text was retained."
                    finding(result, "WORD_CONTENT_OMITTED", message, source=locator)
                line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
                charge(len(line))
                block_file.write(line)

        if not has_text and not has_rows:
            raise ServiceError("INVALID_INPUT", "Word document contains no supported main-body text or table rows.")
        product(result, TEXT_PATH, "document-text", "text/plain; charset=utf-8", source=source)
        product(result, BLOCKS_PATH, "document-blocks", "application/x-ndjson", source=source)
        unit_coverage = coverage(
            "body-block", total, total - unsupported, unsupported=unsupported, notProcessed=0,
            excluded=["headers, footers, footnotes, endnotes, and comments", "formatting, numbering, table grid, and pagination",
                      "field instructions and recalculation", "linked targets and auxiliary package parts"],
        )
        result["coverage"] = unit_coverage
        result["stages"] = [
            stage_outcome("native-word-body", "partial" if unsupported else "completed", dict(unit_coverage)),
            stage_outcome("model-extraction", "not_applicable", reason="Word main-body content is extracted from stored XML."),
        ]
        return result


def extract_table(element, word, writer, locator, rows_remaining):
    rows = 0
    omitted = set()
    has_text = False
    for row in element:
        if row.tag != word + "tr":
            if row.tag not in {word + "tblPr", word + "tblGrid"} and not is_annotation(row, word, "tbl"):
                omitted.add(omission_kind(row, word))
            continue
        rows += 1
        if rows > rows_remaining:
            raise ServiceError("RESOURCE_LIMIT", "Word document exceeds the configured row limit.")
        row_source = locator | {"row": rows}
        cells = []
        for cell in row:
            if cell.tag != word + "tc":
                if cell.tag not in {word + "trPr", word + "tblPrEx"} and not is_annotation(cell, word, "tr"):
                    omitted.add(omission_kind(cell, word))
                continue
            if len(cells) >= writer.limits["columns"]:
                raise ServiceError("RESOURCE_LIMIT", "Word table exceeds the configured column limit.")
            fragments = []
            incomplete = False
            for block in cell:
                if block.tag == word + "p":
                    text, contains_text, skipped = paragraph_text(block, word)
                    fragments.append(text)
                    has_text |= contains_text
                    omitted.update(skipped)
                    incomplete |= bool(skipped)
                elif block.tag != word + "tcPr" and not is_annotation(block, word, "tc"):
                    fragments.append(GAP)
                    omitted.add(omission_kind(block, word))
                    incomplete = True
            value = {"type": "text", "value": "\n".join(fragments),
                     "source": row_source | {"cell": len(cells) + 1}}
            if incomplete:
                value["incomplete"] = True
            cells.append(value)
        writer.add(cells, row_source)
    return rows, omitted, has_text
