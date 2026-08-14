---
name: dki
description: Use when injecting compliance or geospatial domain knowledge for a report-analysis question after HIE metadata exists.
---

# DKI

Inject external knowledge only after loading the verified report metadata.

Procedure:

1. Start with `report_load(slug)` and select the tightest reliable `location_candidates` entry.
2. For compliance questions, use the local DKI bundle written by `uv run analyze-report output/<slug>` and the deterministic flags from `report_flags(slug)`.
3. For geospatial questions, call peace-tool-pool tools only when a reliable location or georeferenced map exists.
4. Preserve provider warnings. A skipped provider or missing bounds is itself an analysis limitation.

Rules:

- DKI evidence is `evidence_origin = dki` and `source_kind = knowledge_item`; it is not a report fact.
- Do not use external knowledge to validate a report number.
- If geospatial providers are unavailable, answer with the report-local evidence and state the skipped DKI explicitly.
