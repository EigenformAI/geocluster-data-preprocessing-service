# extract-reports

Decompose NI 43-101 mineral-exploration technical report PDFs into structured,
per-report directories — **text** (by section), **tables** (as verified CSVs),
and **figures** (with LLM descriptions) — for a downstream analysis pipeline.

Each PDF becomes its own directory under `output/<report_slug>/`.

## Setup

```bash
uv sync
```

The OCR and figure-description stages call the Mistral API, so provide a key.
Put it in a `.env` file (loaded automatically with `override=True`):

```bash
cp .env.example .env        # then edit .env to set MISTRAL_API_KEY=...
```

(or `export MISTRAL_API_KEY=...`). Required for stages `ocr`, `parse`, `verify`,
`describe`, `manifest`. The deterministic stages (`render`, `ground_truth`) need no key.

## Run

```bash
# Full pipeline on one report
uv run python -m pipeline.run "Gold_X2_Mining.pdf"

# Free deterministic core only (page renders + ground-truth text layer)
uv run python -m pipeline.run "Gold_X2_Mining.pdf" --no-llm

# All four reports
uv run python -m pipeline.run *.pdf

# Re-run a subset of stages
uv run python -m pipeline.run "Gold_X2_Mining.pdf" --stages ocr,parse,verify

# Re-run reports with sonnet vision only
uv run python -m pipeline.run "Gold_X2_Mining.pdf" --all-vision
```

Stages are idempotent and resumable: re-running skips completed deterministic
work and reuses cached Mistral responses (`output/<slug>/.cache/`), so it never
re-bills.

## Pipeline

| Stage | Network | Output |
|---|---|---|
| `ingest` | no | `doc.json`, copy of source PDF |
| `render` | no | `pages/page_NNNN.png` (200 DPI) |
| `ground_truth` | no | `ground_truth.json` (pdftotext word boxes — the table auditor) |
| `ocr` | Mistral OCR | `ocr/page_NNNN.md` + extracted images |
| `parse` | no | `text/`, `tables/*.csv`, `images/`, `sections.json`, `page_class.json` |
| `det_tables` | no | rebuilds suspect tables from the digital text layer (authentic values) |
| `verify` | no | per-table `.meta.json` + `.crop.png`; numeric cross-check vs the text layer |
| `describe` | Mistral vision (`ministral-14b-2512`) | `images/<id>.description.md` |
| `manifest` | no | `manifest.json`, `summary.md` (low-confidence review queue up top) |

## Table fidelity ("checkable")

Mistral OCR alters numbers in dense tables, so the pipeline does **not** trust it
blindly. Every OCR table's numbers are cross-checked against the digital text
layer (`pdftotext`, a different extraction path). Outcomes:

- **digital_text / high** — the page has a real text layer and OCR disagreed, so
  the table was **rebuilt deterministically from the pdftotext word boxes**. The
  values are the authentic digital text by construction.
- **mistral_ocr / high** — OCR's numbers are fully backed by the text layer.
- **mistral_ocr / unverifiable** — image/scanned page with no digital text to
  check against; OCR is the only option (review by hand).

Every table ships a `.crop.png` of the same region from the page render so a
human can verify structure against the source. `summary.md` lists anything not
`high` up top.
