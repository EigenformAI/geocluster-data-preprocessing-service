---
name: peqa
description: Use when writing cited QP-style answers, memos, or findings from hierarchical geo-report metadata.
---

# PEQA

Compose answers from selected metadata, deterministic metrics, flags, and cited evidence.

Procedure:

1. Load context with `report_load(slug)`.
2. Pull metrics through deterministic tools: `report_compute_intercepts`, `report_compute_resource`, `report_check_qaqc`, and `report_compute_tenure`.
3. Draft claims as short statements with citations to evidence ids or element ids.
4. Call `report_verify_claim(text, citations)` for every quantitative claim.
5. Mark failed claims as `[UNVERIFIED]` and include them in limitations or findings.

Rules:

- Do not compute, round, or reformat report numbers in prose unless the number comes from a deterministic compute tool or an exact source token.
- Prefer exact source-token wording for intervals, grades, tonnes, and contained metal.
- Keep DKI claims separate from report claims.
