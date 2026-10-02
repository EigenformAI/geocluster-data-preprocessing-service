# Dundas preprocessing dogfood results

## Latest outcome after the detector correction

**DONE_WITH_CONCERNS:** the corrected map run and the previously unrun manifest both completed within the new five-minute deadline. Map coverage is four of four physical pages, and manifest coverage is two of two. Page 2 now contains one coherent full-page map image. Artifact integrity and navigation checks passed, but map transcription and interpretation contain factual errors. The manifest's typed tables omit its main shipment row, which remains available in native page text. Detailed corrected-run evidence is appended below. The original attempt and its output directory are retained unchanged.

## Initial attempt outcome

2026-09-30, 04:25–04:35 UTC. **BLOCKED:** two native fixtures passed. The geological-map PDF exceeded the requested 600-second job deadline. The second PDF was not run because the evaluation stopped after that failure. There were three fixture invocations, no reruns, and no further model calls during verification.

The application code and input files were not edited. Disposable measurement and verification scripts live under `/tmp/opencode`. No commits were made. These are single-run observations, not throughput or capacity estimates.

## Inputs and commands

Commands ran in `/home/elijah/data-preprocessing-service`. The source root was:

```text
/home/elijah/geo-report-pipeline/output/wa-dundas/dataset/source/1. WA_Dundas
```

| Order | Source relative to that root | Outcome |
| --- | --- | --- |
| 1 | `Geology/Working_Files/04_Working_Files/Dundas Project Mineralised Intersections.xlsx` | Exit 0, `succeeded`, publication `ready` |
| 2 | `GIS_South_Dundas/Qgis-working/Mailman Hill/Mailman Project Mineralised Intersections.csv` | Exit 0, `succeeded`, publication `ready` |
| 3 | `Exploration Incentive Scheme/MtBelches_gsdar2005-06_nd.pdf` | External timeout after 600 seconds, exit 143 |
| 4 | `2000_Drilling/manifest.pdf` | Skipped after the timeout; read-only inspection confirms 2 pages |

The retained output root is `/tmp/opencode/dundas-preprocessing-20260930-9f73c2`, abbreviated as `ROOT` below. Each fixture used `uv run preprocess process SOURCE --output ROOT/FIXTURE`. The PDF invocation added exactly:

```sh
--opencode-executable /tmp/opencode/preprocessing-cli-2.0.19/node_modules/.bin/opencode --model opencode/space-bunny-free
```

The service's resolved profile records source digest `d1ba7bfa3396bae15ff78ee52883d048779235dfa9f451336c9c14eec6746f1a`. Default extraction options were retained. In particular, figure descriptions remained enabled, and the service's internal `jobSeconds` remained 3600. The evaluation wrapper enforced the requested 600-second deadline externally.

## Resource observations

PSS is proportional resident memory, which apportions shared mappings between processes. Active measurements sum the launched `uv` process, service, parser, engine guardian, and OpenCode descendants. The monitor itself is excluded. `/proc` traversal also tracked previously observed descendants after reparenting. No process environment files were read.

| Process or fixture | Wall time | Peak sampled tree PSS | Retained allocated disk | Logical disk bytes |
| --- | ---: | ---: | ---: | ---: |
| Idle HTTP/MCP service | 5.96 s, including startup and deliberate shutdown | 62.10 MiB | 0.035 MiB | 36,864 |
| XLSX | 0.890 s | 41.16 MiB | 0.598 MiB | 518,798 |
| CSV | 0.528 s | 41.32 MiB | 0.148 MiB | 92,603 |
| Map PDF, timed out | 600.098 s | 594.05 MiB | 41.504 MiB | 43,361,574 |

The idle service was started separately after both native fixtures, using `uv run preprocess serve`, the explicit executable and free-model flags, loopback, and an automatically assigned port. After application startup, it was observed for five seconds. Its last sample was 48.49 MiB PSS for the service and 62.08 MiB including `uv`. No engine process appeared. Deliberate SIGTERM returned 143, and the log records `Application shutdown complete.` This is an HTTP/MCP baseline, not the same process configuration as the batch CLI.

