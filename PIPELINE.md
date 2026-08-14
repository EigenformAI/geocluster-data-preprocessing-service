# Pipelines

This project has **two pipelines that run in sequence**:

1. **Extraction** (`pipeline/`) — turns one report PDF into one structured
   directory of text / tables / figures (see [OUTPUT_STRUCTURE.md](OUTPUT_STRUCTURE.md)).
   Documented first, below.
2. **Analysis** (`analysis/`) — turns that directory into a **grounded evidence
   digest + conservative-QP assessment**: a hierarchical extraction (HIE) of
   grounded assertions, typed records, and figures/maps → cited metrics + red
   flags → domain-knowledge routing (DKI) → a validated memo (PEQA). Documented
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
| 5 | det_tables | `det_tables.py`, `hybrid_tables.py` | **Anthropic** (N-up pages; `--all-vision`; `--vision-image-tables`) | `tables_index.json`, `ground_truth.json`, `source.pdf` (pdfplumber words), `pages/` | rebuilt `tables/*.csv` (`method=digital_text`, `vision_hybrid`, or `vision_unverified`); rewritten `tables_index.json` |
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
**table structure** — N-up pages by default, plus every table page under
`--all-vision` and image-only pages under `--vision-image-tables`. This is the lone
place the otherwise Mistral-only runtime uses Anthropic; absent that key it falls
back to the deterministic rebuild (skipping only the image-only pages, which have
no text layer to rebuild from), so the pipeline still runs.

---

## Running and composing stages

```bash
uv run python -m pipeline.run "Gold_X2_Mining.pdf"           # full pipeline
uv run python -m pipeline.run *.pdf                          # all reports
uv run python -m pipeline.run "X.pdf" --no-llm               # free core only
uv run python -m pipeline.run "X.pdf" --stages det_tables,verify,manifest
uv run python -m pipeline.run "X.pdf" --force                # ignore stage_state, redo
uv run python -m pipeline.run "X.pdf" --all-vision           # rebuild EVERY table page via Sonnet
uv run python -m pipeline.run "X.pdf" --vision-image-tables  # transcribe IMAGE-ONLY table pages (rotated upright)
```

`--all-vision` forces the N-up hybrid on *every* table-bearing page (with a text
layer), not just side-by-side ones — use it for a report whose default extraction
mangles single tables too. Numbers still come from the text layer; needs
`ANTHROPIC_API_KEY`.

`--vision-image-tables` handles the opposite case: table pages with **no digital
text layer at all** (scanned or sideways inserts). The page is first auto-rotated
upright (Sonnet decides the angle), then transcribed by **vision alone** — there is
no text layer to reconcile against, so these tables are written
`method=vision_unverified`, `confidence=unverifiable` (review by eye). Also needs
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

# Part 2 — Analysis pipeline (structured directory → grounded digest + QP assessment)

The analysis pipeline reads a completed extraction (`output/<slug>/`) and produces
a **grounded evidence digest + conservative-QP assessment** under
`output/<slug>/analysis/`. It never mutates the extraction artifacts.

Unlike extraction (Mistral + Sonnet), analysis calls **`opencode` + `gpt-5.5`** over
your OAuth session — **no metered key, $0** — and its default backend is
`heuristic`, so it **runs fully deterministically with no model calls at all**
unless you turn the LLM on. See *The LLM seam* below.

One design line runs through it, echoing Part 1 but wider:

- **The model types, routes, and reasons; Python extracts, grounds, computes, and
  verifies.** The model maps columns, classifies figures, reads map crops, and
  writes the QP prose — but it has **zero numeric and zero prose authority**. Every
  record value is lifted from a verified CSV (`numguard`); every prose assertion is
  a verbatim substring of the source (`textguard`); every image read is unverified
  until corroborated (`imageguard`); every metric is computed in Python; every memo
  claim is re-checked against the evidence. This is the heart of the design — see
  *Grounding & anti-fabrication* below.

## Stage flow

