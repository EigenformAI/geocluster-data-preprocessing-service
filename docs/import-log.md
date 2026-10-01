# Import log

This log records the source baseline and helper provenance for the preprocessing
service. Hashes identify the inspected source material, not generated results or
the current service implementation. No source datasets are part of this import.

## Upstream baseline

- Repository: [`Lexikat-Pte-Ltd/geo-report-pipeline`](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline).
- Pinned commit: `2c8a05864ce26b49ec88a78bfd0772e97e949958`.
- Baseline inspection: 2026-09-29.
- Service import log: 2026-09-30.

The service package is `preprocessing`. On 2026-10-01, the legacy `pipeline/`
and `analysis/` trees were retired, along with their report-analysis skills,
provider probes, PEACE bootstrap scripts, legacy test, and usage guides.
The pinned upstream links below preserve provenance. They describe historical
source, not executable commands or local paths required by the service.

## Extraction lineage

| Pinned upstream source | Disposition in the service |
| --- | --- |
| [`pipeline/digital_ocr.py`, lines 261–352](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/digital_ocr.py#L261-L352) | Native PDF table extraction and caption-page figure ideas inform `preprocessing/formats/pdf.py`. Report-specific cleaning, merged-header reconstruction, and existence-based caches are not the service contract. |
| [`pipeline/ground_truth.py`, lines 32–58](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/ground_truth.py#L32-L58) | Historical word-box adapter used `pdftotext -bbox`. Current PDF evidence uses physical-page `pdfplumber` word boxes; this adapter is not called. |
| [`pipeline/ocr.py`, lines 19–136](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/ocr.py#L19-L136) | Historical digital/Mistral OCR selection is retired. The service requests model operations through the coordinator and the OpenCode HTTP adapter. |
| [`pipeline/verify_tables.py`, lines 87–172](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/verify_tables.py#L87-L172) | Historical numeric-membership checks are not cell-placement or complete-recall guarantees. Geological assessment is outside the service. |
| [`pipeline/describe_figures.py`, lines 58–137](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/describe_figures.py#L58-L137) | Figure interpretation remains distinct from visible-text OCR. The service records failed coverage instead of treating omitted descriptions as completed work. |
| [`pipeline/opencode_client.py`, lines 19–90](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/opencode_client.py#L19-L90) | The historical v1-style HTTP client is retired. `preprocessing/engine.py` supplies `OpenCodeServerEngine` for the pinned 2.0.19 native HTTP API. |
| [`pipeline/manifest.py`, lines 10–78](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/manifest.py#L10-L78) | Inventory and review-queue concepts informed explicit findings. Current results add source/profile provenance, hashed products, coverage, sealed generations, and agent navigation. |
| [`pipeline/run.py`, lines 44–122](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/pipeline/run.py#L44-L122) | Replaced in the service path by one job owner shared by CLI, HTTP, and MCP. Historical filename/existence caches are not replay or billing guarantees. |
| [`analysis/run.py`, lines 88–226](https://github.com/Lexikat-Pte-Ltd/geo-report-pipeline/blob/2c8a05864ce26b49ec88a78bfd0772e97e949958/analysis/run.py#L88-L226) | HIE, metrics, flags, DKI, PEQA, and assessment reports are retired from this service. Geological assessment is outside its maintained extraction contract. |

These entries describe algorithm and interface lineage. They do not claim
byte-for-byte imports of the upstream modules into `preprocessing`.

## Local helper provenance

The following helpers were untracked prototypes at inspection time. They have no
pinned upstream URL. Their names and SHA-256 values identify the inspected
material; the helpers are not packaged service capabilities.

| Helper | Inspected behavior | Service treatment |
| --- | --- | --- |
| `scripts/dundas_documents.py` | Delimited readers, workbook formulas and caches, hidden sheets, and an XLS reader. Relevant ranges: 69–136, 303–323, 510–565, 623–636. | Native readers preserve typed evidence rather than the prototype's stringification and date conversion. XLS support was not imported. |
| `scripts/dundas_kb.py` | Recursive indexes, provenance chunks, table previews, full-data links, and guides. Relevant ranges: 294–458, 615–688, 844–923. | Navigation ideas inform bounded indexes and `AGENTS.md`. Dataset taxonomy, dataset paths, and multi-file replacement publication were not adopted. |
| `scripts/dundas_vision.py` | One supervised engine per run, image calls, session cleanup, and response checks. Relevant ranges: 203–254, 353–441, 1237–1376. | Engine lifetime and validation ideas inform the adapter. The dataset supervisor, model allowlist, and v1 client substitution were not imported wholesale. |

Observed SHA-256 values:

```text
dundas_documents.py  7ed8c86871017d2c4d8a96016a0010ab3d36983722518361848ace2ad63c570b
dundas_kb.py         479c8a8c27d144d3515c9681fdf2ca0c308b258be9aa1e2101004ff14511e40d
dundas_vision.py     16ed7462a95bb05f9f531746dfd630988eda8af26a7708d1271773087960507f
```

These are inspection-time hashes. The service does not depend on finding these
prototype files at runtime. Tests generate synthetic inputs rather than importing
their source datasets.

## Packaging boundary

`pyproject.toml` declares `packages = ["preprocessing"]` for the wheel and
`preprocess = "preprocessing.cli:main"` for the command. Assessment, map analysis,
dataset supervisors, environment bootstrap, and historical provider runners are
outside that package. The upstream baseline had no project license file or
package license declaration; this log does not establish redistribution rights.
