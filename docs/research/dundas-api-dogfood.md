# Dundas HTTP and MCP dogfood findings

## Outcome

**DONE_WITH_CONCERNS.** On 2026-09-30, real HTTP and MCP clients exercised the service against six original Dundas files. Native CSV and workbook values matched their sources. Published products, paginated reads, snapshot reruns, and restart persistence passed verification. PDF content quality did not pass: shipment data disappears from typed tables, target-table columns shift, map labels are wrong, and engine instructions appear in successful interpretation products. A live 38-page report exhausted the configured 900-second deadline without publishing a result.

Application code and original source files were not changed. The evaluation created this report and disposable scripts and artifacts. The service processes and their listeners were shut down. These are single-run observations, not a throughput benchmark or a full geological accuracy assessment.

## What ran

The source workspace was `/home/elijah/geo-report-pipeline/output/wa-dundas/dataset/source/1. WA_Dundas`. The evaluation used the current `preprocessing` application, not the historical `pipeline` commands. The resolved application source digest was `8a692ec1061f6108ea074507aac11d6acff39c612dd911019dca33c2ab2a497c`.

Two isolated, bearer-authenticated loopback owners exposed HTTP and Streamable HTTP MCP. The native owner had no engine. The live owner used OpenCode 2.0.19 at `/tmp/opencode/preprocessing-cli-2.0.19/node_modules/.bin/opencode`, with `opencode/space-bunny-free`. No provider credential was available in the environment. Extraction options retained their defaults, including figure descriptions. The profile changed only `limits.jobSeconds` to **900**; the repository default is **3600**. Therefore, the large-report failure establishes a failure under the evaluation's 15-minute budget, not failure under the default one-hour budget.

| Fixture | Workspace-relative original source | Reference identity, SHA-256 |
| --- | --- | --- |
| CSV | `GIS_South_Dundas/Qgis-working/Mailman Hill/Mailman Project Mineralised Intersections.csv` | `0e1e90325c34408785a74557e4e2df9d49c8cc8539fb71b630afa84ee5f9af3d` |
| XLSX | `Geology/Working_Files/04_Working_Files/Dundas Project Mineralised Intersections.xlsx` | `d48bee7f7b42934bab2520d83ec9ea6a7acf522de621ba39371aa3d1968c8474` |
| Manifest PDF | `2000_Drilling/manifest.pdf` | `23fa625d80c8bf37de3141031111abb9c4cb0d7de18180762d54b77a350e75cf` |
| Map PDF | `Exploration Incentive Scheme/MtBelches_gsdar2005-06_nd.pdf` | `8cb5ea67ab47c7bf53390ef8e4a97bea2dd378ad0108817d2e9835c0919e4993` |
| SGC4145 | `Exploration Incentive Scheme/Appendix 1 - geophysical targeting report.pdf` | `05055bbef09bbdf64df0b4314ef7583ce37031bfe983fb7667c8627cf3a39cef` |
| SGC4119 | `Geophysics/SGC 4119 Lightning Dundas Historical Data Review.pdf` | `0734b7713141eaecfec33512b352db9ae3b5cda3361178583f74a5339842c733` |

The earlier processing corpus stores documents under their source hashes. It supplied comparison tables and reviewed report evidence. Its tables are labelled low-confidence and its vision supplements are unverified. Original source bytes and visible source pages were the authority for the checks below.

## Observed job outcomes

Durations run from HTTP admission to observed final publication or failure. They include polling delay and are not parser-only times. Table and figure counts describe detected products, not verified recall.

