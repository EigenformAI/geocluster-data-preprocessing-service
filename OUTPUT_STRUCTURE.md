# Output structure

Each input PDF is decomposed into its **own directory** under `output/<report_slug>/`
(the slug is the lowercased, underscore-separated file stem — e.g.
`Gold_X2_Mining.pdf` → `gold_x2_mining`). The four reports are fully independent.

```
output/<report_slug>/
├── source.pdf              # verbatim copy of the input PDF (provenance)
├── doc.json               # report identity + metadata
├── manifest.json          # ⭐ machine-readable index of everything (entry point)
├── summary.md             # ⭐ human-readable report card + review queue
├── sections.json          # section list with page ranges
├── page_class.json        # per-page classification (text / table / figure)
│
├── pages/                 # full-page renders, 200 DPI
│   └── page_0001.png …
├── text/                  # narrative text, one file per section
│   ├── 00_front_matter.md
│   └── 05_1_summary.md …
├── tables/                # one CSV per table (+ a crop image + metadata each)
│   ├── <id>.csv
│   ├── <id>.crop.png
│   └── <id>.meta.json
├── images/                # extracted figures + LLM descriptions
│   ├── fig_p0012_1.jpg
│   └── fig_p0012_1.description.md
│
└── (working files — safe to ignore or delete; regenerated on re-run)
    ├── ground_truth.json  # pdftotext word boxes (the table auditor)
    ├── ocr/               # raw Mistral OCR output (markdown + images per page)
    ├── tables_index.json  # internal table list
    ├── tables_verified.json
    ├── figures_index.json
    ├── stage_state.json   # which pipeline stages have completed
    └── .cache/mistral/    # cached OCR/vision responses (so re-runs don't re-bill)
```

The two starred files — `manifest.json` (for code) and `summary.md` (for people)
— are the intended entry points. Everything else they reference by relative path.

---

## Primary deliverables

### `manifest.json` — the index

The single structured file that ties a report together; consume this from the
downstream pipeline.

```jsonc
{
  "doc_id": "7886894179a1",            // sha256(pdf)[:12]
  "report_slug": "gold_x2_mining",
  "source_name": "Gold_X2_Mining.pdf",
  "sha256": "7886894179a1…",
  "stats": {
    "n_pages": 110, "n_sections": 24, "n_tables": 29,
    "n_tables_high_confidence": 23,
    "n_tables_low_confidence": 0,       // "suspect" — see Table confidence below
    "n_tables_unverifiable": 6,
    "n_figures": 37
  },
  "sections": [ … ],                    // same objects as sections.json
  "tables":   [ … ],                    // same objects as each tables/<id>.meta.json
  "figures":  [ {"id","page","image","description_md"} … ],
  "review_queue": [ "<table id>", … ]   // every table that isn't high-confidence
}
```

### `summary.md` — the report card

Counts at the top, then the **review queue** split into two sections:
*suspect* tables (digital text present but OCR numbers disagree) and
*unverifiable* tables (image/scanned pages with no digital text to check). Start
table QA here.

### `doc.json` — identity

`report_slug`, `source_name`, `doc_id`, `sha256`, `n_pages`, `producer`,
`page_size`, `encrypted`.

### `pages/page_NNNN.png`

A 200-DPI render of every page (1-indexed, zero-padded to 4 digits). Used as the
source for table crops and figure capture, and useful as a visual reference.

### `text/NN_<slug>.md` — narrative by section

One markdown file per section (the `NN` prefix preserves document order). Each
begins with a small YAML header, then the section's text (from the OCR markdown):

```markdown
---
section: 1
title: 1 Summary
pages: 5-11
---

# 1 Summary
…
```

`00_front_matter.md` holds anything before the first numbered section.

### `sections.json`

Ordered list describing how the report was segmented (headings come from the OCR
markdown). One object per section:

```jsonc
{ "idx": 5, "number": "1", "title": "1 Summary",
  "page_start": 5, "page_end": 11,
  "slug": "1_summary", "text_file": "text/05_1_summary.md",
  "source": "ocr_markdown" }
```

### `page_class.json`

One object per page: `{ "page", "n_chars", "n_tables", "n_figures", "class", "section" }`
where `class ∈ {text, table, figure}`.