Sampling targeted 100 ms plus traversal overhead. Median observed intervals were 123–145 ms, with a maximum of 207 ms for the PDF. Native jobs yielded only six and four samples. Peaks between samples can be missed. Durations include process launch and monitoring overhead. Disk counts cover each fixture's output tree, including source snapshot, private state, and publication copies, but exclude measurement files and the preinstalled executable. Disk is retained size after termination, not peak disk use. The map's engine state accounts for 41,910,272 allocated bytes of its 43,520,000-byte total.

The available cgroup was shared: `/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/t3-serve.service`.

| Observation | Shared memory.current at start | Highest sampled shared memory.current |
| --- | ---: | ---: |
| Idle | 14.822 GiB | 15.388 GiB |
| XLSX | 14.867 GiB | 14.875 GiB |
| CSV | 14.910 GiB | 14.949 GiB |
| Map PDF | 15.356 GiB | 16.412 GiB |

`memory.peak` was 32,357,679,104 bytes, or 30.135 GiB, before and after every observation. This is a historical shared-cgroup peak, not an exclusive preprocessing peak. Concurrent parent tests and other processes were not isolated from this cgroup.

## Native artifact verification

The independent verifier read source ZIP/XML, an independent workbook reader, source CSV, sealed products, published products, and Markdown links. It reported:

```text
native_artifact_checks: PASS
product_hash_size_checks: 48
relative_links_checked: 36
xlsx_explicit_cells_checked: 1153
csv_fields_checked: 208
```

Both source hashes match their source snapshots and manifests. Before/after source hashes were unchanged. Sealed and published manifests are byte-identical. Every listed product matched its SHA-256 and byte count in both locations: 18 products per XLSX generation and six per CSV generation. No navigation links were broken. Published guides, generation indexes, table previews, and the top-level catalog exist.

| XLSX sheet | Emitted rows, including headers | Explicit source cells | Raw-value mismatches |
| --- | ---: | ---: | ---: |
| Sheet1 | 47 | 376 | 0 |
| Au | 48 | 448 | 0 |
| Zn | 5 | 49 | 0 |
| Ni | 21 | 197 | 0 |
| Ag | 9 | 83 | 0 |

The XLSX coverage says five of five sheets completed, matching independent enumeration. All 1,153 explicit text and numeric cells matched, including numeric spelling. There were no formulas in those cells. The JSONL retains absent-cell markers separately. Source exclusions in the manifest include pictures, charts, macros, formula calculation, and external-link refresh.

Meaningful examples from `Au` are `A2=WDR1660`, `B2=472951.48`, `C2=6505429.1100000003`, `F2=0.88`, and `P2=1m @ 0.88g/t Au`. The long spelling in C2 exists in source XML; it is not a conversion error.

The CSV contains 26 rows, including the header, and 208 fields. Every field matched in typed JSONL and the CSV export, including empty comments. Row 2 is `BYWA1,372084,6791900,AC,Au,1995,46185,Greywacke`. Row 26 is `MHRC015,368750,6803261,RC,Zn,2012,96847,`.

One navigation usability issue remains: previews expose truncated, escaped JSON cell structures rather than readable table rows. The CSV preview's first data row stops during the second cell's source metadata, before northing, anomaly, or comment. The guide correctly labels previews incomplete, and full-data links resolve. This does not invalidate the raw data, but the preview is poor for inspecting useful values.

## Map failure and content quality

The source has four physical pages. Page 2 is the geological map on printed page 78. The service found 19 embedded images on that page; the page also has a Figure 1 caption. At termination, the service had staged 13 figure PNGs, 13 OCR files, and 12 interpretation files. OCR files for figures 5 and 13 are empty. Only pages 1 and 2 had native text files. The buffered page inventory was empty, and no sealed manifest, guide, index, or published result existed. This output is unfinished staging evidence, not a consumable extraction result.

The operation ledger contains 25 model operations: 23 `completed`, one `outcome_unknown`, and one `admitted`. The unknown operation was OCR for `figure_p0002_0005`. It reached its 180-second model deadline with `PROVIDER_OUTCOME_UNKNOWN`. The service proceeded to later figures. At the external job deadline, OCR for `figure_p0002_0013` was still admitted. The database still says `running` after termination. The evaluation did not restart or reconcile that owner.

Recorded operation intervals did not overlap, and all recorded requested models were `opencode/space-bunny-free`. All returned-model fields are null. The 23 completed operations reported USD 0, but usage is marked `incomplete`. The earlier unknown remote outcome prevents a claim about remote cancellation or remote exclusivity. Local PIDs observed for this job were all absent during the subsequent `/proc` check.