```
   INPUT  output/<slug>/   (completed extraction: manifest + tables + text + figures)
     │
     ▼
  ══ 1. HIE  — hie.py  (map-reduce into a grounded IR) ═══════════════════════
     Model seams are gated: deterministic by default, LLM/VLM when enabled.

       MAP (per element):
         tables   → typed records          tables.py + column-mapping seam + numguard
         prose    → grounded assertions    spans.py + assertions.py + textguard
         figures  → classification         figures.py
         maps     → components + crop reads maps.py (PEACE worker) + imageguard
       REDUCE — reduce.py:
         entity hierarchy · location candidates · project stage

     writes → report_metadata.json, assertions.json, evidence_index.json,
              input_index.json, records/*, hie/*, hierarchy/*
     │
     ▼   ───────────────────────────── deterministic from here on ─────────────
  ══ 2. METRICS — compute.py ════════════════════════════════════════════════
     intercepts · resource contained-metal · QA/QC bias/pass · tenure,
     each recomputed vs reported with a formula + provenance     → metrics/*.json
     │
     ▼
  ══ 3. FLAGS — flags.py ═════════════════════════════════════════════════════
     deterministic red-flag rules (missing QA/QC, suspect-table reliance,
     recompute mismatch, intercept containment, …)               → flags.json
     │
     ▼
  ══ 4. DKI — dki.py ═════════════════════════════════════════════════════════
     route the question → NI 43-101 taxonomy classes (taxonomy.py) and
     surface the matching grounded assertions from the HIE graph → dki/report.json
     │
     ▼
  ══ 5. PEQA — peqa.py ═══════════════════════════════════════════════════════
     assemble the answer-pack (grounded digest + metrics + crops + DKI);
     optionally an LLM writes the QP answer; every claim re-validated
     (claims.py: citations resolve + numguard + textguard); render the
     digest-first memo                        → peqa/*, memo.md, findings.json
     │
     ▼
   OUTPUT  output/<slug>/analysis/
```

Run order is `HIE → metrics → flags → DKI → PEQA → findings` (`run.py`). There is
no `--stages` / `--no-llm` flag; deterministic-vs-LLM is chosen by the
`ANALYSIS_LLM_BACKEND` env var (see *The LLM seam*).

## Stage reference

| # | Stage | Module(s) | Model | Reads | Writes |
|---|---|---|---|---|---|
| 1 | HIE | `hie.py`, `tables.py`, `spans.py`, `assertions.py`, `figures.py`, `maps.py`, `reduce.py` | LLM + VLM, **gated** | `manifest.json`, `tables/*.csv`, `text/`, `images/` | `report_metadata.json`, `assertions.json`, `evidence_index.json`, `input_index.json`, `records/*`, `hie/*`, `hierarchy/*` |
| 2 | metrics | `compute.py` | – | `records/`, `report_metadata.json` | `metrics/{_stage,intercepts,resource,qaqc,tenure}.json` |
| 3 | flags | `flags.py` | – | `metrics/`, metadata | `flags.json` |
| 4 | DKI | `dki.py`, `taxonomy.py` | – (deterministic) | `report_metadata.json` (harvested assertions) | `dki/report.json` |
| 5 | PEQA | `peqa.py`, `claims.py`, `report_agent.py` | LLM (QP answer only), **gated** | metrics, flags, DKI, grounded digest | `peqa/answers/report.json`, `memo.md`, `findings.json` |

## The HIE map-reduce (extraction → grounded IR)

HIE (Hierarchical Information Extraction) is the heavy lifter: a **map-reduce** that
turns the extraction directory into a single grounded intermediate representation,
`report_metadata.json`.

- **Map** runs per element and each model touch-point is a *seam* with a
  deterministic fallback (see below):
  - **Tables** → typed records. A column-mapping seam types the table
    (`resource_estimate`, `assay_interval`, `drillhole_collar`, `drill_history`,
    `qaqc_crm`, `tenure`, `budget`, or `other`) and maps its columns; `tables.py`
    then lifts every value from the CSV and `numguard` quarantines any record whose
    numbers aren't in the source. The `map_table` seam is heuristic-first and only
    escalates to the LLM when the header signature is ambiguous.
  - **Prose** → grounded assertions. `spans.py` extracts candidate spans (lexical,
    optionally LLM-assisted) and `assertions.py` builds a span-grounded assertion
    graph; `textguard` rejects any assertion that isn't a verbatim substring of the
    source. The harvest is **question-agnostic** — it collects assertions for *every*
    NI 43-101 taxonomy class present, so downstream recall isn't bottlenecked on one
    question.
  - **Figures** → classification (`figures.py`, from the extraction-time description
    prose) with map / georeference candidates.
  - **Maps** → components. `maps.py` runs a warm PEACE subprocess
    (`peace_map_worker.py`, inside the peace-tool-pool detector venv) to detect map
    regions/legends, and the VLM reads legend/geo crops — each read gated by
    `imageguard`.
