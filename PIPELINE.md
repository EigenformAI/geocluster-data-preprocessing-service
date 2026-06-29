# Pipelines

This project has **two pipelines that run in sequence**:

1. **Extraction** (`pipeline/`) — turns one report PDF into one structured
   directory of text / tables / figures (see [OUTPUT_STRUCTURE.md](OUTPUT_STRUCTURE.md)).
   Documented first, below.
2. **Analysis** (`analysis/`) — turns that directory into a conservative-QP
   geological assessment: typed domain data → cited metrics → a memo. Documented
   at the end (*Part 2 — Analysis pipeline*).

Both share the same shape: a **linear sequence of stages**, each reading earlier
artifacts and writing its own, recorded in a stage-state file; **idempotent,
resumable, cached**; processed per report with failure isolation, so one bad
input never blocks the rest.

---

# Part 1 — Extraction pipeline (PDF → structured directory)

Two design lines run through it:

- **A free deterministic core** (page renders + the pdftotext text layer) that
  needs no API key, and an **LLM layer** (Mistral OCR + vision) that does.
- **Tables are never trusted blind.** OCR output is cross-checked against an
  independent text extraction; tables OCR gets wrong are rebuilt from that text
  layer or flagged. This is the heart of the design — see *Table fidelity* below.

## Stage flow

```
                    INPUT: one report PDF
                            │
        ┌───────────────────▼───────────────────────────────────────┐
        │  0. INGEST            ingest.py                             │  always runs
        │     pdfinfo → doc.json, copy source.pdf                     │
        └───────────────────┬───────────────────────────────────────┘
                            │
  ── deterministic core ────┼─────────────────────────────  (no network, no key)
        ┌───────────────────▼───────────────────────────────────────┐
        │  1. RENDER           render.py                             │
        │     pdftoppm  →  pages/page_NNNN.png   (200 DPI)           │
        ├────────────────────────────────────────────────────────────┤
        │  2. GROUND TRUTH     ground_truth.py                       │
        │     pdftotext -bbox  →  ground_truth.json  (word boxes)    │  ← the table auditor
        └───────────────────┬───────────────────────────────────────┘
                            │
  ── LLM layer ─────────────┼───────────────────────────────  (needs MISTRAL_API_KEY)
        ┌───────────────────▼───────────────────────────────────────┐
        │  3. OCR  *network*   ocr.py        Mistral OCR             │
        │     source.pdf  →  ocr/page_NNNN.md + extracted images     │
        │     (25-page chunks · 3× retry · resumable per chunk)      │
        ├────────────────────────────────────────────────────────────┤
        │  4. PARSE            parse_ocr.py                          │
        │     OCR markdown  →  text/ · tables/*.csv · images/ ·      │
        │                      sections.json · page_class.json       │
        ├────────────────────────────────────────────────────────────┤
        │  5. DET_TABLES       det_tables.py     ┌─ table fidelity ─┐│
        │     rebuild SUSPECT tables from the    │  see sub-flow    ││
        │     text layer + Sonnet for N-up pages └──────────────────┘│
        ├────────────────────────────────────────────────────────────┤
        │  6. VERIFY           verify_tables.py                      │
        │     cross-check every CSV vs ground truth →                │
        │     tables/*.meta.json + tables/*.crop.png                 │
        ├────────────────────────────────────────────────────────────┤
        │  7. DESCRIBE *network*  describe_figures.py  ministral-14b │
        │     each figure  →  images/<id>.description.md  (cached)   │
        ├────────────────────────────────────────────────────────────┤
        │  8. MANIFEST         manifest.py                          │
        │     →  manifest.json + summary.md (review queue)           │
        └───────────────────┬───────────────────────────────────────┘
                            ▼
              OUTPUT: output/<report_slug>/
```

Run order is `ingest → render → ground_truth → ocr → parse → det_tables →
verify → describe → manifest`. `--no-llm` stops after `ground_truth` (the free
core). `ingest` always runs because it provides the page count the others need.

---

## Stage reference