Fragment processing consumed the budget without completing the map. For example, `figure_p0002_0012.png` is only 3×5 pixels, yet it received OCR and a separate interpretation operation. Its interpretation begins, `Maximum processing steps have been reached, but a limited interpretation is possible`, then describes an abstract color field. There is no meaningful geological interpretation in that fragment.

Visual comparison used the actual staged crops and an independent render of source page 2. The independently rendered full page was generated only for evaluation, not by an extra extraction or model run. In the first staged crop:

| Visible label | OCR in `figure_p0002_0001.ocr.txt` | Assessment |
| --- | --- | --- |
| Randall Dome | Randall Dome | Correct |
| Cowarna Fault | Fawn Fault | Wrong name |
| Roe Hills, with following label clipped | Boggs Hills F[cut off] | Wrong name, not just clipping |

The full-page reference clearly shows `MT BELCHES`, `ERAYINIA`, `177917`, `177920`, and the legend's `Mount Belches Formation`. These provide useful source anchors, but complete map OCR was not produced. Native text retains some labels while breaking many rotated labels into separate characters. Successful model-operation status therefore does not establish faithful label transcription.

## Retained evidence and exact verification

The main artifacts are:

- XLSX publication: `ROOT/xlsx/documents/result_3b85720c1b994acc8365baa1202bab4f/`.
- CSV publication: `ROOT/csv/documents/result_e53f8a9a99f44448bd75fc8c396c8f76/`.
- Unfinished map: `ROOT/map/.preprocessing/jobs/job_25a4782f6f464854a5ad452b259c0ad8/staging/`.
- Resource summaries, exact command arrays, and raw samples: `ROOT/measurement/{idle,xlsx,csv,map}.result.json`, `.started.json`, and `.samples.jsonl`.
- Artifact comparisons: `ROOT/measurement/verification.json`.
- Read-only operation ledger export: `ROOT/measurement/map.operations.json`.
- Visual references: `ROOT/measurement/map-page2-source-render.png` and `map-page2-label-detail.png`.

Verification decision: direct execution and independent artifact inspection. No new maintained tests were warranted for a disposable evaluation. No application test-suite result is claimed; the parent test run was not observed by this evaluation.

The fixture commands were invoked by `python /tmp/opencode/dundas_dogfood_measure.py /tmp/opencode/dundas-preprocessing-20260930-9f73c2 FIXTURE`, with `FIXTURE=xlsx`, then `csv`, then the idle measurement, then `map`. The wrapper records each actual `uv run preprocess` command. Native child exit codes were 0; the PDF child exit code was 143 with `timed_out: true`. The wrapper itself reports job outcomes in JSON rather than propagating the child's exit status.

The final verification command was `uv run python /tmp/opencode/dundas_dogfood_verify.py`. It exited 0 with `native_artifact_checks: PASS`, `map_snapshot_sha256_matches: true`, and `map_remaining_measured_pids: {}`. This verifies the native artifacts and the reported local cleanup observation. It does not approve the PDF, establish complete map coverage, or validate the unrun manifest fixture.

## Corrected run: execution and resources

The corrected evaluation ran on 2026-09-30, 04:53–04:57 UTC, against source digest `fa11bace77e07dfed923f0cdd5b3aed45a97a7dd6ae385124fdffa6727e48305`. Both jobs used the same pinned executable and `--model opencode/space-bunny-free` flags recorded above. Each had a 300-second external deadline. The map ran first. The manifest started only after the map returned exit 0 with publication ready. No native fixture was rerun, and no application code was changed by this evaluation.

The new root is `/tmp/opencode/dundas-preprocessing-corrected-20260930-f1a6d8`, abbreviated as `NEW_ROOT` in this section. The earlier `ROOT` directory was neither reused nor modified.

| Run | Outcome | Duration | Peak sampled tree PSS | Allocated output disk | Logical output bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| Idle HTTP/MCP service | Deliberate clean shutdown | 6.171 s | 62.34 MiB | 0.051 MiB | 37,206 |
| MtBelches PDF | Exit 0, succeeded, ready | 162.540 s | 534.96 MiB | 28.320 MiB | 29,493,113 |
| Manifest PDF | Exit 0, succeeded, ready | 23.942 s | 506.57 MiB | 25.570 MiB | 26,528,693 |