- **Reduce** (`reduce.py`) folds the per-element results into a report-wide **entity
  hierarchy**, **location candidates**, and the **project stage**.

Outputs: the `report_metadata.json` blob (with a `build_trace.counters` telemetry
block), the flat `assertions.json`, the `evidence_index.json` (evidence-id →
source span), and the `hie/` + `hierarchy/` + `records/` trees.

## Grounding & anti-fabrication (three guards + evidence ids)

The model is allowed to type, route, and phrase — never to introduce a fact:

```
  numguard   — every numeric token in a record must already exist in the source
               CSV; a record with an invented number is quarantined into `rejected`.
               Records from an `unverifiable` source table run "ungated" and are
               barred from all quantitative claims.
  textguard  — every prose assertion must be a verbatim substring of the source
               section text; non-substring spans are dropped (harvest counter
               `harvest.n_rejected_by_textguard`).
  imageguard — image/VLM reads are UNVERIFIED by default; a read is promoted only
               when corroborated by independent evidence. Verdicts:
               corroborated / plausible / unverifiable / rejected.
  evidence   — every mapped field and assertion carries an `evidence_id` that
               resolves through `evidence_index.json` back to its source span, and
               every memo citation must resolve in the manifest. Claims that fail
               validation are surfaced as [UNVERIFIED] and listed in
               `unverified_claims`.
```

Net: typed, grounded, computed, and cited — with no point at which the model can
introduce a number, sentence, or image fact that isn't in the source.

## The LLM seam (pluggable, deterministic by default)

`llm.py` is a single pluggable boundary. Four selectors expose the model to the
pipeline — the **table mapper**, **figure classifier**, **answerer** (the QP memo),
and **image reader** (VLM) — and each is chosen by env var:

- `ANALYSIS_LLM_BACKEND` ∈ `heuristic` (default — pure Python, no network),
  `opencode` (real LLM/VLM), `fake` (tests).
- `opencode` drives `ANALYSIS_LLM_MODEL` (default `openai/gpt-5.5`) against an
  externally managed `opencode serve` at `OPENCODE_SERVER_URL` — no metered key; it
  runs over your OAuth session.
- Non-`opencode` backends wrap the heuristic as *primary* with the LLM as
  *fallback*; and if the opencode server is unreachable, the selectors **degrade
  gracefully** to the deterministic path rather than crashing.
- Maps/VLM have their own gates: `ANALYSIS_MAP_BACKEND` (`none` | `peace`) selects
  the map detector, and `ANALYSIS_MAP_VLM` (`on` | `off`) toggles crop reads.

Responses cache under `output/<slug>/analysis/.cache/llm/opencode/<key>.json`, keyed
by content, so re-runs re-call nothing. **Telemetry is cache-miss-only** —
`llm.tokens.*` / `vlm.tokens.*` accrue only on live calls, so a warm run reports ~0
tokens; run cold (`--force`, cleared cache) to measure true spend. Optional
`ANALYSIS_LLM_MAX_CALLS_PER_RUN` (default 1000) and
`ANALYSIS_LLM_TOKEN_BUDGET_PER_RUN` caps throttle a run when hit.

## Project-stage adaptivity

`reduce.py` classifies the project from **which record types exist** (not the report
name): `resource` (has a resource estimate) → run resource math and lead with it;
`target` (assays but no resource, e.g. an early-stage property) → skip resource
math, don't flag it as missing; `early` / `desktop` otherwise. Each metrics file
records `applicable` + `reason`, so flags and PEQA distinguish "skipped by design"
from a real gap (e.g. *no QA/QC tabulated* → a high red flag).

