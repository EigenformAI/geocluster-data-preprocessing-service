# How to use

Two pipelines run back-to-back (see [PIPELINE.md](PIPELINE.md) for the design,
[OUTPUT_STRUCTURE.md](OUTPUT_STRUCTURE.md) for what they emit):

1. **Extraction** — a PDF report → a structured directory (`output/<slug>/`).
2. **Analysis** — that directory → a grounded evidence digest + conservative-QP
   assessment (`output/<slug>/analysis/`).

You run extraction first, then point analysis at its output.

> **The two pipelines configure model backends independently.** Extraction
> defaults to **Mistral** for OCR and figure descriptions, with optional
> **Anthropic/Sonnet** table reading. Tagged PDFs can instead use the local
> digital text adapter and an **OpenCode** model for figure descriptions.
> Analysis can use OpenCode models over your OAuth session and otherwise runs
> fully deterministically. See the prerequisites table for each backend.

---

## 1. Prerequisites

| Need | Check | Used by | Notes |
|---|---|---|---|
| **uv** (Python manager) | `uv --version` | both | Creates/manages the venv from `pyproject.toml`; no manual `pip`/`venv`. |
| **poppler CLI** | `which pdftoppm pdftotext pdfinfo` | extraction | Used by the deterministic extraction stages (render + ground-truth). |
| **Nix + direnv** | `nix --version`, `direnv version` | analysis (maps) | Loads the repo dev shell and syncs the map-analysis MCP dependency. Run `direnv allow` once after checkout. |
| **peace-tool-pool MCP** | `bash scripts/sync-peace-tool-pool.sh` | analysis (maps) | Required only for map-figure HIE / geospatial DKI (`ANALYSIS_MAP_BACKEND=peace`). Defaults to latest upstream `main` at `.cache/peace-tool-pool`; also syncs every MCP startup. |
| **`MISTRAL_API_KEY`** | see §1.1 | **extraction** | Required when OCR or figure descriptions use the default Mistral backend. The digital/OpenCode route does not use it. |
| **`ANTHROPIC_API_KEY`** | `echo $ANTHROPIC_API_KEY` | **extraction** (vision) | Required only for the Sonnet vision-hybrid table reader — N-up pages, `--all-vision`, `--vision-image-tables`. |
| **`opencode serve`** + **`OPENCODE_SERVER_URL`** | `curl $OPENCODE_SERVER_URL/session` | **extraction descriptions, analysis LLM/VLM** | Required when `PIPELINE_VISION_BACKEND=opencode` or `ANALYSIS_LLM_BACKEND=opencode`. |
| Tagged-PDF digital adapter | `PIPELINE_OCR_BACKEND=digital` | **extraction** | Uses the embedded text layer instead of Mistral OCR. Digital tables remain review-required because Poppler and pdfplumber read the same source stream. |

Dependencies install automatically the first time you `uv run` anything (or run
`uv sync` once up front). See [docs/environment.md](docs/environment.md) for the
Nix/direnv setup and the latest-main `peace-tool-pool` policy.

### 1.1 Configure extraction backends

Extraction reads `MISTRAL_API_KEY` (and optionally `ANTHROPIC_API_KEY`) from a
**`.env`** file at the repo root (gitignored, loaded automatically with
override). Copy the template and fill it in:

```bash
cp .env.example .env
# then edit .env:
# MISTRAL_API_KEY=your-key-here
# ANTHROPIC_API_KEY=your-key-here   # only if you use the vision-hybrid table reader
```

You can also `export` them in your shell instead — either works.

For a tagged PDF, use its embedded text layer for OCR and OpenCode for figure
descriptions:

```bash
export PIPELINE_OCR_BACKEND=digital
export PIPELINE_VISION_BACKEND=opencode
export PIPELINE_VISION_MODEL=openai/gpt-5.6-luna
export PIPELINE_VISION_WORKERS=4
export OPENCODE_SERVER_URL=http://127.0.0.1:8779
```

The digital adapter remains conservative: every extracted table is queued for
human review because pdfplumber and Poppler read the same embedded text stream.

### 1.2 Point analysis at your opencode server

The **analysis** pipeline never touches Mistral. Its default LLM backend is
`heuristic` — pure Python, no network, no key. To enable the real LLM/VLM
(column mapping, figure classification, legend/map crop reads, the QP answer),
start an `opencode serve` instance driving `gpt-5.5` over your OAuth session and
tell analysis where it lives:

```bash
export ANALYSIS_LLM_BACKEND=opencode
export OPENCODE_SERVER_URL=http://127.0.0.1:8779   # wherever your opencode serve listens
```

There is **no default URL** — if `ANALYSIS_LLM_BACKEND=opencode` and
`OPENCODE_SERVER_URL` is unset, the backend refuses to start and analysis falls
back to the deterministic path.

---

## 2. Quick start

The four source PDFs live at the repo root. To process one report end-to-end:

```bash
# Default extraction: PDF -> output/gold_x2_mining/ (needs MISTRAL_API_KEY)
uv run python -m pipeline.run Gold_X2_Mining.pdf

# Tagged-PDF alternative: local text OCR + OpenCode figure descriptions
PIPELINE_OCR_BACKEND=digital PIPELINE_VISION_BACKEND=opencode \
  uv run python -m pipeline.run Gold_X2_Mining.pdf

# Analysis: output/gold_x2_mining/ -> output/gold_x2_mining/analysis/
# deterministic-only (no key, $0):
uv run python -m analysis.run output/gold_x2_mining
# ...or with the LLM/VLM on (opencode serve must be running):
ANALYSIS_LLM_BACKEND=opencode OPENCODE_SERVER_URL=http://127.0.0.1:8779 \
  uv run python -m analysis.run output/gold_x2_mining
```

Then open the two human entry points:

```bash
output/gold_x2_mining/summary.md          # extraction report card + table review queue
output/gold_x2_mining/analysis/memo.md    # grounded evidence digest + QP assessment
```

> The slug is the lowercased, underscore-separated file stem
> (`Gold_X2_Mining.pdf` → `gold_x2_mining`). Filenames with spaces must be quoted:
> `uv run python -m pipeline.run "New Found Gold Corp. tech report.pdf"`.

---

## 3. Extraction pipeline

```bash
uv run python -m pipeline.run <pdf> [<pdf> ...] [options]
```

| Option | Effect |
|---|---|
| `--output DIR` | Output root (default: `output`). |
| `--stages LIST` | Run only a comma-separated subset (see stages below). |
| `--no-llm` | Run only the **free deterministic core** (`render` + `ground_truth`); skips all network calls. |
| `--force` | Re-run stages even if already completed (ignores cache/state). |
| `--all-vision` | Rebuild **every** table-bearing page (with a text layer) with the Sonnet vision hybrid, not just N-up pages — for a report whose default extraction is messy on single tables too. Numbers still come from the text layer. Needs `ANTHROPIC_API_KEY`; ~$0.05–0.15 per table page. |
| `--vision-image-tables` | Transcribe **image-only** table pages — pages a table sits on that have **no digital text layer** (scanned / sideways inserts). The page is auto-rotated upright first, then read by vision alone. Results are flagged **`unverifiable`** (`method: vision_unverified`) because there is no text layer to cross-check. Needs `ANTHROPIC_API_KEY`. |

**Stages** (in order): `ingest` (always runs), `render`, `ground_truth`, `ocr`,
`parse`, `det_tables`, `verify`, `describe`, `manifest`.

- `render`, `ground_truth` — **free / deterministic**.
- `ocr` — calls **Mistral** by default or reads the embedded text layer when
  `PIPELINE_OCR_BACKEND=digital`.
- `describe` — calls **Mistral** by default or the configured OpenCode model
  when `PIPELINE_VISION_BACKEND=opencode`.
- `parse`, `det_tables`, `verify`, `manifest` — **deterministic**, *except*
  `det_tables` invokes the **Sonnet vision hybrid** (`claude-sonnet-4-6`, needs
  `ANTHROPIC_API_KEY`) on N-up pages, and — when opted in — on `--all-vision`
  pages and `--vision-image-tables` image-only pages. **Table orientation** is
  handled here: the model is first asked how many degrees to rotate the page so
  its text is upright, then the rotated page is transcribed.

Examples:

```bash
# Free dry-run — renders pages + extracts the ground-truth text layer, no key needed
uv run python -m pipeline.run Gold_X2_Mining.pdf --no-llm

# All four reports in one go (each gets its own output dir; one failure won't abort the rest)
uv run python -m pipeline.run \
  Gold_X2_Mining.pdf \
  "Harfang Exploration Inc. TECH REPORT.pdf" \
  "Kenorland Minerals Ltd. TECH REPORT.pdf" \
  "New Found Gold Corp. tech report.pdf"

# Re-extract just the tables of one report, including scanned/sideways image tables
uv run python -m pipeline.run Gold_X2_Mining.pdf \
  --stages ocr,parse,det_tables,verify --vision-image-tables --force
```

Extraction backend variables:

| Var | Default | Effect |
|---|---|---|
| `PIPELINE_OCR_BACKEND` | `mistral` | `mistral` for OCR or `digital` for a tagged PDF's embedded text layer. |
| `PIPELINE_VISION_BACKEND` | `mistral` | `mistral` or `opencode` for figure descriptions. |
| `PIPELINE_VISION_MODEL` | `openai/gpt-5.6-luna` | OpenCode model used for figure descriptions. |
| `PIPELINE_VISION_WORKERS` | `1` | Parallel OpenCode figure-description requests. |

---

## 4. Analysis pipeline

Input is an **extraction output directory**, not a PDF, and **one directory per
invocation** (batch multiple reports with the runner in §4.3):

```bash
uv run python -m analysis.run output/<slug> [options]
```

| Option | Effect |
|---|---|
| `--force` | Rebuild cached HIE artifacts (otherwise a completed report is served from cache). |
| `--question "…"` | The question that drives DKI routing + the PEQA answer. Default: *"Assess the project evidence and main technical risks."* |

> There is **no `--stages` and no `--no-llm`** anymore. Stage selection is gone;
> the LLM is toggled entirely by the `ANALYSIS_LLM_BACKEND` env var. Leaving it
> at the default (`heuristic`) is the modern equivalent of the old `--no-llm` —
> the whole pipeline runs deterministically with zero model calls.

**Stages** (in order): `HIE → metrics → flags → DKI → PEQA → findings`.

| Stage | What it does | Model? |
|---|---|---|
| **HIE** | Hierarchical information extraction: harvests grounded assertions, types tables + maps columns, classifies figures, processes map figures, reads legend/geo crops, and reduces to an entity hierarchy, location candidates, and project stage → `report_metadata.json`. | LLM + VLM, **gated** (deterministic fallback when backend = `heuristic`). |
| **metrics** | Deterministic recompute over the typed records — resource, intercepts, QA/QC, tenure. | deterministic |
| **flags** | Deterministic red-flag rules over metrics + metadata. | deterministic |
| **DKI** | Domain-knowledge injection: routes the question to taxonomy classes and surfaces the matching grounded assertions. | deterministic |
| **PEQA** | Assembles the answer pack, (optionally) produces the QP narrative answer, validates every claim, and renders `memo.md`. | LLM for the QP answer only (**gated**); memo + validation are deterministic. |

### 4.1 Environment variables

All are optional; defaults give a safe, deterministic, $0 run.

| Var | Default | Effect |
|---|---|---|
| `ANALYSIS_LLM_BACKEND` | `heuristic` | `heuristic` (no model), `opencode` (real LLM/VLM), or `fake` (tests). |
| `OPENCODE_SERVER_URL` | *(none)* | Address of your `opencode serve`. **Required** when backend = `opencode`. |
| `ANALYSIS_LLM_MODEL` | `openai/gpt-5.5` | Model id passed to opencode. |
| `ANALYSIS_TABLE_MODEL` | `ANALYSIS_LLM_MODEL` | Optional model for table-column mapping. |
| `ANALYSIS_FIGURE_MODEL` | `ANALYSIS_LLM_MODEL` | Optional model for figure classification. |
| `ANALYSIS_SPAN_MODEL` | `ANALYSIS_LLM_MODEL` | Optional model for grounded span extraction. |
| `ANALYSIS_IMAGE_MODEL` | `ANALYSIS_LLM_MODEL` | Optional model for map image reads. |
| `ANALYSIS_ANSWER_MODEL` | `ANALYSIS_LLM_MODEL` | Optional model for final PEQA synthesis. |
| `ANALYSIS_LLM_VARIANT` | `low` | Reasoning effort for normal calls. |
| `ANALYSIS_LLM_HEAVY_VARIANT` | `medium` | Reasoning effort for heavy calls (e.g. the QP answer). |
| `ANALYSIS_MAP_BACKEND` | `none` | Map-figure detector: `none` or `peace` (needs peace-tool-pool). |
| `ANALYSIS_MAP_VLM` | `on` | VLM legend/geo crop reads: `on` / `off`. |
| `ANALYSIS_VLM_WORKERS` | `1` | Per-report VLM crop-read fan-out (thread pool size). |
| `ANALYSIS_LLM_MAX_CALLS_PER_RUN` | `1000` | Hard cap on LLM calls per run (throttles when hit). |
| `ANALYSIS_LLM_TOKEN_BUDGET_PER_RUN` | *(none)* | Optional per-process token budget (throttles when hit). |
| `ANALYSIS_REPORT_WORKERS` | `1` | Batch runner only: number of report-level worker processes. |

