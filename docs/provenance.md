# Source provenance

The service draws extraction ideas from
[`Lexikat-Pte-Ltd/geo-report-pipeline`](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline)
at commit `2c8a05864ce26b49ec88a78bfd0772e97e949958`, inspected on 2026-09-29.
These links record algorithm lineage, not byte-for-byte imports or current
service instructions.

| Upstream source | Contribution |
| --- | --- |
| [Native PDF extraction](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/digital_ocr.py#L261-L352) | Native table extraction and caption-page figure discovery. |
| [Figure descriptions](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/describe_figures.py#L58-L137) | Separation of visible-text transcription from interpretation. |
| [Manifest](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/manifest.py#L10-L78) | Inventory and review findings. |

The pinned upstream baseline has no project license file or package license
declaration. This provenance record does not establish redistribution rights.

## PDF table evidence

The service adds occurrence-based geometry recovery, independent image reads,
comparison decisions, and resumable work plans to native extraction. These
products distinguish the original readings from the selected table view.
Paths below are relative to a sealed or published result generation.

| Product | Evidence |
| --- | --- |
| `text/page_0001.json` | Page word occurrences with IDs, text, and PDF-point boxes. Repeated text has distinct occurrence IDs. |
| `tables/evidence/page_0001.candidates.jsonl` | Original native and OCR rows, geometry alternatives, visual reads, and tile aggregates. Each candidate records its method, rows, logical grid, cells, spans, and available word assignments. |
| `tables/evidence/page_0001.discovery.json` | Physical-region groups, image dispositions, and discovery triggers. Scout images and raw responses are listed separately when scouting runs. |
| `tables/<table-id>.jsonl` | Canonical selected rows and typed cells. Headers remain rows. |
| `tables/<table-id>.csv` | Convenience export of the selected view, not the complete evidence record. |
| `tables/evidence/<table-id>.structure.json` | `selectedCandidateId`, `selectedRows`, the logical `grid`, cell anchors and spans, and available word and tile-row mappings. |
| `tables/evidence/<table-id>.decisions.json` | Candidate comparisons, differences, normalizations, recovery and confirmation summaries, `passesSpent`, and image and response paths. |
| `work-plan.json` | Semantic fingerprint, work units, dependencies, states, and evidence paths used to validate resume. |

Each table manifest entry links the three main evidence products through
`evidencePaths.candidates`, `evidencePaths.structure`, and `evidencePaths.decisions`.
`selectedCandidateId` identifies the candidate behind canonical rows. Alternate
candidates are not exported as extra canonical tables for the same occurrence.
Geometry recovery uses word positions and native cells, not newline splitting
or a set of numbers found elsewhere on the page.

### Cells, spans, and coordinates

Native and page-OCR candidates retain original row arrays in `rows`.
`logicalRows` represents anchors in the logical grid. For native candidates
with spans, the canonical export uses the logical view. Original rows remain in
candidate evidence. A cell records one-based `row` and `column`, `rowSpan`,
`columnSpan`, `text`, `bbox`, `wordIds`, and `unreadableReason`.

Grid states are `text`, `empty`, `unreadable`, `covered`, and `omitted`.
`covered` points to a span's anchor. Logical rows use `null` for covered,
omitted, or unavailable positions. The grid distinguishes those meanings.
An empty cell is `""`. Missing readings do not become empty strings.
Uncertainty markers remain in text rather than becoming guessed values.

PDF bounds use points with a top-left origin in the rotated MediaBox coordinate
space. Table-region images retain requested and actual crop bounds, DPI,
orientation, and `pageToImage` and `imageToPage` transforms. Corrected quarter-turn images
also retain their unrotated source crop. Large tables use overlapping tiles at
the requested DPI. Tile aggregation matches physical row occurrences, not
equal row text. Clipped fragments cannot supply completed row values, and
ambiguous seams remain unresolved with their original reads retained.

### Recovery and confirmation states

Recovery and confirmation are separate summaries:

| Field | State | Meaning |
| --- | --- | --- |
| `recovery.state` | `unchanged` | The geometry step retains the original structure. This includes disabled geometry recovery. |
| `recovery.state` | `recovered` | Geometry or independent visual agreement supports a corrected structure, including a quarter-turn logical grid. `recovery.reason` records the cause. |
| `recovery.state` | `unresolved` | Geometry evidence leaves uncertain structure or assignments. A usable selected view can still be exported. |
| `confirmation.state` | `agreed` | Complete readings agree under the recorded comparison rules. |
| `confirmation.state` | `unresolved` | Readings disagree, structure is uncertain, or tile seams remain unresolved. |
| `confirmation.state` | `failed` | Required confirmation work fails. Available original evidence is retained. |
| `confirmation.state` | `pending` | Required work is unfinished, for example after a work cutoff or table-call cap. |
| `confirmation.state` | `not_checked` | The resolved vision policy does not require confirmation for this occurrence. |

Visual recovery can record `visual-structure-correction` when independent visual
reads agree, or `visually-supported-alignment` when vision supports a weak
alignment proposal. The state alone does not identify how the structure changed.

An agreed table records `confirmation.basis` as `native-vision`, `ocr-vision`,
or `repeat-vision`. Other confirmation states have a null basis. Comparisons
separately record `positioned-grid` when both readings have row-band evidence,
or `ordered-grid` otherwise. Counts, cell positions, spans, and available
boundaries establish correspondence before cell text is compared. Prose can
normalize whitespace. Text containing a Unicode numeric character permits
outer trim only, preserving internal spelling, signs, precision, and spacing.
The comparison records every applied normalization.

Generated navigation shows recovery state, confirmation state and basis, the
selected candidate, and links to original rows, images, structure, and decisions.
HTTP manifests and MCP table summaries expose the same summaries and evidence
paths. `agreed` means consistency between readings, not proof of source accuracy.
`recovered` does not mean confirmed. Discovery counts do not establish complete
recall. The [profile reference](profiles.md#pdf-table-recovery) defines policies
and budgets. The [API reference](api.md#resume-extraction) defines resume rules.