| # | Stage | Module | Network | Reads | Writes |
|---|---|---|---|---|---|
| 0 | ingest | `ingest.py` | – | the PDF | `doc.json`, `source.pdf` |
| 1 | render | `render.py` | – | the PDF | `pages/page_NNNN.png` |
| 2 | ground_truth | `ground_truth.py` | – | the PDF | `ground_truth.json` (pdftotext word boxes) |
| 3 | ocr | `ocr.py` | **Mistral OCR** | `source.pdf` | `ocr/page_NNNN.md`, `ocr/img/`, `ocr/page_NNNN.json` |
| 4 | parse | `parse_ocr.py` | – | `ocr/` | `text/`, `tables/*.csv`, `images/fig_*`, `sections.json`, `page_class.json`, `tables_index.json` |
| 5 | det_tables | `det_tables.py`, `hybrid_tables.py` | **Anthropic** (N-up pages only) | `tables_index.json`, `ground_truth.json`, `source.pdf` (pdfplumber words), `pages/` | rebuilt `tables/*.csv` (`method=digital_text` or `vision_hybrid`); rewritten `tables_index.json` |
| 6 | verify | `verify_tables.py` | – | `tables_index.json`, `ground_truth.json`, `pages/` | `tables/*.meta.json`, `tables/*.crop.png`, `tables_verified.json` |
| 7 | describe | `describe_figures.py` | **Mistral vision** | `images/fig_*` | `images/<id>.description.md`, `figures_index.json` |
| 8 | manifest | `manifest.py` | – | all of the above | `manifest.json`, `summary.md` |

`run.py` is the CLI / orchestrator that wires these together.

---

## Table fidelity (stages 2 → 4 → 5 → 6)

Mistral OCR sometimes alters numbers in dense tables, so the pipeline audits
every table against an **independent** extraction — the pdftotext word boxes from
stage 2 — and rebuilds or flags accordingly.

```
   OCR table CSV (stage 4)          ground_truth.json (stage 2)
   numbers in the CSV               numbers pdftotext found on that page
            │                                  │
            └──────────────┬───────────────────┘
                           ▼
              coverage = (CSV numbers also in the text layer) / (CSV numbers)
                           │
        ┌──────────────────┼──────────────────────────────┐
        │                  │                               │
   coverage ≥ 0.95    coverage < 0.95               coverage < 0.95
        │              AND page has text            AND ~no text layer
        ▼                  │                               ▼
   keep OCR table          │                          keep OCR table
   confidence=high         │                          confidence=unverifiable
   method=mistral_ocr      │                          method=mistral_ocr
                           ▼
                split page into side-by-side regions (region-split)
                           │
              ┌────────────┴──────────────┐
              │                           │
       single table (1 region)      N-up page (≥2 regions)
              │                           │
              ▼                           ▼
      column-anchor + row         Sonnet vision reads each cropped
      clustering on the           sub-table for STRUCTURE; cell values
      pdfplumber words            still taken from the text layer
      method=digital_text         (reconciled — flag, never trust)
              │                    method=vision_hybrid
              │                           │  (falls back to digital_text if
              │                           │   no ANTHROPIC_API_KEY / poor read)
              └────────────┬──────────────┘
                           ▼
              VERIFY (stage 6) re-checks every table,
              writes meta + a .crop.png of the source region
```

Result: each table ends up **`high`** (numbers backed by, or rebuilt from, the
digital text), **`low`/suspect** (text present but OCR disagreed and couldn't be
auto-rebuilt — review by hand), or **`unverifiable`** (image/scanned page, no
text layer). Nothing is silently trusted. The `<id>.crop.png` beside each CSV
lets a human confirm against the source render.

**The N-up hybrid (vision for structure, text layer for numbers).** Side-by-side
tables — two or more tables printed across the page width (a tenure list, an
intercept summary) — defeat geometric clustering: adjacent columns jam together
and a Claim ID is indistinguishable from a neighbouring company number. For these
pages `hybrid_tables.py` crops each sub-table from the page render and asks
**`claude-sonnet-4-6`** to read its *structure* (rows × columns), then fills every
value from the pdftotext layer, reconciling each number and flagging any the text
layer can't back. The model supplies layout only — never a trusted number. This is
the single place the otherwise Mistral-only runtime calls Anthropic
(`ANTHROPIC_API_KEY`); without that key, or if a crop reads below 80 % backed, it
falls back to the deterministic `digital_text` rebuild.

> Why rebuild only *suspect* pages rather than all of them: where OCR already
> matches the text layer it's correct (and often handles wrapped multi-line cells
> better than geometric reconstruction does), so those are left alone. The
> deterministic rebuild is applied exactly where OCR is demonstrably wrong.

---

## Design properties

**Stage-gated & resumable.** Every stage records completion in `stage_state.json`
and skips work whose output already exists:

- Deterministic stages skip entirely if their output is present.
- `ocr` resumes per 25-page chunk — a chunk is skipped if its `ocr/page_*.md`
  already exists, so a crash or timeout re-runs only the unfinished chunks.
- `describe` caches each result under `.cache/mistral/<hash>.json` keyed by image
  content + prompt + model, so re-runs never re-bill.

Re-running the whole command is therefore cheap and safe.

**Failure isolation.** A failure in one report is caught in `run.py` and the next
report still runs. Within `describe`, one failing figure is skipped (counted, not
fatal). OCR calls retry 3× with backoff and a 10-minute timeout.