A typical **full** (LLM + VLM + maps) invocation:

```bash
export ANALYSIS_LLM_BACKEND=opencode
export OPENCODE_SERVER_URL=http://127.0.0.1:8779
export ANALYSIS_MAP_BACKEND=peace     # optional: analyse map figures
export ANALYSIS_MAP_VLM=on
uv run python -m analysis.run output/gold_x2_mining --force
```

### 4.2 Outputs

Outputs land in `output/<slug>/analysis/` — `memo.md` (people) and
`findings.json` (downstream code) are the entry points, backed by
`report_metadata.json` (the HIE blob), `flags.json`, and the
`records/`, `metrics/`, `hie/`, `hierarchy/`, `dki/`, and `peqa/` evidence chain.
See [OUTPUT_STRUCTURE.md](OUTPUT_STRUCTURE.md) for the full contract.

### 4.3 Analysing all reports (batch)

`analysis.run` takes a single directory. To run every extracted report, use the
batch runner — it discovers `output/*/` dirs that have a `manifest.json`,
runs each with `force=True`, scrapes the per-run counters, and writes a
scorecard to `output/_run_all_scorecard.json`:

```bash
# serial
uv run python scripts/run_all_reports.py

# parallel across reports (set the same LLM env as above first)
ANALYSIS_REPORT_WORKERS=4 uv run python scripts/run_all_reports.py
```

---

## 5. Re-runs, caching, and cost

- **Idempotent & resumable.** Extraction records completed stages
  (`stage_state.json`) and caches every LLM response by content hash under
  `output/<slug>/.cache/mistral/` or `.cache/vision/opencode/`. Analysis records
  per-stage state in `analysis/analysis_state.json` and caches LLM/VLM responses
  under `output/<slug>/analysis/.cache/llm/opencode/`. Re-running skips finished work
  and serves calls from cache; use `--force` to override.
- **Cost.**
  - *Extraction* — Mistral OCR ≈ $0.001/page (~$1.10 for all four reports).
    Digital OCR is local; figure-description cost depends on the selected
    Mistral or OpenCode model. The Sonnet vision hybrid is ~$0.05–0.15 per table
    page and only fires on N-up / `--all-vision` / `--vision-image-tables` pages.
  - *Analysis* — **$0.** It runs either fully deterministic (default) or over
    `opencode` + `gpt-5.5` on your OAuth session, which is not metered.
- **Telemetry is cache-miss-only.** The `llm.tokens.*` / `vlm.tokens.*` counters
  only accrue on live model calls — a warm (cached) analysis run reports ~0
  tokens. To measure true token spend you must run cold (`--force` with a cleared
  cache). See
  [docs/design/dki-cprime-image-hardening-dogfood-findings.md](docs/design/dki-cprime-image-hardening-dogfood-findings.md)
  for the measured breakdown.
- **Failure isolation.** Extraction continues past a per-report failure; analysis
  degrades gracefully — if the opencode server is unreachable, every model seam
  falls back to its deterministic path rather than crashing.

---

## 6. How long it takes

Wall-clock is **dominated by the model/network stages**; the deterministic
stages are seconds.

**Extraction, by stage:**

| Stage | Type | Typical time | Scales with |
|---|---|---|---|
| `render`, `ground_truth` | deterministic | seconds–1 min | page count |
| `ocr` | Mistral or local digital text | seconds–minutes | page count (chunked + cached) |
| `parse`, `det_tables`, `verify`, `manifest` | deterministic (+Sonnet on N-up/vision pages) | seconds–minutes | table count |
| `describe` (figures) | Mistral or OpenCode | minutes | figure count (1 call/figure) |