| Owner | Fixture | State | Shared publication | Observed seconds | Tables | Figures |
| --- | --- | --- | --- | ---: | ---: | ---: |
| Native | CSV | `succeeded` | `ready` | 0.217 | 1 | 0 |
| Native | XLSX | `succeeded` | `ready` | 0.623 | 5 | 0 |
| Native | Manifest PDF | `partial` | `ready` | 1.232 | 6 | 2 |
| Native | Map PDF | `partial` | `ready` | 1.640 | 3 | 1 |
| Native | SGC4145, 38 pages | `partial` | `ready` | 7.138 | 10 | 17 |
| Native | SGC4119, 19 pages | `partial` | `ready` | 5.520 | 2 | 14 |
| Native | Exact CSV snapshot rerun | `succeeded` | `ready` | 0.222 | 1 | 0 |
| Live | Map PDF, 4 pages | `succeeded` | `ready` | 119.496 | 3 | 1 |
| Live | Manifest PDF, 2 pages | `succeeded` | `ready` | 21.100 | 6 | 2 |
| Live | SGC4145, 38 pages | `failed`, `RESOURCE_LIMIT` | None | 900.740 | No finalized manifest | No finalized manifest |

The native PDF partial states correctly disclose unavailable model operations. Native evidence covers all 63 physical pages across those four documents. This does not establish completeness of text inside raster images.

## Successful API and native-data checks

The client exercised capabilities, bearer authentication, admission, submission-key lookup, identical replay, conflicting replay, job polling, product downloads, byte-range downloads, typed-row pagination, history pagination, shared-publication reconciliation, and snapshot reruns. It also checked workspace traversal rejection, browser mutation rejection, invalid row pagination, and asynchronous failure for a missing source.

A real MCP client initialized the transport and discovered seven tools. HTTP and MCP returned the same replayed job and the same typed rows. MCP text chunks reconstructed the complete CSV export. A separate CSV snapshot submission created a new generation with identical rows. After the owner stopped and restarted, all seven native published results remained accessible through HTTP with unchanged result IDs. These were sequential workflow checks, not a concurrency stress test.

Independent source checks found:

- **CSV:** all 26 rows and **208 fields** matched typed rows and exported CSV, including empty fields.
- **XLSX:** all **1,153 explicit source-XML cells** matched exact types and values: 556 text cells and 597 numeric strings. All five sheets matched, with no extra non-absent cells. For example, `Au!C2` retains `6505429.1100000003`, exactly as stored in source XML.
- **SGC4145 targets:** all **28 target IDs, 28 priorities, and 56 coordinate values** matched independent source-text reads. This validates those values, not the extracted table structure.
- **PDF native text:** independent PDFium reads found no alphanumeric-glyph deficits across 63 pages. Reading order, word relationships, and raster-only text were not exhaustively validated.

Native previews now show readable cell values rather than escaped cell-metadata objects. The `Au` preview displays `WDR1660`, `472951.48`, and `6505429.1100000003`; it explicitly says that the preview is truncated.

## Finding 1: engine-control instructions become successful document products

The live map interpretation begins:

```text
?

CRITICAL - MAXIMUM STEPS REACHED

The maximum number of steps allowed for this task has been reached.
Tools are disabled until next user input. Respond with text only.
```

It then includes operational instructions such as `Do NOT make any tool calls`, followed by map prose. The complete product contains **220 words**, despite the requested maximum of 120. Its `interpretationStatus` is `succeeded`; the map manifest also says `outcome: succeeded` and has `findings: []`.

This is not concatenation of separate assistant turns. Operation `operation_ad264a6bdccf4ee7937aa71f8efd7453`, session `ses_f0e6462b1ffelB5syfsHzNtj7Z`, records one completed attempt. The engine database contains one assistant message with one text part. That text is byte-for-byte equal to the published interpretation. The submitted user prompt contains no maximum-steps instruction.

`preprocessing/engine.py:228–230` configures the agent with `steps: 1`. In the pinned engine, the step counter starts at 1 and `SessionRunner.runStep` appends the maximum-steps message as an assistant-role instruction when `step >= agent.steps`. Thus, the instruction is injected before the first generation. Whether the provider treats it as a prefix or the model echoes it is not established by the retained evidence. Both successful manifest interpretations also contain maximum-steps summaries. Three completed SGC4145 staging OCR operations begin with such summaries instead of clean transcriptions.

`preprocessing/formats/pdf.py:198–206` checks nonempty output and byte size but does not establish that the returned text satisfies the extraction task. The first correction to investigate is the engine's step-limit configuration and the distinction between a completed operation and valid extraction output. Blindly stripping arbitrary phrases would risk removing legitimate source text and would not restore missing transcription.