The idle sample contains `uv` and the service, with no OpenCode process. Service-only PSS was 50,919,424 bytes, or 48.56 MiB. Its shutdown log records `Application shutdown complete.` The map and manifest needed no forced cleanup, and all tracked process identities were absent during verification. Successful ordinary shutdown does not test the guardian's coordinator-loss behavior; no new claim about that failure path is made here.

Resource measurement used the same `/proc` process-tree method as the original evaluation. Median sampling intervals were 125 ms idle, 141 ms map, and 140 ms manifest. The maximum interval was 185 ms. Values are sampled observations from one invocation each, not exclusive-cgroup benchmarks or peak-disk measurements. The retained engine state occupies 27,369,472 allocated bytes for the map and 26,095,616 for the manifest.

| Run | Shared memory.current before | Highest sampled memory.current | memory.current after |
| --- | ---: | ---: | ---: |
| Idle | 21.937 GiB | 21.946 GiB | 21.837 GiB |
| Map | 21.877 GiB | 22.251 GiB | 20.160 GiB |
| Manifest | 20.171 GiB | 20.617 GiB | 20.222 GiB |

The same shared cgroup reported `memory.peak=32,357,679,104` bytes before and after every corrected observation. Its 30.135 GiB historical peak cannot be attributed to these fixtures.

## Corrected detector and call-count comparison

The corrected detector identifies itself as `native-images-caption-page-2`. The saved page-2 detector product records all 19 embedded rectangles as `subsumed-caption-page`. Its sole candidate covers the full page, `[0, 0, 595.276, 841.89]` PDF points. No page-2 image was discarded as tiny. The other three pages have no figure candidates.

| Map observation | Original attempt | Corrected run |
| --- | ---: | ---: |
| Page-2 figure candidates | 20: 19 rectangles plus caption page | 1: caption page includes all 19 rectangles |
| Actual admitted model operations | 25 before timeout | 2 |
| Completed model operations | 23 | 2 |
| Unknown or unfinished operations | 1 outcome_unknown, 1 admitted | 0 |
| Physical-page outcome | No sealed coverage; native text reached pages 1–2 | 4/4 succeeded |
| Publication | None | Ready |

The old candidate count was reconstructed using the original detector rule on the unchanged source: unique valid embedded rectangles plus one caption-page candidate. It was not another extraction run. Twenty old candidates would have implied 40 OCR and description operations if all had been reached; **40 was not the observed call count**. The actual before/after count is 25 admitted operations before timeout versus two completed operations after correction.

The published map image is `figures/figure_p0002_0001.png`, measuring 1191×1684 pixels. It includes the complete map, legend, inset, title, caption, and printed page number 78. It is the figure on physical page 2; the source caption calls it Figure 1. Visual inspection confirms that the image is coherent rather than a collection of fragments.

## Corrected artifact and coverage verification

The verifier reported `ARTIFACT_CHECKS: PASS []`. Source hashes were unchanged before and after both runs and match their retained snapshots and manifests. Sealed and published manifests are byte-identical.

| Check | Map | Manifest |
| --- | ---: | ---: |
| Physical pages independently counted | 4 | 2 |
| Page-inventory records, all succeeded | 4 | 2 |
| Manifest-listed products per generation | 29 | 36 |
| Product hash and size checks across both copies | 58 | 72 |
| Relative navigation links checked | 36 | 47 |
| Broken links or integrity failures | 0 | 0 |

The map's native page-text lengths are 3,811, 1,855, 6,012, and 5,446 characters, respectively. All four page files, evidence files, and detector audits are linked from the inventory. Both manifest page-text files contain 1,596 characters. Native text matches a direct read of each source page using the same PDF library. That check establishes that publication retained the parser output; visual checks below address source fidelity.

Both generations include `AGENTS.md`, `index.md`, `manifest.json`, an evidence inventory, figure guides, and full table downloads. Figure guides link the image, OCR, and separate interpretation. The map index states `total: 4; completed: 4; failed: 0`; the manifest index states `total: 2; completed: 2; failed: 0`. The guides advise citing physical pages and explicitly say that discovery is not necessarily complete.

The corrected PDF table previews now show values as `C1: ...; C2: ...`, with source-page information and an explicit truncation notice. The old native preview observations above describe the retained original outputs only. The revised native preview implementation was not rerun or reapproved in this evaluation.