**Analysis** — a bare deterministic run (`heuristic`) is **seconds to a couple
of minutes**. With the LLM/VLM on (`opencode`), it is dominated by the number of
live `gpt-5.5` calls (each ~11–15 s, serial). Measured **cold** runs
(`ANALYSIS_LLM_BACKEND=opencode`, VLM on):

| Report | LLM calls | VLM calls | Cold wall-clock |
|---|---|---|---|
| `gold_x2_mining` | 54 | (VLM-heavy) | ~22 min |
| `new_found_gold_corp` | 163 | 116 | ~60 min |

> A **warm** re-run (unchanged inputs) hits cache and finishes in **seconds**.
> The pipeline is currently serial; the real ceiling is the opencode server +
> OAuth rate limit, not the code. See the findings doc referenced in §5 for the
> full token/spend profile.

---

## 7. Checking the results

**Extraction — trust the tables:**
- Open `output/<slug>/summary.md`: counts at top, then the **review queue** of
  any table that isn't high-confidence.
- For any table, open `tables/<id>.csv` next to `tables/<id>.crop.png` and
  eyeball-match the numbers. `tables/<id>.meta.json` shows the
  `text_layer_checksum` and the `confidence`. Image-only tables read via
  `--vision-image-tables` are `unverifiable` by construction — verify by eye.

**Analysis — trust the citations:**
- `memo.md` opens with the **Grounded Evidence Digest**, organized by NI 43-101
  item. Each bullet is a verbatim, source-grounded assertion tagged
  `_[<section_slug> p.N]_`.
- **Verified Metric Claims** carry a table-level citation `_[<table_id> p.N]_`
  (the number traces to that table and our recompute reconciles). **Deterministic
  Flags** list the fired red-flag rules.
- **QP Assessment** (the narrative opinion) renders **only if the LLM answerer
  ran**; it is a subordinate section, not the primary deliverable. Any claim in
  it stating a number that isn't source-backed is stamped `**[UNVERIFIED]**`.
- `findings.json` carries the same content for code: `grounded_digest`,
  `claims` (each with its `validated` verdict), the recomputed `resource` /
  `intercepts` / `qaqc` / `tenure`, the fired `flags`, and `qp_assessment`.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `MISTRAL_API_KEY is not set` (extraction) | No key in `.env` or environment. The deterministic extraction stages still run with `--no-llm`. |
| Extraction stops with `429 rate_limited` | Mistral throttling. It retries with backoff; just re-run — cached work resumes and only the remaining calls are made. |
| Analysis produced no LLM content / memo has no QP Assessment | The LLM backend was off (`ANALYSIS_LLM_BACKEND=heuristic`, the default) **or** the opencode server was unreachable, so seams fell back to deterministic. Set `ANALYSIS_LLM_BACKEND=opencode` and a reachable `OPENCODE_SERVER_URL`, then `--force`. |
| Analysis stops with `opencode token budget reached` / `max-call cap reached` | You hit `ANALYSIS_LLM_TOKEN_BUDGET_PER_RUN` / `ANALYSIS_LLM_MAX_CALLS_PER_RUN`. Raise the cap or re-run (cached calls resume for free). |
| No map components / `hie.n_maps_processed = 0` | Map analysis is off. Set `ANALYSIS_MAP_BACKEND=peace` and ensure peace-tool-pool synced (`bash scripts/sync-peace-tool-pool.sh`). |
| `peace-tool-pool` MCP fails to start | Run `bash scripts/sync-peace-tool-pool.sh`. If the checkout diverged, delete `.cache/peace-tool-pool` or set `PEACE_TOOL_POOL_ROOT` to a clean checkout. |
| `[skip] … no manifest.json` (analysis) | You pointed `analysis.run` at a directory the extraction pipeline hasn't completed. Run extraction first. |
| A table is in the review queue | Its OCR numbers disagreed with the PDF text layer (`low`) or the page has no text layer (`unverifiable`). Verify by hand against the `.crop.png` — this is the pipeline flagging, not hiding, a risk. |
| Want to start clean | Delete `output/<slug>/` (extraction) or `output/<slug>/analysis/` (analysis) and re-run; both regenerate fully. |
