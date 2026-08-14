---
name: report-analysis
description: Use when running the full HIE -> DKI -> PEQA workflow for an extracted geo-report output slug.
---

# Report Analysis

Run the full workflow over one shared verified substrate.

Procedure:

1. HIE: ensure `output/<slug>/analysis/report_metadata.json` exists by running `uv run analyze-report output/<slug>` if needed.
2. Load: call `report_load(slug)` and inspect project stage, location candidates, and warnings.
3. DKI: gather compliance/geospatial knowledge only when relevant and preserve warnings.
4. Metrics: call deterministic `report_compute_*` tools for quantitative summaries.
5. PEQA: verify every numeric claim with `report_verify_claim` before presenting it.

Output expectations:

- Cite report facts by element/evidence id and page where available.
- Keep unresolved hierarchy links and skipped map HIE visible as limitations.
- Never let model-written prose be the source of a number.
