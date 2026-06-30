# How to use

Two pipelines run back-to-back (see [PIPELINE.md](PIPELINE.md) for the design,
[OUTPUT_STRUCTURE.md](OUTPUT_STRUCTURE.md) for what they emit):

1. **Extraction** — a PDF report → a structured directory (`output/<slug>/`).
2. **Analysis** — that directory → a conservative-QP assessment (`output/<slug>/analysis/`).

You run extraction first, then point analysis at its output.

---

## 1. Prerequisites

| Need | Check | Notes |
|---|---|---|
| **Nix + direnv** | `nix --version`, `direnv version` | Loads the repo dev shell and syncs the map-analysis MCP dependency. Run `direnv allow` once after checkout. |
| **uv** (Python manager) | `uv --version` | Creates/manages the venv from `pyproject.toml`; no manual `pip`/`venv`. |
| **poppler CLI** | `which pdftoppm pdftotext pdfinfo` | Used by the deterministic extraction stages (render + ground-truth). |
| **peace-tool-pool MCP** | `bash scripts/sync-peace-tool-pool.sh` | Required for map HIE/geospatial DKI. Defaults to latest upstream `main` at `.cache/peace-tool-pool`; also syncs every MCP startup. |
| **`MISTRAL_API_KEY`** | see below | Required for the LLM stages (OCR, figure descriptions, normalize, assess). Deterministic stages run without it. |

Dependencies install automatically the first time you `uv run` anything (or run
`uv sync` once up front). Runtime deps: `mistralai`, `pandas`, `pillow`,
`python-dotenv`.

See [docs/environment.md](docs/environment.md) for the Nix/direnv setup, the
latest-main `peace-tool-pool` policy, and the current non-Nix fallback.

### Set the API key

The key is read from a **`.env`** file at the repo root (gitignored, loaded
automatically with override). Copy the template and fill it in:

```bash
cp .env.example .env
# then edit .env:
# MISTRAL_API_KEY=your-key-here
```

You can also `export MISTRAL_API_KEY=…` in your shell instead — either works.

---

## 2. Quick start

The four source PDFs live at the repo root. To process one report end-to-end:

```bash
# Extraction: PDF -> output/gold_x2_mining/
uv run python -m pipeline.run Gold_X2_Mining.pdf

# Analysis: output/gold_x2_mining/ -> output/gold_x2_mining/analysis/
uv run python -m analysis.run output/gold_x2_mining
```

Then open the two human entry points:

```bash
output/gold_x2_mining/summary.md          # extraction report card + table review queue
output/gold_x2_mining/analysis/memo.md    # conservative-QP due-diligence memo
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
| `--no-llm` | Run only the **free deterministic core** (render + ground-truth); skips all network calls. |
| `--force` | Re-run stages even if already completed (ignores cache/state). |
| `--all-vision` | Rebuild **every** table-bearing page with the Sonnet vision hybrid, not just N-up pages — for a report whose default extraction is messy on single tables too. Numbers still come from the text layer. Needs `ANTHROPIC_API_KEY`; ~$0.05–0.15 per table page. |

**Stages** (in order): `ingest` (always runs), `render`, `ground_truth`, `ocr`,
`parse`, `det_tables`, `verify`, `describe`, `manifest`. The first two are free;
the rest need `MISTRAL_API_KEY`.

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

# Re-extract just the tables of one report
uv run python -m pipeline.run Gold_X2_Mining.pdf --stages ocr,parse,det_tables,verify --force
```

---

## 4. Analysis pipeline

Input is an **extraction output directory**, not a PDF:

```bash
uv run python -m analysis.run output/<slug> [output/<slug2> ...] [options]
```

| Option | Effect |
|---|---|
| `--stages LIST` | Subset of `normalize, compute, flags, assess`. |
| `--no-llm` | Run only the deterministic stages (`compute`, `flags`); skips `normalize` + `assess`. |
| `--force` | Re-run even if already done / cached. |

**Stages** (in order): `normalize` (LLM), `compute`, `flags`, `assess` (LLM).

Examples:

```bash
# Full analysis of one report
uv run python -m analysis.run output/gold_x2_mining

# Deterministic only — recompute metrics + red-flag rules, no LLM (needs normalize to have run once)
uv run python -m analysis.run output/gold_x2_mining --no-llm

# Re-write just the memo after a prompt change
uv run python -m analysis.run output/gold_x2_mining --stages assess --force

# Analyse all four extracted reports
uv run python -m analysis.run output/*/
```

Outputs land in `output/<slug>/analysis/` — `memo.md` (people), `findings.json`
(downstream code), `flags.json`, plus the `records/` and `metrics/` evidence
chain.

---

## 5. Re-runs, caching, and cost