## Design properties

- **Stage-gated & resumable** via `analysis/analysis_state.json` (per-stage
  `counters` + `trace_id`) and cached HIE artifacts. `--force` rebuilds the HIE
  layer; the deterministic tail (metrics/flags/DKI/PEQA) re-runs cheaply against
  cached HIE metadata. LLM/VLM responses are cached, so re-runs re-call nothing.
- **Deterministic by default.** With `ANALYSIS_LLM_BACKEND=heuristic` (the default)
  the whole pipeline runs with zero model calls — the modern equivalent of the old
  `--no-llm`. The QP Assessment section of the memo simply doesn't render when no
  answerer ran.
- **Failure isolation & graceful degradation.** A bad table is quarantined, not
  fatal; an unreachable model server falls back to the deterministic path; the batch
  runner isolates per-report failures.
- **Digest-first output (goal C′).** `memo.md` leads with the grounded recall digest
  organized by NI 43-101 item; the LLM QP opinion is demoted to a subordinate
  `## QP Assessment` section that renders only if the answerer ran.

## Running and composing stages

```bash
# deterministic-only (no key, $0)
uv run python -m analysis.run output/gold_x2_mining

# with the LLM/VLM on (an opencode serve must be running)
export ANALYSIS_LLM_BACKEND=opencode
export OPENCODE_SERVER_URL=http://127.0.0.1:8779
export ANALYSIS_MAP_BACKEND=peace                 # optional: analyse map figures
uv run python -m analysis.run output/gold_x2_mining --force

# drive a different question
uv run python -m analysis.run output/<slug> --question "Assess QA/QC adequacy."

# all reports (batch runner; discovers output/*/ dirs that have a manifest.json)
ANALYSIS_REPORT_WORKERS=4 uv run python scripts/run_all_reports.py
```

Input is the **output directory**, not the PDF, and **one directory per invocation**
— `analysis.run` no longer globs; batch with `scripts/run_all_reports.py`.

## Module map

```
analysis/
├── run.py            # CLI / orchestrator (HIE → metrics → flags → DKI → PEQA → findings)
├── source.py         # read adapter over output/<slug>/  (+ source_paths.py for safe refs)
├── paths.py          # analysis output layout under output/<slug>/analysis/
├── schemas.py        # canonical records + lightweight structured-output models
│   ── HIE: map ──
├── hie.py            # map-reduce builder for report_metadata.json
├── spans.py          # prose span-extraction seam (lexical + optional LLM)
├── assertions.py     # span-grounded assertion graph for prose facts
├── tables.py         # deterministic table normalization once columns are mapped
├── figures.py        # figure classification from extraction-time description prose
├── maps.py           # map-figure components via a warm PEACE subprocess worker
├── peace_map_worker.py  # runs inside the peace-tool-pool detector venv
│   ── HIE: reduce ──
├── reduce.py         # entity hierarchy, location candidates, project stage
│   ── deterministic tail ──
├── compute.py        # deterministic cited metrics (intercepts/resource/qaqc/tenure)
├── flags.py          # deterministic due-diligence red flags
├── taxonomy.py       # NI 43-101 item + keyword taxonomy for prose recall
├── dki.py            # domain-knowledge injection seams (question → assertions)
├── peqa.py           # answer-pack assembly, QP-answer validation, memo rendering
├── claims.py         # claim citation + numeric-token verification
├── report_agent.py   # Report-Agent HIE artifact helpers
│   ── grounding & seams ──
├── llm.py            # pluggable LLM/VLM boundary (heuristic | fake | opencode)
├── numguard.py       # numeric anti-fabrication (values must exist in source CSV)
├── textguard.py      # verbatim-text grounding for prose evidence
├── units.py          # deterministic unit normalization (g/t, t, koz, m, ha)
├── validation.py     # JSON Schema + semantic validation for IR artifacts (schema/)
│   ── tooling ──
├── mcp.py            # stdio MCP JSON-RPC wrapper for geo-report tools
├── mcp_tools.py      # in-process tool surface for the MCP wrapper + tests
└── ablation.py       # ablation harness for Report-Agent modules
```

