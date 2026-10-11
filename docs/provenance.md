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