## Finding 2: typed PDF tables omit and misalign important values

### Manifest shipment row is absent

The successful live manifest has six typed tables containing 18 rows. None contains exact cell values `59362`, `2637893680`, `nagrom`, `KELMSCOTT`, `6111`, or `12294990`. Complete paginated API rows equal the published JSONL, so this is not a pagination omission.

The native page text retains the shipment:

```text
59362 General 2637893680 nagrom KELMSCOTT 6111 1 300.0 1.44
References: 12294990 , 6111 000001 2637893680
```

The extracted tables retain headers and totals such as `TOTALS: 1,1,300.0,1.44,`, but lose the main shipment row. Full page coverage and successful publication therefore do not establish table completeness.

### SGC4145 target-table structure shifts

On physical page 21, the visible source target table has six logical columns and 13 rows including its header. `table_0004` emits **20 rows and 18 columns**. Wrapped descriptions become extra rows. The description for `DW_01` splits between row 2, column 8 and row 3, column 8:

```text
East striking fault, running out from large multi-stage
granitic intrusion, parallel to mapped Li anomalies.
```

For `DW_05` in output row 8, the ID, priority, X, and Y appear in columns **2, 5, 11, and 14**, while the corresponding header labels occupy columns **1, 4, 10, and 13**. `DE_01` on page 22 shifts similarly. A fixed-column consumer can therefore attach the wrong value or miss a target even though all target values exist somewhere in the output. Legacy extraction shares these defects and cannot serve as a structural oracle.

The PDF detector also emits false tables. SGC4145 page 25 produces a four-row, one-column table from a wrapped `NOTE: Due to the various acquisition parameters...` paragraph. The map produces three tables from map and legend geometry; labels become fragmented or interleaved. PDF table structure and recall need source-based acceptance checks beyond raw token retention.

## Finding 3: a full report spends its deadline on model operations

SGC4145 job `job_e199b32050f2405ba4e5218d9dceaad0` persisted `failed` with `RESOURCE_LIMIT`. Job timestamps span **900.020 seconds**. There were **16 operations: 14 completed and two with `outcome_unknown`**. Their nonoverlapping ledger intervals total **897.356 seconds**. These durations include provider waiting, adapter activity, and settlement; they do not isolate model inference.

Two avoidable operations are directly evidenced:

- **False figure on page 4:** the page says `LIST OF FIGURES`, contains dotted-leader navigation entries, and has no embedded images. It is classified as one whole-page `caption-page` figure. Its OCR and description consume **49.406 seconds**. The same false detection occurs in the native SGC4119 result. The separate contents pages are not falsely detected.
- **Repeated whole-page OCR on page 14:** page OCR and figure OCR use identical bounds `[0, 0, 842.04, 595.32]`. The first occupies **180.090 seconds** and has unknown remote outcome; the second completes in **79.196 seconds**. Together they consume **259.285 seconds**. They are separate tasks with different requested output schemas, not recorded retries.

Seven completed descriptions add **139.553 seconds**. Page-9 figure OCR takes **179.847 seconds**. The final page-16 OCR occupies the remaining **164.163 seconds** before the global deadline and has unknown remote outcome.

Native staging evidence reaches pages 1–16 of 38. No finalized manifest or shared SGC4145 live generation exists; `pages.jsonl` is empty. Engine startup succeeded and rendered images exist. The evidence supports model-path deadline exhaustion, not a demonstrated parser or engine-startup crash. The failed result correctly avoids publishing unfinished staging as a successful generation, but useful early evidence is unavailable through the result API.

Removing list-of-figures false positives and sharing suitable whole-page evidence across OCR tasks are focused opportunities. A longer deadline alone does not establish that the complete report will finish or produce accurate content.

## Finding 4: successful map OCR still changes names

The live map produces one coherent full-page image on physical page 2, printed page 78. Targeted visual comparison with that image finds:

| Visible source label | Published OCR | Assessment |
| --- | --- | --- |
| Mount Monger Fault | Mount Wonnet Fault | Wrong name |
| ERAYINIA | No matching label | Omitted |
| YARDILLA | YARDILANA | Wrong spelling |
| BULDANIA | BULUNDA | Wrong spelling |
| 177917 and 177920 | Both retained | Correct |