---

## Tables — the load-bearing part

Each table produces **three co-located files** sharing one id:

| File | Contents |
|---|---|
| `tables/<id>.csv` | the table data |
| `tables/<id>.crop.png` | the same region cropped from the page render — open it next to the CSV to verify by eye |
| `tables/<id>.meta.json` | provenance + the fidelity check |

**Id format:** `<section_slug>_t<seq>__p<page>` — e.g.
`4_property_description_and_location_t1__p0019` is the 1st table on page 19,
in section 4. (A few tables have no `.crop.png` when no source region could be
located — their `crop_png` is `null`.)

### `tables/<id>.meta.json`

```jsonc
{
  "id": "4_property_description_and_location_t1__p0019",
  "page": 19, "section": "4",
  "method": "digital_text",          // how this CSV was produced (see below)
  "n_rows": 53, "n_cols": 20,
  "rotation_deg": 0,                  // 90 if the table was detected sideways
  "text_layer_checksum": {           // cross-check vs the pdftotext layer
    "csv_numbers": 761,              // numeric tokens in the CSV
    "found": 761,                    // …also present in the page's digital text
    "missing": [],                   // CSV numbers NOT backed by the text layer
    "coverage": 1.0,                 // found / csv_numbers
    "ragged_rows": false,
    "page_digit_count": 1530,        // numeric tokens pdftotext found on the page
    "pass": true
  },
  "confidence": "high",
  "csv": "tables/….csv",
  "crop_png": "tables/….crop.png"    // or null
}
```

### Table `method` — where the numbers came from