**Free core vs paid layer.** Stages 0–2 need no key and produce the page renders
and the ground-truth text layer. Stages 3–8 use the Mistral API
(`MISTRAL_API_KEY`, loaded from `.env`). `--no-llm` runs only the free core.
`det_tables` additionally calls **Anthropic Sonnet** (`ANTHROPIC_API_KEY`) for
**N-up table structure only** — the lone place the otherwise Mistral-only runtime
uses Anthropic; absent that key it falls back to the deterministic rebuild, so the
pipeline still runs (just without the vision hybrid on side-by-side tables).

---

## Running and composing stages

```bash
uv run python -m pipeline.run "Gold_X2_Mining.pdf"           # full pipeline
uv run python -m pipeline.run *.pdf                          # all reports
uv run python -m pipeline.run "X.pdf" --no-llm               # free core only
uv run python -m pipeline.run "X.pdf" --stages det_tables,verify,manifest
uv run python -m pipeline.run "X.pdf" --force                # ignore stage_state, redo
uv run python -m pipeline.run "X.pdf" --all-vision           # rebuild EVERY table page via Sonnet
```

`--all-vision` forces the N-up hybrid on *every* table-bearing page (with a text
layer), not just side-by-side ones — use it for a report whose default extraction
mangles single tables too. Numbers still come from the text layer; needs
`ANTHROPIC_API_KEY`.

`--stages` runs a subset (comma-separated); useful for iterating on the
network-free stages (`parse,det_tables,verify,manifest`) without re-calling the
API. `ingest` always runs regardless of `--stages`.

> **Re-running table stages? Include `manifest`.** The analysis pipeline reads its
> table list from `manifest.json`, not `tables_index.json`. Re-segmenting tables
> (e.g. `parse,det_tables,verify`) without regenerating the manifest leaves it
> pointing at CSVs that `det_tables` deleted, and analysis dies with
> `FileNotFoundError`.

---

## Module map

```
pipeline/
├── run.py              # CLI / orchestrator (stage order, --stages, --no-llm, resilience)
├── ingest.py           # 0  identity + metadata
├── render.py           # 1  pdftoppm page renders
├── ground_truth.py     # 2  pdftotext -bbox word boxes (the table auditor)
├── ocr.py              # 3  Mistral OCR (chunked, retry, resumable)
├── parse_ocr.py        # 4  OCR markdown → text / tables / images / sections
├── det_tables.py       # 5  rebuild suspect tables from the text layer
│                       #    (pdfplumber words + side-by-side region split)
├── hybrid_tables.py    # 5  N-up tables: Sonnet vision for STRUCTURE, text layer for numbers
├── verify_tables.py    # 6  numeric cross-check + crops + meta
├── describe_figures.py # 7  Mistral vision figure descriptions
├── manifest.py         # 8  manifest.json + summary.md
├── mistral_client.py   #    Mistral client factory (loads .env, override=True)
└── util.py             #    paths, hashing, JSON, stage-state helpers
```

---

# Part 2 — Analysis pipeline (structured directory → QP assessment)

The analysis pipeline reads a completed extraction (`output/<slug>/`) and produces
a **conservative-QP geological assessment** under `output/<slug>/analysis/`. It
never mutates the extraction artifacts.

One design line runs through it, echoing Part 1:

- **The LLM types and reasons; Python computes and verifies.** The model maps
  columns and writes prose, but it has **zero numeric authority** — every value
  comes straight from a verified table, every metric is computed in Python, and
  every number the memo states is re-checked against the evidence. This is the
  heart of the analysis design — see *Typed data & anti-fabrication* below.

## Stage flow

```
        INPUT: output/<slug>/  (a completed extraction: manifest + tables + text)
                            │
  ── LLM ───────────────────┼───────────────────────────────  (needs MISTRAL_API_KEY)
        ┌───────────────────▼───────────────────────────────────────┐
        │  A. NORMALIZE  *network*  normalize.py                     │
        │     each data table → typed records (drillhole / assay /   │
        │     resource / qaqc / tenure …). LLM maps columns only;    │
        │     Python extracts values, ffills ids, number-guards.     │  → analysis/records/*.json
        └───────────────────┬───────────────────────────────────────┘
                            │
  ── deterministic ─────────┼───────────────────────────────  (no network)
        ┌───────────────────▼───────────────────────────────────────┐
        │  B. COMPUTE          compute.py + report_meta.py           │
        │     gram-metres & significant intercepts; resource         │
        │     contained-metal recompute; QA/QC bias/pass verdict;    │
        │     tenure. Project-stage adaptivity.                      │  → analysis/metrics/*.json
        ├────────────────────────────────────────────────────────────┤
        │  E. DUE DILIGENCE    flags.py                              │
        │     red-flag rules (missing QA/QC, suspect-table reliance, │
        │     recompute mismatch, thin drilling, …)                  │  → analysis/flags.json
        └───────────────────┬───────────────────────────────────────┘
                            │
  ── LLM ───────────────────┼───────────────────────────────  (needs MISTRAL_API_KEY)
        ┌───────────────────▼───────────────────────────────────────┐
        │  F. ASSESS  *network*  assess.py                          │
        │     conservative-QP memo grounded only in metrics+flags;   │
        │     every claim's citations + numbers machine-validated.   │  → analysis/memo.md + findings.json
        └───────────────────┬───────────────────────────────────────┘
                            ▼
              OUTPUT: output/<slug>/analysis/
```