This sample is not an overall OCR accuracy percentage. The operation succeeded and its prompt explicitly prohibited guesses. That does not establish faithful source transcription.

## Source inconsistency retained by extraction

SGC4145's target tables contain **11 priority-1, nine priority-2, and eight priority-3 targets**, totaling 28. Its prose instead says `ten priority 1` and 18 additional targets. This inconsistency exists in the source. It is not an extraction error and should not be silently normalized to match the prose.

## Evidence and reproduction

The retained evaluation root is `/tmp/opencode/dundas-api-20260930-b7e295`. Each owner's `evidence` directory contains its command, profile, capabilities, jobs, runs, manifests, complete API rows, summaries, checks, operations, and server log. Published products live under `native/documents` and `live/documents`. The failed live report's private staging remains under `live/.preprocessing/jobs/job_e199b32050f2405ba4e5218d9dceaad0/staging`.

The core evidence files are:

- `verification.json`: final independent integrity checks and polluted-response correlation.
- `native/evidence/content-audit.json`: source-XML cells, CSV fields, target checks, PDFium comparisons, and sampled visual findings.
- `live/evidence/deadline-audit.json`: operation durations, duplicate OCR, false figure, unknown outcomes, and shipment-row omissions.

The disposable scripts are `/tmp/opencode/dundas_api_dogfood.py` and `/tmp/opencode/dundas_api_finalize.py`. Commands run from `/home/elijah/data-preprocessing-service` were:

```sh
uv run python /tmp/opencode/dundas_api_dogfood.py native
uv run python /tmp/opencode/dundas_api_dogfood.py native-resume
uv run python /tmp/opencode/dundas_api_dogfood.py live map manifest sgc4145
uv run python /tmp/opencode/dundas_api_finalize.py
```

The first native run processed all six originals, then stopped because the evaluator incorrectly expected synchronous rejection of a nonexistent source. The corrected `native-resume` continued against the retained jobs without rerunning those six extractions. A separate initial CSV probe under `/tmp/opencode/dundas-api-20260930-b7e294` exposed another evaluator assumption: extraction can be terminal while shared publication is still `pending`. Publication completed normally, and the client was corrected to poll both states. These harness mistakes were not classified as service defects. The native corpus also contains two intentionally missing-source jobs, both ultimately failed with `SOURCE_NOT_FOUND`.

## Verification

**Decision:** direct execution, independent artifact inspection, and targeted visual review. No maintained test was added; this is a disposable evaluation with no application behavior change.

The final command exited 0 and reported:

```text
ARTIFACT_CHECKS: PASS
publications: 9
manifestListedProducts: 476
sealedPublishedHashSizeChecks: 952
navigationLinks: 577
completeApiTables: 37
apiWorkflowChecks: 130
apiCheckFailures: 0
artifactMatchesStoredText: true
```

The 952 checks cover every listed product's SHA-256 and size in both sealed and published locations. The original network harness also downloaded every listed product and checked its bytes against its manifest. All nine source snapshots match their manifests and original files; sealed and published manifests are byte-identical. All 577 checked navigation links resolve. All 37 complete paginated table reads match the stored typed rows.

Independent native checks ran with `/home/elijah/data-preprocessing-service/.venv/bin/python` against `/tmp/opencode/native_content_inventory.py`, `/tmp/opencode/native_content_audit.py`, and `/tmp/opencode/native_content_finalize.py`. All exited 0. Their decisive results include `product_failures: 0`, `target_checks: 28`, matching CSV typed and exported fields, and zero mismatches on all five workbook sheets.

Limitations: the application test suite was not run because no application code changed. No cancellation or coordinator-loss fault was injected, no concurrency stress test was performed, and no exclusive memory benchmark was made. SGC4119 was not run with a live model. The live SGC4145 job did not complete; its staging products are not approved outputs. Model-reported costs are not a billing guarantee. Both unknown remote outcomes remain unknown, and final-operation remote settlement is not explicitly evidenced. Integrity success does not override the content defects above.