## Corrected map content quality

The full-page image resolves the fragmentation problem, but the free model's text is not a reliable transcription of every label. These observations compare the actual published image with `figure_p0002_0001.ocr.txt`:

| Visible source content | Corrected OCR | Assessment |
| --- | --- | --- |
| Randall Dome | Randall Dome | Correct |
| Geochronology sites 177917 and 177920 | 177917 and 177920 | Correct |
| Kurnalpi Terrane and Burtville Terrane | Both names retained | Correct |
| Mount Belches Formation in the legend | Mount Belches Formation | Correct |
| Mount Monger Fault | Mount Margaret Fault | Wrong name |
| Ida Fault | Ila Fault | Wrong name |
| Cowarna Fault, Roe Hills Fault, Claypan Fault, and Pinjin Fault | These exact names are absent; OCR includes Crawford Fault and Gibson Fault, which are not visible source labels | Omitted and invented names |
| BULDANIA | BULDNA | Missing letters |
| YARDILLA | YARDILYA | Wrong spelling |
| ERAYINIA | No matching label | Omitted |

This is a targeted label inspection, not an overall OCR accuracy percentage. The OCR preserves much more coherent content than the failed fragment run, including the legend and both site numbers, but success status does not establish label accuracy.

The interpretation contains two consequential factual errors. Its first sentence describes the `Proterozoic Mount Belches Formation northeast of Kalgoorlie`. The map places Mount Belches Formation under **ARCHEAN: YILGARN CRATON** in the legend. Its mapped area lies southeast of Kalgoorlie–Boulder on the latitude/longitude-labelled map. These errors remain in the published interpretation despite figure outcome and OCR status both being `succeeded`. The map manifest contains no findings about these accuracy problems.

## Manifest content and remaining omissions

The source is a transport manifest dated 15 February 2024, not a drilling-results table. Its two physical pages have identical native text and identical independently rendered images. The two page records are therefore faithful to duplicate source pages, not an accidental duplication introduced by extraction.

Native page text retains these visible values: manifest ID `MYT33870732`, payer `59362`, connote `2637893680`, receiver `nagrom`, suburb `KELMSCOTT`, postcode `6111`, reference `12294990`, mass `300.0` kg, and volume `1.44` m3.

However, **the main shipment row is missing from typed tables**. The six detected tables contain two sets of headers, totals, and pallet counts. None contains cells for `59362`, `2637893680`, `nagrom`, `KELMSCOTT`, `6111`, or `12294990`. Tables 2 and 5 correctly retain the totals row, `TOTALS: 1,1,300.0,1.44,`. This is a meaningful table-discovery omission, even though page coverage is complete. The native text remains the usable source for the missing shipment details.

Each page contributes one QR-code crop. Its OCR says `[no visible text]`, which agrees with visual inspection. The separate interpretation identifies a QR-like barcode without claiming to decode it. Each page also has a 141×13.5-point linear barcode excluded by the detector's 16-point minimum dimension. Both exclusions are explicitly recorded as `FIGURE_DETECTOR_EXCLUSIONS` warnings. The printed number beneath that barcode is present in native text. The QR and linear barcodes were not decoded by this evaluation.

## Operation correlation and usage

All six corrected operations completed on their first recorded attempt, with non-overlapping ledger intervals. Every operation requested `opencode/space-bunny-free`. `returnedModel` is null for all six. Every session usage report lists USD 0; both jobs still mark `usageStatus` as `incomplete`. These are provider-reported observations, not a complete billing assertion.

Map job `job_daac1c40a7854a5c8c85b0946922fcc7`, generation `ae8c002571024629849cbe03c45d974b`, has two operations, both for physical page 2 and `figure_p0002_0001`:

| Operation ID | Kind | Session ID | Duration | Reported total tokens |
| --- | --- | --- | ---: | ---: |
| `operation_cfadb95d639d42b583df2fab86c0c704` | figure-ocr | `ses_f0f562565ffe82wBXT7vcYszmQ` | 143.688 s | 19,861 |
| `operation_d757e1f0410b4d3f84e4eca3b0c64c06` | figure-description | `ses_f0f53fdffffeJsCPY2dRY54zYc` | 16.587 s | 4,248 |

