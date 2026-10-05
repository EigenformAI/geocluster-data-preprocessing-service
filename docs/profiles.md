# Extraction profile reference

`--profile PATH` supplies credential-free JSON overrides to `process` or `serve`.
Profiles supply administrator defaults. PDF submissions can override only
`tableRecovery` extraction fields. Omitted fields merge with defaults;
`--model provider/model` overrides all model roles.

```json
{
	"id": "small-documents",
	"limits": {"pages": 100, "rows": 100000},
	"options": {"figureDescriptions": false},
	"models": {"vision": "deployment/vision"}
}
```

## Fields

Accepted top-level fields are `id`, `limits`, `options`, `models`, and `retry`.
Unknown fields, duplicate keys, and credential or environment fields are rejected.
The file may contain at most 65,536 bytes. The ID accepts 1–128 ASCII letters,
digits, dots, underscores, or hyphens; its default is `default`.

The current model role is `vision`. Model values use `provider/model` identifiers,
optionally followed by `#variant` for a variant configured on the engine.
Every role must be explicitly selected through `models` or `--model`.
There is no automatic model fallback.

## Limits

Limits are enforced positive integers up to Python's `sys.maxsize`.
Administrators can raise or lower them. `indexBytes` must be at least 768 for
linked navigation; individual navigation entries can require a larger budget.