| `method` | Meaning |
|---|---|
| `digital_text` | Rebuilt **deterministically from the pdftotext word boxes**, because OCR's numbers disagreed with the digital text. Values are the authentic digital text by construction. |
| `vision_hybrid` | A **side-by-side (N-up) page** where clustering jams the columns. A vision model (`claude-sonnet-4-6`) read the table **structure** only; every value was then filled from the pdftotext layer and reconciled against it (numbers it can't back are flagged). The model supplies layout, never a number — so values are still the authentic digital text. |
| `mistral_ocr` | Extracted from Mistral OCR's markdown. |
| `vision_unverified` | An **image-only table page** (no digital text layer) transcribed by the Sonnet vision hybrid **alone**, after auto-rotating the page upright. There is no text layer to reconcile against, so every value is `unverifiable` by construction. Produced only by `--vision-image-tables`. |

### Table `confidence` — how much to trust it

| `confidence` | Meaning | What to do |
|---|---|---|
| `high` | Numbers are backed by the digital text layer (either rebuilt from it, or OCR matched it). | Trust the numbers; structure still worth a glance via the crop. |
| `low` (*suspect*) | The page has a digital text layer but the table's numbers don't match it — likely an OCR transcription error that couldn't be auto-repaired. | **Review by hand** against the `.crop.png`. |
| `unverifiable` | Image/scanned page — there is no digital text layer to check against, so OCR (or the `vision_unverified` reader) is the only source. | **Review by hand**; cannot be auto-verified. |

Why this matters: Mistral OCR sometimes alters numbers in dense tables. The
pipeline cross-checks every table against an independent extraction (pdftotext),
rebuilds the ones it can, and **flags the rest instead of silently trusting them**
— so the CSVs are checkable, per the project requirement.

---

## Figures

Each extracted figure produces two co-located files:

| File | Contents |
|---|---|
| `images/fig_pNNNN_S.<ext>` | the figure image (page `NNNN`, sequence `S`; ext is usually `.jpg`/`.png`) |
| `images/fig_pNNNN_S.description.md` | a short factual LLM description (`ministral-14b-2512`) — figure type, what it depicts, visible labels/legends/scale |

The `figures` array in `manifest.json` lists `{id, page, image, description_md}`
for each.

---

## Working / intermediate files

Not part of the deliverable contract — they are pipeline internals, are
regenerated on re-run, and are safe to delete:

- `ground_truth.json` — per-page pdftotext word boxes (token + bbox); the
  independent text layer used to audit tables and to rebuild `digital_text` ones.
- `ocr/` — raw Mistral OCR output: `page_NNNN.md`, `page_NNNN.json` (image bboxes),
  and `ocr/img/` (images OCR extracted).
- `tables_index.json`, `tables_verified.json`, `figures_index.json` — internal
  indexes the stages pass between each other.
- `stage_state.json` — records which stages have completed (drives resume/skip).
- `.cache/mistral/` — content-hashed cache of OCR/vision responses so re-runs are
  free.

---

# Analysis output — `analysis/`

The **analysis pipeline** (a separate stage; see [PIPELINE.md](PIPELINE.md) and
[HOW_TO_USE.md](HOW_TO_USE.md) §4) reads a completed extraction and writes a
**grounded evidence digest + conservative-QP assessment** into a single subtree,
`output/<report_slug>/analysis/`. It runs `HIE → metrics → flags → DKI → PEQA →
findings`. It never mutates the extraction artifacts above; it only consumes them.

```
output/<report_slug>/analysis/
├── memo.md                 # ⭐ human-readable digest + QP memo (entry point)
├── findings.json           # ⭐ machine-readable handoff (every item carries provenance)
├── flags.json              # red-flag rules that fired (severity + evidence)
├── report_metadata.json    # the HIE metadata blob: report-wide graph + build_trace/counters
├── analysis_state.json     # per-stage state + counters (drives resume/skip)
├── assertions.json         # every harvested grounded assertion (verbatim span + citation)
├── evidence_index.json     # evidence-id → source span / citation lookup
├── input_index.json        # normalized index of the extraction inputs HIE consumed
│
├── hie/                    # HIE per-element artifacts
│   ├── tables/<table_id>.json    # per-table typed records + column mapping + guard
│   ├── figures/<figure_id>.json  # per-figure classification + map / georef candidates
│   ├── maps/<figure_id>/…         # map-processing cache (peace detector output, if maps on)
│   └── sections/                  # (reserved; often empty)
├── hierarchy/              # reduced report-wide entity graph
│   ├── entities.json
│   ├── relationships.json
│   ├── assertions.json
│   └── unresolved.json
├── metrics/               # Stage: deterministic, cited metrics
│   ├── _stage.json         # project-stage classification + record counts
│   ├── resource.json
│   ├── intercepts.json
│   ├── qaqc.json
│   └── tenure.json
├── records/               # clean typed-records view — one file per typed table
│   ├── <table_id>.json
│   └── _index.json
├── dki/                   # domain-knowledge injection
│   └── report.json         # routing + which grounded assertions were surfaced
├── peqa/                  # prompt-enhanced question answering
│   ├── answers/report.json # the QP answer + assembled answer-pack + validated claims
│   └── prompts/            # (reserved; often empty)
├── ablations/             # (reserved; ablation experiments)
│
└── .cache/                # cached model responses (re-runs don't re-call)
    └── llm/opencode/<key>.json
```

The two starred files are the intended entry points: `memo.md` for a person,
`findings.json` for the downstream pipeline. `report_metadata.json` is the
authoritative HIE blob everything else is derived from.

> **The one invariant:** the LLM has **zero numeric authority**. Every number in
> `records/` must already exist in its source CSV (else the record is quarantined
> by `numguard`); every number in `memo.md` must trace to a verified table or be
> stamped `[UNVERIFIED]`, and every prose assertion must be a verbatim substring
> of the source (`textguard`). The numbers and text are produced and checked by
> Python, never invented by the model.

---

## Primary deliverables

### `memo.md` — the digest + QP memo (for people)

The memo leads with **grounded recall** and demotes the QP opinion to a
subordinate section. Its structure:

```
# <slug> - Grounded evidence digest
## Grounded Evidence Digest
### NI 43-101 Item <n>          # one subsection per NI 43-101 item with grounded content (data-driven)
## Verified Metric Claims
## Deterministic Flags
## QP Assessment                # renders ONLY if the LLM answerer ran
### Validated QP Claims
## Limitations
## Reasoning Boundary
```

Every claim that states content is annotated:

| Annotation | Where | Meaning |
|---|---|---|
| `_[<section_slug> p.N]_` | Grounded Evidence Digest | **Grounded assertion** — the sentence is a verbatim span from that section, page `N`. |
| `_[<table_id> p.N]_` | Verified Metric Claims | **Verified metric** — the number traces to that table and our recompute reconciles. |
| `**[UNVERIFIED]**` | QP Assessment / Validated QP Claims | The claim states a number/text not backed by the evidence (or carries no resolvable citation). Shown, but not trusted. |

The bias is deliberate: a correct figure quoted in a non-source form (e.g.
"271,000 t" when the table wrote "271" kt) is stamped `[UNVERIFIED]` rather than
let through — false negatives over false positives. The **QP Assessment** section
is absent entirely when the run was deterministic-only (no answerer).

### `findings.json` — the handoff (for code)

```jsonc
{
  "report_slug": "gold_x2_mining",
  "project_stage": "target",            // resource | target | early | desktop
  "resource":   [ … ],                  // recomputed resource rows (from metrics/resource.json)
  "intercepts": [ … ],                  // top significant intercepts
  "qaqc":   { … },                      // from metrics/qaqc.json
  "tenure": { … },                      // from metrics/tenure.json
  "flags":  [ … ],                      // same objects as flags.json
  "claims": [                           // every memo claim, with its validation verdict
    { "text": "- The top captured interval is 45.1 m at 6.2 g/t Au.",
      "evidence_origin": "…",
      "citations": [ … ], "resolved_citations": [ … ],
      "confidence": "…", "origin": "…",
      "validated": true,
      "bad_numbers": [], "missing_citations": [], "unbacked_text": [],
      "tagged_text": "…_[6_history_t1__p0055 p.55]_" }
  ],
  "unverified_claims": [ … ],           // the subset where validated == false
  "dki_warnings": [ … ],                // DKI provider/coverage warnings (NEW)
  "grounded_digest": {                  // the digest surfaced in memo.md (NEW)
    "assertions":          [ … ],       // every grounded assertion (verbatim + citation)
    "assertions_by_item":  { "4": [ … ], "7": [ … ] },   // keyed by NI 43-101 item
    "assertions_by_class": { "geological_risk": [ … ], "legal_jurisdictional": [ … ], "design_maturity": [ … ] }
  },
  "qp_assessment": "…"                  // the QP narrative prose, or "" if no answerer ran (NEW)
}
```

### `flags.json` — red-flag rules

```jsonc
{
  "stage": "target",
  "flags": [
    {
      "id": "missing_qaqc",
      "severity": "high",               // low | medium | high
      "category": "qaqc",
      "message": "QA/QC data was not detected while assay/resource data was captured …",
      "evidence": [ { "element_id": "6_history_t1__p0055", "page": 55 }, … ]
    }
  ]
}
```

Rules include: missing QA/QC; QA/QC failure; any claim resting on a `low`/
`unverifiable` table; resource-vs-recompute mismatch; sparse drilling for a
resource; intercept-containment violations; reliance on non-43-101 historical data.

### `report_metadata.json` — the HIE blob

The report-wide graph produced by HIE (large: sections, elements, entities,
assertions, locations, project stage). It also carries a top-level `build_trace`
whose `counters` are the run-wide telemetry the batch runner scrapes. Counter
families (see also `analysis_state.json`):

- `input_index.*`, `hie.n_{sections,tables,figures,maps_processed,map_components,image_labels,related_figure_links}`
- `imageguard.{version,n_corroborated,n_plausible,n_unverifiable,n_rejected}` — image-read verification gate
- `hierarchy.n_{entities,relationships,unresolved}`, `guard.n_rejected`
- `llm.n_{calls,cache_hits,retries,degraded,throttled}`, `llm.tokens.{total,input,output,reasoning}`
- `vlm.n_{calls,low_readability_skipped}`, `vlm.tokens.*`
- `map_table.n_{confident_other_skipped,fallback_other}`, `georef.n_processed`
- `schema.validation_{passed,failed}`
- `harvest.n_{assertions,rejected_by_textguard,llm_spans_added_over_lexical,llm_degraded}`

> **Token counters are cache-miss-only.** `llm.tokens.*` / `vlm.tokens.*` accrue
> only on live model calls; a warm (cached) run reports ~0 tokens. Run cold
> (`--force`, cleared cache) to measure true spend.

---

## The evidence chain

### `hie/tables/<table_id>.json` — per-table (HIE)

The HIE per-table artifact. The LLM only **classified the table and mapped its
columns**; values are lifted from the source CSV. Keys:

```jsonc
{
  "table_id": "…", "page": 55, "table_type": "assay_interval",
  "confidence": "high", "method": "mistral_ocr",
  "mapping": { … }, "mapping_meta": { … },   // the column map + its provenance/verdict
  "records": [ … ], "rejected": [ … ], "n_records": 74,
  "evidence": { … },                         // evidence ids for mapped fields
  "guard": { "status": "checked", "n_rejected": 0, "source_numbers": 29 },
  "source_sha256": "…", "mapper_cache_key": "…"
}
```

### `hie/figures/<figure_id>.json` — per-figure (HIE)

```jsonc
{
  "figure_id": "fig_p0013_1", "page": 13,
  "figure_class": "map", "figure_class_provenance": { … },
  "classification": { … },
  "map_candidate": true, "map_candidate_provenance": { … },
  "georeference_candidate": { … },
  "map_meta": { … }                          // component crops, labels, legend reads
}
```

`hie/maps/<figure_id>/…` holds the map-processing cache (the peace detector's
regions/artifacts), present only when `ANALYSIS_MAP_BACKEND=peace`.

### `hierarchy/` — the reduced graph

`entities.json`, `relationships.json`, `assertions.json`, `unresolved.json` — the
report-wide entity/relationship graph derived by the reduce step (entities and
the edges between them, the grounded assertions that survived, and spans that
could not be resolved to an entity).

### `records/<table_id>.json` — clean typed-records view

One file per typed table (matches the extraction table id). Values come from the
source CSV; every field carries an `evidence_id`, and numeric fields carry raw +
normalized units (the model emits only `raw_value`; `units.py` converts).

```jsonc
{
  "table_id": "1_summary_t1__p0011",
  "table_type": "budget",
  "records": [
    {
      "_id": "1_summary_t1__p0011:r1", "_type": "budget", "_row": 1,
      "provenance": { "element_id": "…", "table_id": "…", "page": 11,
                      "confidence": "high", "method": "vision_hybrid" },
      "phase": "Phase 1",
      "item": "Existing geophysical data compilation …",
      "amount": { "value": 800000.0, "unit": "CDN$",
                  "raw_value": "$800,000", "raw_unit": "CDN$",
                  "evidence_id": "ev:1_summary_t1__p0011:r1:amount" },
      "_field_evidence": { "phase": "ev:…:phase", "item": "ev:…:item", "amount": "ev:…:amount" }
    }
  ],
  "rejected": [],                       // records quarantined by the anti-fabrication guard
  "guard": { "status": "checked",       // "checked" | "ungated" (unverifiable source table)
             "n_rejected": 0, "source_numbers": 21 },
  "n_records": 11
}
```

`records/_index.json` is a compact list — `[{ "table_id", "table_type", "page",
"confidence", "n_records" }, …]`.

**`table_type`** is one of: `resource_estimate`, `assay_interval`,
`drillhole_collar`, `drill_history`, `qaqc_crm`, `tenure`, `budget`, or `other`
(tables the mapper can't confidently type / with too few numbers — kept with
`n_records: 0`). `guard.status: "ungated"` marks records from an `unverifiable`
source table — kept for reference but **excluded from all quantitative claims**.

### `metrics/*.json` — deterministic metrics

Deterministic Python over the typed records. Every metric file carries an
`applicable` flag (with a `reason` when false — so downstream can tell *"skipped
by design"* from *"missing"*), and, when applicable, a `formula` string and
per-row provenance.

`metrics/_stage.json` — project-stage classification derived from *which* record
types are present:

```jsonc
{ "stage": "target",                    // resource | target | early | desktop
  "record_counts": { "budget": 22, "tenure": 519, "drill_history": 235,
                     "drillhole_collar": 609, "assay_interval": 335 } }
```

`metrics/resource.json` (when applicable):

```jsonc
{ "applicable": true,
  "formula": "contained_koz = tonnage_t * grade_gpt / 31.1035 / 1000",
  "n_rows": 189, "n_recompute_mismatches": 13,   // rows where reported ≠ recomputed (>±2%)
  "rows": [ { "deposit": …, "category": …, "tonnage_t": …, "grade_gpt": …,
              "contained_reported_koz": 33.6, "contained_recomputed_koz": 33.65,
              "recompute_ok": true, "is_subtotal": false,
              "confidence": "high", "provenance": { … } } ] }
// when not applicable: { "applicable": false, "reason": "…" }
```

`metrics/intercepts.json`:

```jsonc
{ "applicable": true, "cutoff_gpt": 0.3,
  "formula": "gram_metres = interval_m * au_gpt; length-weighted grade",
  "n_intervals": 28, "n_parent_intervals": 22, "n_significant_at_cutoff": 17,
  "containment_violations": [ … ], "top_intercepts": [ … ], "intervals": [ … ] }
```

`metrics/qaqc.json`:

```jsonc
{ "applicable": true,
  "formula": "bias = (mean_measured - certified) / certified * 100",
  "n_crms": 6, "overall": "fail",       // pass | marginal | fail | absent
  "crms": [ { "crm_id": "OREAS 229b", "bias_pct": -3.35, "pass_rate_pct": 91.0, … } ] }
// when absent: { "applicable": false, "reason": "no QA/QC tables tabulated" }
```

`metrics/tenure.json`:

```jsonc
{ "applicable": true, "n_records": 519, "total_area_ha": 5553.5,
  "n_with_area": 296, "expiry_dates": [ … ] }
```

### `dki/report.json` — domain-knowledge injection

```jsonc
{
  "question": "Assess the project evidence and main technical risks.",
  "location": { … },                    // resolved project location (for geospatial experts)
  "routing": { … },                     // question → taxonomy-class routing decision
  "tool_pool": [ … ], "gathered": [ … ], "selected": [ … ],
  "warnings": [ … ],
  "counters": { "dki.n_experts_selected": …,
                "dki.n_assertions_surfaced_by_class": { "geological_risk": 60, … },
                "coverage": { "items_covered": [4,7,11,12,14,25,26] } }
}
```

### `peqa/answers/report.json` — question answering

```jsonc
{
  "answer": "…",                        // the QP narrative (empty if no answerer ran)
  "answer_pack": {                      // the assembled context/prompt pack fed to the answerer
    "schema_version": …, "question": …, "question_class": …,
    "report_context": …, "selected_assertions": [ … ], "source_snippets": [ … ],
    "grounded_digest": { … }, "tables_metrics": { … }, "figure_crops": [ … ],
    "dki_bundles": [ … ], "tool_pool": [ … ], "coverage": { … },
    "open_gaps": [ … ], "confidence": …, "flags": [ … ], "required_output_schema": { … }
  },
  "claims": [ … ],                      // same claim objects as findings.json → claims
  "unverified_claims": [ … ]
}
```

### `assertions.json` / `evidence_index.json` / `input_index.json`

- `assertions.json` — every grounded assertion HIE harvested (verbatim span +
  taxonomy class + source citation); the superset the digest and DKI draw from.
- `evidence_index.json` — maps each `evidence_id` (e.g. `ev:<table>:<row>:<field>`)
  back to its source span / citation; how any claim or record field is traced.
- `input_index.json` — the normalized index of extraction inputs (sections,
  tables, figures) HIE consumed.

---

## `analysis_state.json` — stage state

Records which analysis stages have completed, a one-line summary of each, and a
per-stage `counters` block (plus a `trace_id`), so re-runs skip finished work.
Not a deliverable. Top-level keys are the stages, in order:

```jsonc
{ "input_index": { "trace_id": …, "counters": { "input_index.n_elements": … } },
  "hie":     { "done": true, "counters": { "hie.n_tables": 34, "hie.n_figures": 37,
                                           "llm.tokens.total": …, "vlm.n_calls": …, … } },
  "metrics": { "counters": { "metrics.n_record_types": 5 } },
  "flags":   { "counters": { "flags.n_flags": 3 } },
  "dki":     { "counters": { "dki.n_experts_selected": …, … } },
  "peqa":    { "counters": { "peqa.n_claims": 120, "peqa.n_unverified": 0,
                             "answer.tokens.total": … } } }
```

`.cache/llm/opencode/` holds content-hashed model responses — re-running the
analysis re-calls nothing unless the evidence changed (and cache hits accrue no
tokens).