- **Idempotent & resumable.** Both pipelines record completed stages
  (`stage_state.json` / `analysis_state.json`) and cache every LLM response by
  content hash (`.cache/mistral/`). Re-running skips finished work and **never
  re-bills** unless inputs changed. Use `--force` to override.
- **Failure isolation.** Processing multiple reports continues past a failure;
  the analysis pipeline also isolates per-table normalize failures.
- **Cost.** OCR is ≈ $0.001/page (~$1.10 for all four reports); figure
  descriptions and the analysis LLM calls add a few dollars total. The large
  583-page report's `normalize` stage takes ~25 min and is rate-limit-prone —
  it retries with backoff automatically, and is cached, so a re-run resumes
  instantly.

---

## 6. How long it takes

Wall-clock is **dominated by the LLM/network stages**; the deterministic stages
are seconds. Times vary with report size and Mistral rate-limiting (throttling
triggers automatic backoff, which stretches the LLM stages). A re-run that hits
cache finishes in seconds.

**By stage:**

| Stage | Type | Typical time | Scales with |
|---|---|---|---|
| `render`, `ground_truth` | deterministic | seconds–1 min | page count |
| `ocr` | LLM / network | minutes | page count (chunked + cached) |
| `parse`, `det_tables`, `verify`, `manifest` | deterministic | seconds–1 min | table count |
| `describe` (figures) | LLM | minutes | figure count (1 call/figure) |
| `normalize` | LLM | **the dominant cost** — ~12–18 s per *typed* table | # tables that get an LLM call |
| `compute`, `flags` | deterministic | < 5 s | — |
| `assess` | LLM | ~1–2 min | single call |

**Analysis, by report** (measured wall-clock):

| Report | Tables | Typed (LLM calls) | Full analysis |
|---|---|---|---|
| `gold_x2_mining` | 29 | 21 | ~8 min |
| `harfang_…` | 69 | 51 | ~15 min |
| `kenorland_…` | 121 | 104 | ~31 min |
| `new_found_gold_…` | 185 | 50 | ~28 min |

> **Total table count is a poor predictor — *typed* tables drive the time.**
> Kenorland (121 tables) took *longer* than New Found Gold (185) because
> `normalize` only calls the LLM on tables it can type as data: NFG's pre-filter
> skipped most of its 185 tables (only 50 typed), while Kenorland typed 104.
> Rate-limiting adds further variance on top.

These are real runs — rate-limiting can still shift them by minutes. Two ways to go faster:

- **`--no-llm`** runs only the deterministic stages (seconds to ~1 min) — useful
  to re-derive metrics and flags without waiting on the model.
- **Re-runs are nearly instant** for unchanged inputs: completed stages skip and
  every LLM call is served from `.cache/mistral/`.

---

## 7. Checking the results

**Extraction — trust the tables:**
- Open `output/<slug>/summary.md`: counts at top, then the **review queue** of
  any table that isn't high-confidence.
- For any table, open `tables/<id>.csv` next to `tables/<id>.crop.png` and
  eyeball-match the numbers. `tables/<id>.meta.json` shows the
  `text_layer_checksum` (how many CSV numbers were confirmed by the independent
  PDF text layer) and the `confidence`.

**Analysis — trust the memo's citations:**
- In `memo.md`, claims tagged `_[<table_id> p.N]_` are verified (number traces to
  that table **and** our recompute reconciles); claims tagged `**[UNVERIFIED]**`
  state a number that isn't source-backed and should not be relied on.
- `findings.json` carries the same claims with their `validated` verdict, plus the
  recomputed resource/intercept/QA-QC numbers and the fired `flags`.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `MISTRAL_API_KEY is not set` | No key in `.env` or environment. The deterministic stages still run with `--no-llm`. |
| LLM stage stops with `429 rate_limited` | Mistral throttling. It retries with backoff; just re-run — cached work resumes and only the remaining calls are made. |
| `peace-tool-pool` MCP fails to start | Run `bash scripts/sync-peace-tool-pool.sh`. If the checkout diverged or has local changes, delete `.cache/peace-tool-pool` or set `PEACE_TOOL_POOL_ROOT` to a clean checkout. |
| `[skip] … no manifest.json` (analysis) | You pointed `analysis.run` at a directory the extraction pipeline hasn't completed. Run extraction first. |
| A table is in the review queue | Its OCR numbers disagreed with the PDF text layer (`low`) or the page has no text layer (`unverifiable`). Verify by hand against the `.crop.png` — this is the pipeline flagging, not hiding, a risk. |
| Want to start clean | Delete `output/<slug>/` (extraction) or `output/<slug>/analysis/` (analysis) and re-run; both regenerate fully. |