| Limit | Default | Scope |
| --- | ---: | --- |
| `inputBytes` | 268,435,456 | Source snapshot bytes. |
| `outputBytes` | 536,870,912 | Accepted products and staging output. |
| `expandedBytes` | 536,870,912 | Sum of workbook ZIP member sizes. |
| `pages` | 1,000 | Physical PDF pages. |
| `rows` | 2,000,000 | Rows per document. |
| `columns` | 16,384 | Columns in a table row. |
| `pixels` | 16,000,000 | Pixels per PDF render and total image pixels per model operation. |
| `queueDepth` | 100 | Nonterminal jobs admitted by the owner. |
| `requestBytes` | 65,536 | HTTP and MCP request body. |
| `responseBytes` | 262,144 | Typed-row response and accumulated model text; not full downloads or manifests. |
| `textBytes` | 32,768 | Text-read chunk and model task text. |
| `indexBytes` | 32,768 | Navigation and MCP result summary. |
| `jobSeconds` | 3,600 | Processing timeout and worker CPU soft limit. |
| `modelSeconds` | 180 | Model completion window. PDF operations also receive a coordinator-owned window that includes preparation; see [model completion windows](engine.md#model-completion-windows). |
| `workerMemoryBytes` | 2,147,483,648 | Parser address-space limit, excluding the owner and model engine. |
| `orphanGraceSeconds` | 86,400 | Startup grace before removing unpublished outputs of fenced terminal jobs. |
| `modelImages` | 4 | Images per model operation. |
| `modelImageBytes` | 8,388,608 | Total image-file bytes before base64 per model operation. |

`modelImages` and `modelImageBytes` use engine defaults when omitted.
Admission requires free disk space of at least `inputBytes + 2 × outputBytes`
(1,342,177,280 bytes by default). This does not reserve space or cap retained
storage. Referenced snapshots and published results are retained.

The worker CPU hard limit is `jobSeconds + 1`. Local I/O and engine settlement
complete before ownership is released. The engine adapter separately applies
a 16 MiB HTTP request/response limit, a 30-second request timeout, and a
10-second cleanup-attempt timeout. An in-flight preparation request can outlast
the PDF work window. Cleanup still requires settlement. Cancellation can wait
up to 131 seconds with the default engine settings. Remote CLI responses have
a 16 MiB limit and a 30-second network timeout.

The standalone engine owner's recovery evidence has a separate serialized-byte
limit; see [standalone supervision](engine.md#standalone-supervision).

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `encoding` | `utf-8-sig` | Delimited-text encoding. |
| `delimiter` | Comma for CSV, tab for TSV | One character, excluding newline, carriage return, and NUL. |
| `figureDescriptions` | `true` | Include figure interpretation. Disabling it does not disable OCR. |
| `figureDetector.minDimensionPoints` | 16 | Minimum image dimension; zero disables size filtering. |
| `figureDetector.groupOverlaps` | `true` | Group overlapping images before size filtering. |
| `figureDetector.captionPage` | `true` | Use whole-page evidence for Figure/Fig. captions, subsuming embedded fragments. |

Detection evidence records excluded images. Caption-page bounds are not precise
figure segmentation.

## PDF table recovery

`options.tableRecovery` supplies the PDF policy. The submission-level
`tableRecovery` object overrides only these fields. Unspecified fields inherit
the configured profile. `forcePages` replaces the list rather than extending it.
The job and manifest retain the resolved profile and its `profileDigest`.

| Field | Default | Values and scope |
| --- | --- | --- |
| `geometry` | `auto` | `auto`, `force`, or `off`. Controls native geometry recovery and alternate table proposals. |
| `vision` | `confirm-all` | `confirm-all`, `on-demand`, or `off`. Controls table scouting and independent image reads. |
| `forcePages` | `[]` | Positive, one-based physical PDF page numbers. Sorted and deduplicated; pages beyond the source page count fail during processing. |
| `renderDpi` | 300 | Positive finite number. Target DPI for table-region reads, not ordinary page or figure OCR. |
| `maxAdditionalPasses` | 1 | Nonnegative integer. Optional dispute rereads per table occurrence, cumulative across compatible resumes. |
| `maxModelCalls` | `null` | Positive integer or `null`. Cap on table scout, extraction, confirmation, and reread calls in this job; `null` sets no table-call cap. |
| `workSeconds` | `null`, resolved to 2,700 | Positive finite number below `jobSeconds`, or `null` to resolve to `0.75 × jobSeconds`. Soft PDF work window. |

`geometry: auto` uses native cell rectangles, word occurrences, and local text
baselines to recover supported row structure. It retains uncertain alternatives
as unresolved evidence. `force` also requests alternate geometry and, when table
vision is enabled, scouting on every page. `off` skips geometry recovery,
alternate native extraction, and initial text-alignment discovery. Native table
extraction remains enabled. Enabled scouting can still refine proposals.

`vision: confirm-all` requires an independent image read for every discovered
table occurrence. A table with no usable initial rows needs image extraction
before confirmation. `on-demand` requires confirmation for suspect geometry,
forced pages, and scout-only proposals. Both policies can scout missing bounds,
overlapping regions, unclaimed images, or ruling with no table proposal.
`off` disables table scouting and dedicated table reads. It does not disable
ordinary page OCR, tables transcribed by page OCR, figure OCR, or figure
interpretation. `figureDescriptions: false` separately disables interpretation.

`forcePages` requests recovery on selected pages under the enabled policies.
It does not enable an `off` policy or bypass resource limits. On resume, only
pages explicitly supplied in the new submission invalidate reusable work.
An inherited force list does not invalidate completed units.

### Budgets and partial results

Required table reads run during page processing. Optional dispute rereads run
after the page scan so they do not consume later pages' initial-read allowance.
One pass can require several model calls when a table is tiled. Setting
`maxAdditionalPasses: 0` disables optional rereads, not required confirmation.
The table-call cap excludes ordinary page OCR, figure OCR, and interpretation.
It counts calls attempted by table recovery, not provider requests or charges.

Additional-pass spending survives resume. If a table has spent one pass, a
resume with `maxAdditionalPasses: 1` grants no new pass. Raising the total to
`2` grants one more. An unfinished active pass can continue using its retained
tile reads. Each new job has a fresh work window and table-call counter.

The soft work deadline starts when the job runs, before snapshot acquisition
and resume copies. It bounds PDF extraction, including ordinary OCR and
figure stages. The coordinator declines model dispatch when there is too little
time for preparation and settlement. At cutoff, the adapter marks unfinished
units pending and can seal usable evidence as a partial result. The remaining
hard job budget covers settlement and sealing. Unknown model ownership prevents
sealing until settlement. The hard `jobSeconds` limit can still terminate
processing. Non-PDF extraction does not use this soft window.

`renderDpi` is retained for table crops. Large crops use overlapping tiles
within `pixels`; impossible complete-row renders fail with `RESOURCE_LIMIT`.
Rows, columns, output bytes, model input limits, and worker memory remain strict
limits. Optional rereads can be deferred for evidence headroom. Visual grid
allocation is capped at `workerMemoryBytes // 4096` positions to reserve parser
and render memory. Raising the relevant profile limits changes these budgets.

### CLI overrides

`process`, `dev process`, and `resume` accept these submission overrides:

| Flag | Values |
| --- | --- |
| `--table-geometry` | `auto`, `force`, `off` |
| `--table-vision` | `confirm-all`, `on-demand`, `off` |
| `--table-force-pages` | Comma-separated one-based pages; `""` replaces the profile list with `[]`. |

```sh
uv run preprocess process /path/to/report.pdf --output /path/to/data \
	--engine-descriptor /private/engine.json --model deployment/vision \
	--table-geometry auto --table-vision on-demand --table-force-pages 2,5
```

`--table-geometry force` requests recovery on every page. `forcePages` with
`geometry: auto` selects individual pages. For native table extraction without
geometry recovery or dedicated table vision, use
`--table-geometry off --table-vision off`. Ordinary OCR remains separate.

`serve` takes administrator defaults through `--profile`. The other table fields
are configured through profiles or HTTP and MCP submissions, not dedicated CLI
flags. A valid table override on a non-PDF source is admitted, then fails during
processing with `UNSUPPORTED_OPTION`. Profile defaults alone do not cause this
error. See [submission overrides](api.md#table-recovery-overrides) and
[resume extraction](api.md#resume-extraction).

## Retry fields

`retry.attempts` defaults to 24 and `retry.noProgress` to 8.
`retry.backoffSeconds` accepts a nonnegative finite number. The CLI validates
these fields, but the HTTP adapter makes one prompt attempt and does not use
retry counts or backoff.
`retryAmbiguous` must be false. An intentional repeat requires a new job.
One client prompt can result in multiple provider requests.
