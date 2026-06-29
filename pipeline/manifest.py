"""Stage 7 MANIFEST: tie everything together (manifest.json + summary.md)."""

from __future__ import annotations

from .util import Paths, mark_stage_done, read_json, write_json

STAGE = "manifest"


def run(paths: Paths) -> dict:
    doc = read_json(paths.doc_json, default={}) or {}
    sections = read_json(paths.sections_json, default=[]) or []
    tables = read_json(paths.root / "tables_verified.json", default=[]) or []
    figures = read_json(paths.root / "figures_index.json", default=[]) or []

    low = [t for t in tables if t.get("confidence") == "low"]
    unver = [t for t in tables if t.get("confidence") == "unverifiable"]
    high = [t for t in tables if t.get("confidence") == "high"]
    stats = {
        "n_pages": doc.get("n_pages"),
        "n_sections": len(sections),
        "n_tables": len(tables),
        "n_tables_high_confidence": len(high),
        "n_tables_low_confidence": len(low),
        "n_tables_unverifiable": len(unver),
        "n_figures": len(figures),
    }
    manifest = {
        "doc_id": doc.get("doc_id"),
        "report_slug": doc.get("report_slug"),
        "source_name": doc.get("source_name"),
        "sha256": doc.get("sha256"),
        "stats": stats,
        "sections": sections,
        "tables": tables,
        "figures": figures,
        "review_queue": [t["id"] for t in low + unver],
    }
    write_json(paths.manifest_json, manifest)

    def _table_line(t: dict) -> str:
        chk = t.get("text_layer_checksum", {})
        return (
            f"- `{t['id']}` (p{t['page']}) — coverage {chk.get('coverage')}, "
            f"rotated={t.get('rotation_deg')}deg, missing {len(chk.get('missing', []))} number(s)"
        )

    lines = [
        f"# {doc.get('report_slug', paths.root.name)} — extraction summary",
        "",
        f"- Source: `{doc.get('source_name', '')}`  ({doc.get('n_pages')} pages)",
        f"- Sections: {stats['n_sections']}",
        f"- Tables: {stats['n_tables']}  "
        f"(high: {stats['n_tables_high_confidence']}, "
        f"suspect: {stats['n_tables_low_confidence']}, "
        f"unverifiable: {stats['n_tables_unverifiable']})",
        f"- Figures: {stats['n_figures']}",
        "",
        "Open each CSV next to its `.crop.png` in `tables/` to verify against the source.",
        "",
        "## Suspect tables — digital text present but OCR numbers disagree (likely OCR error)",
        "",
    ]
    lines += [_table_line(t) for t in low] or ["None."]
    lines += [
        "",
        "## Unverifiable tables — image/scanned page, no digital text layer to check against",
        "",
    ]
    lines += [_table_line(t) for t in unver] or ["None."]
    lines.append("")

    paths.summary_md.write_text("\n".join(lines))
    mark_stage_done(paths, STAGE, stats)
    return stats
