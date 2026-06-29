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

### Table `confidence` — how much to trust it

| `confidence` | Meaning | What to do |
|---|---|---|
| `high` | Numbers are backed by the digital text layer (either rebuilt from it, or OCR matched it). | Trust the numbers; structure still worth a glance via the crop. |
| `low` (*suspect*) | The page has a digital text layer but the table's numbers don't match it — likely an OCR transcription error that couldn't be auto-repaired. | **Review by hand** against the `.crop.png`. |
| `unverifiable` | Image/scanned page — there is no digital text layer to check against, so OCR is the only source. | **Review by hand**; cannot be auto-verified. |

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

The **analysis pipeline** (a separate stage; see [PIPELINE.md](PIPELINE.md) Part 2)
reads a completed extraction and writes a **conservative-QP assessment** into a
single subtree, `output/<report_slug>/analysis/`. It never mutates the extraction
artifacts above; it only consumes them.

```
output/<report_slug>/analysis/
├── memo.md               # ⭐ human-readable QP due-diligence memo (entry point)
├── findings.json         # ⭐ machine-readable handoff (every item carries provenance)
├── flags.json            # red-flag rules that fired (severity + evidence)
├── analysis_state.json   # which analysis stages have completed (drives resume/skip)
│
├── records/              # Stage A — one typed-records file per data table
│   ├── <table_id>.json
│   └── _index.json
├── metrics/              # Stage B — deterministic, cited metrics
│   ├── _stage.json       # project-stage classification + record counts
│   ├── resource.json
│   ├── intercepts.json
│   ├── qaqc.json
│   └── tenure.json
│
└── .cache/mistral/       # cached normalize/assess LLM responses (re-runs don't re-bill)
```

The two starred files are the intended entry points: `memo.md` for a person,
`findings.json` for the downstream pipeline. Everything else is the evidence
chain behind them.

> **The one invariant:** the LLM has **zero numeric authority**. Every number in
> `records/` must already exist in its source CSV (else the record is quarantined);
> every number in `memo.md` must trace to a verified table or be stamped
> `[UNVERIFIED]`. The numbers are produced and checked by Python, never invented
> by the model.

---

## Primary deliverables

### `memo.md` — the QP memo (for people)

A skeptical, structured due-diligence write-up: *Scope → Project stage → Mineral
resources → Drilling & intercepts → QA/QC → Tenure → Red flags → Conclusion*
(sections marked not-applicable are skipped with a reason). Every bullet that
states a number is annotated:

| Annotation on a claim | Meaning |
|---|---|
| `_[<table_id> p.N]_` | **Verified** — the number traces to that table and our recompute reconciles; the citation resolves in `manifest.json`. |
| `**[UNVERIFIED]**` | The claim states a number that isn't backed by the evidence (or carries no resolvable citation). Shown, but not trusted. |

The bias is deliberate: a correct figure quoted in a non-source form (e.g.
"271,000 t" when the table wrote "271" kt) is stamped `[UNVERIFIED]` rather than
let through — false negatives over false positives.

### `findings.json` — the handoff (for code)

```jsonc
{
  "report_slug": "new_found_gold_corp_tech_report",
  "project_stage": "resource",          // resource | target | early | desktop
  "resource":   [ … ],                  // recomputed resource rows (from metrics/resource.json)
  "intercepts": [ … ],                  // top significant intercepts
  "qaqc":   { "overall": "fail", "crms": [ … ] },
  "tenure": { … },
  "flags":  [ … ],                      // same objects as flags.json
  "claims": [                           // every memo claim, with its validation verdict
    { "section": "Mineral Resources",
      "text": "- Orion deposit: Indicated resource of 598,000 tonnes at 1.75 g/t Au …",
      "citations": [ { "table_id": "1_t1__p0048", "page": 48 } ],
      "cite_str": "1_t1__p0048 p.48",
      "validated": true }
  ],
  "unverified_claims": [ … ]            // the subset where validated == false
}
```

### `flags.json` — red-flag rules

```jsonc
{
  "stage": "resource",
  "flags": [
    {
      "id": "qaqc_performance",
      "severity": "high",               // low | medium | high
      "category": "qaqc",
      "message": "QA/QC overall 'fail': OREAS 229b bias -3.35% pass 91.0% …",
      "evidence": [ { "table_id": "10_t1__p0231", "page": 231 }, … ],
      "rule_version": "v1"
    }
  ]
}
```

Rules include: missing QA/QC; QA/QC failure; any claim resting on a `low`/
`unverifiable` table; resource-vs-recompute mismatch; sparse drilling for a
resource; intercept-containment violations; reliance on non-43-101 historical data.

---

## Stage A — `records/<table_id>.json`