The map's reported prompt-usage sums are 6,295 input tokens, 276 cached-read tokens, 650 output tokens, and 16,888 thought tokens: 24,109 total. The long OCR operation spent most of its reported tokens on reasoning, despite the two-call reduction.

Manifest job `job_64167d9fa4b2484aaaa9244b0c71a7d5`, generation `c9e70beb9bee43bd82c03ba810428698`, has these operations:

| Operation ID | Physical page and kind | Session ID | Duration | Reported total tokens |
| --- | --- | --- | ---: | ---: |
| `operation_86810f335be04f57a26c3b59f5743f88` | 1, figure-ocr | `ses_f0f537cc6ffeX2Rk21S60V4C8k` | 5.673 s | 845 |
| `operation_17a2b6483b5d48948bb50fde7078043c` | 1, figure-description | `ses_f0f536dd6ffemQ4jcrFfpFazff` | 6.865 s | 929 |
| `operation_3bc42990c5cc446c8efdc9d488a24412` | 2, figure-ocr | `ses_f0f535292ffesNUVdnykWjq9yf` | 3.206 s | 910 |
| `operation_e4d2ae32a89a43528775c833a278becd` | 2, figure-description | `ses_f0f5345fdffeBoF6qkDf9mT1DO` | 6.658 s | 1,229 |

The manifest's reported prompt-usage sums are 1,065 input tokens, 1,853 cached-read tokens, 168 output tokens, and 827 thought tokens: 3,913 total. The figure IDs are `figure_p0001_0001` and `figure_p0002_0001` for pages 1 and 2. Operation usage was matched by operation ID against the job's usage observations; all six associations matched exactly.

Complete ledgers, including source bounds, requested model, returned-model field, attempts, timestamps, and session usage, are retained in `NEW_ROOT/measurement/map.operations.json` and `manifest.operations.json`.

## Corrected result paths for independent navigation

The independent navigation entry points are:

```text
/tmp/opencode/dundas-preprocessing-corrected-20260930-f1a6d8/map/documents/result_1baa4b622fc047b984111a0425d36f81/AGENTS.md
/tmp/opencode/dundas-preprocessing-corrected-20260930-f1a6d8/manifest/documents/result_ba3542ead1334812b0839dd04645c607/AGENTS.md
```

Each guide sits beside `index.md` and `manifest.json`. The map's figure-navigation entry is `indexes/figure_000001.md`. The manifest has `indexes/figure_000001.md` and `indexes/figure_000002.md`. Both output roots also expose a shared catalog at `documents/index.md`.

The sealed manifests are:

```text
/tmp/opencode/dundas-preprocessing-corrected-20260930-f1a6d8/map/.preprocessing/jobs/job_daac1c40a7854a5c8c85b0946922fcc7/sealed/manifest.json
/tmp/opencode/dundas-preprocessing-corrected-20260930-f1a6d8/manifest/.preprocessing/jobs/job_64167d9fa4b2484aaaa9244b0c71a7d5/sealed/manifest.json
```

Resource JSON, stdout, stderr, and raw samples are under `NEW_ROOT/measurement/`, using the `idle`, `map`, and `manifest` prefixes. `verification.json` contains product checks and correlation evidence. `content-review.json` records candidate counts, selected missing table values, duplicate-source checks, and per-operation usage. The manifest source renders and excluded-barcode context are in that same directory. Existing native result paths remain as recorded in the original section.

Verification decision: direct fixture execution, artifact checks, and visual content review. Exact commands and outcomes:

- `python /tmp/opencode/dundas_dogfood_rerun.py map`: child exit 0, `timed_out: false`, succeeded, ready.
- `python /tmp/opencode/dundas_dogfood_rerun.py manifest`: child exit 0, `timed_out: false`, succeeded, ready.
- `uv run python /tmp/opencode/dundas_dogfood_rerun_verify.py`: exit 0, `ARTIFACT_CHECKS: PASS []`, 130 product checks and 83 links across the two jobs.
- `uv run python /tmp/opencode/dundas_dogfood_content_review.py`: exit 0; page-2 candidates 20→1, actual map operations 25→2, and 19 `subsumed-caption-page` dispositions. Its value checks also expose the shipment-row omissions described above.

No maintained tests, native fixture reruns, application edits, or additional model calls beyond the two requested PDF invocations were needed for this evaluation. Passing execution and integrity checks do not override the documented OCR, interpretation, and table-discovery defects.
