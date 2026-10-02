# Extraction profile reference

`--profile PATH` supplies credential-free JSON overrides to `process` or `serve`.
Profiles are administrator configuration, not per-request overrides. Omitted
fields merge with defaults; `--model provider/model` overrides all model roles.

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
| `modelSeconds` | 180 | Completion-polling window after prompt submission, not a whole-attempt deadline. |
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
10-second cleanup-attempt timeout. An in-flight request can outlast the
completion-polling window. Cancellation can wait up to 131 seconds with the
default engine settings. Remote CLI responses have a 16 MiB limit and a
30-second network timeout.

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

## Retry fields

`retry.attempts` defaults to 24 and `retry.noProgress` to 8.
`retry.backoffSeconds` accepts a nonnegative finite number. The CLI validates
these fields, but the HTTP adapter makes one prompt attempt and does not use
retry counts or backoff.
`retryAmbiguous` must be false. An intentional repeat requires a new job.
One client prompt can result in multiple provider requests.