One file per data table the normalizer typed (`<table_id>` matches the extraction
table id, e.g. `10_t1__p0231`). The LLM only **classified the table and mapped its
columns**; the values are lifted from the source CSV.

```jsonc
{
  "table_id": "10_t1__p0231",
  "page": 231,
  "table_type": "qaqc_crm",            // see table_type values below
  "confidence": "high",                // inherited from the source table's meta
  "method": "mistral_ocr",
  "carry_fields": ["crm_id"],          // identifier columns forward-filled (ffill)
  "records": [
    {
      "_type": "qaqc_crm",
      "_row": 1,
      "provenance": { "table_id": "10_t1__p0231", "page": 231,
                      "confidence": "high", "method": "mistral_ocr" },
      "crm_id": "OREAS 218",
      // every numeric field carries raw + normalized units; the model emits only
      // raw_value (which must exist in the source CSV); units.py does the conversion:
      "certified_value": { "value": 0.531, "unit": "g/t",
                           "raw_value": "0.531", "raw_unit": "ppm" }
    }
  ],
  "rejected": [],                       // records quarantined by the anti-fabrication guard
  "n_records": 5,
  "guard": { "status": "checked",       // "checked" | "ungated" (unverifiable table)
             "n_rejected": 0,
             "source_numbers": 29 }      // distinct numbers found in the source CSV
}
```

**`table_type`** is one of: `resource_estimate`, `assay_interval`,
`drillhole_collar`, `drill_history`, `qaqc_crm`, `tenure`, `budget`. Tables the
normalizer can't confidently type (or with too few numbers) are skipped, not
guessed. `guard.status: "ungated"` marks records from an `unverifiable` source
table — kept for reference but **excluded from all quantitative claims**.

---

## Stage B — `metrics/*.json`

Deterministic Python over the typed records. Every metric file carries an
`applicable` flag (with a `reason` when false, so Stage F can tell *"skipped by
design"* from *"missing"*), a `formula` string, and per-row provenance.

### `metrics/_stage.json` — project-stage classification

```jsonc
{ "stage": "resource",                  // resource | target | early | desktop
  "record_counts": { "resource_estimate": 189, "qaqc_crm": 30,
                     "assay_interval": 28, "tenure": 61, … } }
```

The stage is derived from *which* record types are present — it makes the rest of
the pipeline adapt instead of assuming a fixed report shape.

### `metrics/resource.json`

```jsonc
{ "applicable": true,
  "formula": "contained_koz = tonnage_t * grade_gpt / 31.1035 / 1000",
  "n_rows": 189,
  "n_recompute_mismatches": 13,         // rows where reported ≠ recomputed (>±2%)
  "rows": [ { "deposit": …, "category": …, "tonnage_t": …, "grade_gpt": …,
              "contained_reported_koz": 33.6, "contained_recomputed_koz": 33.65,
              "recompute_ok": true, "is_subtotal": false,
              "confidence": "high", "provenance": { … } } ] }
```

### `metrics/intercepts.json`

```jsonc
{ "applicable": true,
  "cutoff_gpt": 0.3,
  "formula": "gram_metres = interval_m * au_gpt; length-weighted grade",
  "n_intervals": 28, "n_parent_intervals": 22, "n_significant_at_cutoff": 17,
  "containment_violations": [ … ],      // sub-intervals that don't lie within their parent
  "top_intercepts": [ … ], "intervals": [ … ] }
```

### `metrics/qaqc.json`

```jsonc
{ "applicable": true,
  "formula": "bias = (mean_measured - certified) / certified * 100",
  "n_crms": 6,
  "overall": "fail",                    // pass | marginal | fail | absent
  "crms": [ { "crm_id": "OREAS 229b", "bias_pct": -3.35, "pass_rate_pct": 91.0, … } ] }
```

### `metrics/tenure.json`

```jsonc
{ "applicable": true, "n_records": 61, "total_area_ha": 46538.29,
  "n_with_area": 45, "expiry_dates": { "earliest": "2025-12-22", … } }
```

---

## `analysis_state.json` — stage state

Records which analysis stages have completed and a one-line summary of each, so
re-runs skip finished work. Not a deliverable.

```jsonc
{ "normalize": { "done": true, "n_tables": 185, "typed": 50, "skipped": 18,
                 "tables_with_quarantine": 0 },
  "compute":   { "done": true, "project_stage": "resource", "qaqc_overall": "fail", … },
  "flags":     { "done": true, "n_flags": 3, "n_high": 1 },
  "assess":    { "done": true, "n_claims": 28, "n_unverified": 11 } }
```

`.cache/mistral/` holds content-hashed normalize/assess responses — re-running
the analysis is free unless the evidence changed.
