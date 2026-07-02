---
name: hie
description: Use when building or refreshing hierarchical report_metadata.json for extracted NI 43-101 report directories under output/<slug>.
---

# HIE

Build the deterministic Hierarchical Information Extraction foundation before answering analytical questions.

Procedure:

1. Resolve the report slug to `output/<slug>`; never use arbitrary paths in MCP calls.
2. If `output/<slug>/analysis/report_metadata.json` is missing or stale, run `uv run analyze-report output/<slug> --force` from the repo root.
3. Use `report_load(slug)` to inspect stats, project stage, location candidates, and hierarchy counts.
4. Use `report_get_records(slug, type, filters)` for typed records; do not read table numbers from prose.
5. Treat `build_trace.warnings` and `hierarchy/unresolved.json` as limitations, not facts to smooth over.

Rules:

- The model has no numeric authority. Numbers must come from `report_get_records`, `report_compute_*`, or cited evidence.
- Map-like figures may have `map_meta.status = skipped` until peace-tool-pool map HIE is available; surface that as a limitation.
- If a hierarchy link is unresolved, say it is unresolved instead of inferring the parent.