## Stage reference

| Stage | Module | Network | Reads | Writes |
|---|---|---|---|---|
| A normalize | `normalize.py` | **Mistral chat.parse** | `manifest.json`, `tables/*.csv` | `records/<table_id>.json`, `records/_index.json` |
| B compute | `compute.py`, `report_meta.py` | – | `records/` | `metrics/{intercepts,resource,qaqc,tenure}.json`, `metrics/_stage.json` |
| E flags | `flags.py` | – | `metrics/`, section text | `flags.json` |
| F assess | `assess.py` | **Mistral chat.parse** | `metrics/`, `flags.json`, high-confidence tables | `memo.md`, `findings.json` |

## Typed data & anti-fabrication (A → F)

This is where the LLM is allowed in, so it gets the tightest leash:

```
  Stage A — the LLM only classifies the table and maps columns to fields.
            Python reads the CELL VALUES straight from the CSV, forward-fills
            identifier columns (hole id / property), and flags sub-interval /
            subtotal / zone-header rows.
                            │
            number guard: every numeric token in a record must already exist in
            the source CSV (reuses pipeline/verify_tables._numbers). A record
            with an invented number is quarantined.   ── unverifiable tables run
                            │                            ungated and are barred
                            ▼                            from quantitative claims.
  Stage B — pure-Python metrics, each carrying input provenance + a formula:
            contained_koz = tonnes × grade / 31.1035 / 1000  (recomputed vs reported),
            QA/QC bias = (measured − certified)/certified, gram-metres, etc.
                            │
  Stage F — the memo model sees ONLY metrics + flags + high-confidence tables.
            Each claim it makes is re-validated: citations must resolve in the
            manifest, and every number must come from the evidence supplied —
            unsupported claims are marked [UNVERIFIED].
```

Net: typed, computed, and cited — with no point at which the LLM can introduce a
number that isn't in the source.

## Project-stage adaptivity

`report_meta.py` classifies the project from **which record types exist** (not the
report name): `resource` (has a resource estimate) → run resource math and lead
with it; `target` (assays but no resource, e.g. an early-stage property) → skip
resource math, don't flag it as missing; `early`/`desktop` otherwise. Each metrics
file records `applicable` + `reason`, so Stage F distinguishes "skipped by design"
from a real gap (e.g. *no QA/QC tabulated* → a high red flag).

## Design properties

- **Stage-gated & resumable** via a separate `analysis/analysis_state.json`.
  `normalize` is per-table resumable (skips a table whose `records/<id>.json`
  exists) and caches each LLM mapping under `analysis/.cache/mistral/`; `assess`
  caches its memo call. Re-runs never re-bill.
- **Failure isolation.** One bad table is recorded and skipped, not fatal; one bad
  report doesn't abort the others. LLM calls retry with backoff (longer on 429).
- **Deterministic vs LLM split.** `--no-llm` runs only `compute` + `flags`.

## Running and composing stages

```bash
uv run python -m analysis.run output/new_found_gold_corp_tech_report   # full
uv run python -m analysis.run output/*                                 # all reports
uv run python -m analysis.run output/<slug> --stages compute,flags     # re-run math only, no API
uv run python -m analysis.run output/<slug> --no-llm                   # deterministic stages only
uv run python -m analysis.run output/<slug> --force
```

Input is the **output directory**, not the PDF.

## Module map

```
analysis/
├── run.py          # CLI / orchestrator (normalize → compute → flags → assess)
├── source.py       # read adapter over output/<slug>/ + cell cleaning
├── schemas.py      # canonical record fields + the LLM column-mapping model
├── units.py        # deterministic unit normalization (g/t, tonnes, koz, m, ha)
├── numguard.py     # anti-fabrication (reuses pipeline verify_tables._numbers)
├── llm.py          # cached Mistral chat.parse wrapper (retry/backoff)
├── normalize.py    # A  tables → typed records
├── compute.py      # B  deterministic cited metrics
├── report_meta.py  #    record loading + project-stage classification
├── flags.py        # E  due-diligence red-flag rules
├── assess.py       # F  conservative-QP memo + citation validation + findings.json
├── apaths.py       #    analysis output locations
└── astate.py       #    analysis stage-state
```

