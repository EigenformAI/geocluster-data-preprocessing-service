# Detecting unreliable PDF extraction and OCR

Research date: 2026-10-01. This note distinguishes published methods, metadata standards, and proposed engineering heuristics. Research used public documentation and papers. No report content or private repository content was submitted to external research services.

## Findings that determine the design

1. **A completed OCR call is not evidence of a complete transcription.** Missing regions, reading-order errors, table omissions, and hallucinations require separate assessment. OmniDocBench evaluates these dimensions separately [8].
2. **Short output is a useful trigger, not a failure verdict.** A map, photograph, title page, or blank page can legitimately contain little text. Detection must consider visible text rather than total image area. Independent text detection can locate regions without recognizing their characters [10].
3. **Reference-based metrics measure correctness offline.** Character error rate (CER) and word error rate (WER) compare output with a reference transcription. They cannot establish production-page correctness when that reference does not exist [1].
4. **Confidence and geometry help, but neither guarantees completeness.** Confidence describes recognized items, including potentially incorrect ones. It says nothing about text the recognizer never detected. Metadata schemas define score ranges but do not establish that scores are calibrated correctness probabilities [2–4].
5. **Image-grounded vision checking is a published approach, with substantial limitations.** OCR-Quality supplies a page-image-plus-output judging prompt and human quality labels. Its reported judge results and dataset limitations argue against treating a judge as an oracle [7].
6. **Cross-model disagreement is promising evidence.** Consensus Entropy evaluates agreement among multiple vision-language models and routes difficult cases to a stronger model. It supports selective escalation, but shared omissions can produce agreement on the wrong answer [6].
7. **No reviewed source defines a universal minimum text length, coverage threshold, or agent score that certifies OCR correctness.** Thresholds are local policy, tuned against representative labels.

## Methods and their limits

| Method | Evidence it provides | Important limitation | Use in this service |
|---|---|---|---|
| CER, WER, edit distance | Reference-based recognition errors, including deletions and insertions | Needs ground truth; page serialization can confound reading order with recognition | Evaluate recovery and remaining error on a labeled corpus |
| Text-region or line detection | Text-like regions exist even when the transcript is empty | Detector can miss text or confuse diagram marks with text | Stronger second-phase completeness witness |
| Output length and table inventory | Empty output, tiny prose, missing structured tables | Sparse pages and tables need separate treatment; long output can still omit a column | Cheap first-phase routing signals |
| Word boxes and confidence | Locations and scores for recognized words | Missing words have no score; engine scales are not interchangeable | Optional evidence if a conventional OCR backend is added |
| Image-quality checks | Downsampling, skew, poor contrast, orientation, small glyphs | Predicts difficulty, not exact transcription accuracy | Choose a changed render or orientation recovery |
| Image-grounded vision judge | Observable mismatch between page image and supplied extraction | Subjective judgments, shared model weaknesses, unreadable inputs | Bounded diagnosis, with explicit abstention |
| Cross-engine disagreement | Alternative transcription differs in content or structure | Shared errors remain invisible; formatting differences inflate disagreement | Targeted alternate-model recovery, not full ensembles initially |
| Sampling and human adjudication | False negatives on otherwise unflagged pages; trustworthy local labels | Requires continuing review effort and representative sampling | Calibrate routing and judge, and audit a sample of unflagged pages |

These methods complement one another. For example, an OCR engine may assign high confidence to the footer it recognized while omitting every body paragraph.

## Reference-based quality measures

OCR-D defines the conventional character error rate as:

```text
CER = (insertions + substitutions + deletions) / reference_character_count
```

The value can exceed 100% when insertions are numerous. OCR-D's benchmark uses a normalized variant:

```text
normalized_CER = (i + s + d) / (i + s + d + correct)
```

State the denominator and normalization when reporting a metric. OCR-D also separates page-level results because document averages can conceal difficult pages. Its bag-of-words discussion and flexible-character-accuracy discussion explain why incorrect reading order can inflate whole-page edit distance even when individual words are correct [1].

For technical reports, complement CER with content-presence checks, critical numeric-string accuracy, table-cell correspondence, and reading order. A misplaced decimal can matter more than several punctuation errors. Preserve original numeric strings during extraction. Evaluation normalization must not silently turn `001` into `1` or `1.2300` into `1.23`.

## Metadata standards preserve evidence, not assurance

### ALTO

The Library of Congress ALTO schema represents page layout, text blocks, text lines, strings, coordinates, and processing provenance. Its documented string scores include:

- `WC`: word confidence from 0, unsure, to 1, confident.
- `CC`: character scores from 0, confident, to 9, unsure. The direction differs from `WC`.

Missing scores mean no score was emitted. They do not mean high confidence. ALTO is useful for interchange and auditability; its XML structure does not certify correct recognition [2].

### PAGE XML

PAGE represents region types, polygons, reading order, and text alternatives. OCR-D's PAGE conventions describe `TextEquiv/@conf` in the range 0–1 and indexed alternatives. These features can retain multiple OCR outputs with geometry [3].

Storing alternatives does not standardize a disagreement metric. Two alternatives can agree because both omitted the same region. A small JSON evidence report is sufficient for the initial service update. ALTO or PAGE export becomes worthwhile when interoperability is a concrete requirement.

### Tesseract TSV and hOCR

Tesseract's official TSV examples contain `conf=-1` on page, block, paragraph, and line rows. Word-level rows carry actual scores. Aggregate applicable word rows and exclude sentinel values. hOCR contains word boxes and `x_wconf` [4].

The service currently uses a generative vision model for OCR, so these engine-specific word scores are not available. A model's self-reported confidence must not be presented as equivalent to Tesseract word confidence.

## Input quality and existing text-layer policy

Tesseract's quality guide discusses resolution, thresholding, skew, borders, alpha, and page-segmentation mode. Its approximately 300 DPI guidance is specific OCR advice, not a general rule for every vision model [4]. Increasing render resolution does not restore information lost in a low-resolution source scan. Some model serving stacks resize images internally.

OCRmyPDF distinguishes preserving pages with existing text, redoing an OCR layer, and forcing rasterization. A page with a thin or incorrect existing text layer can escape OCR under a text-presence-based skip policy. These modes are processing choices, not output-quality metrics [5].

FADGI's May 2023 third edition retains its four-star system. Section 2.1 nominally maps four, three, and two stars to ISO 19264 Levels A, B, and C. It does **not** replace star ratings with ISO levels. Section 6.7 states that OCR settings require testing because software and source materials vary [9]. ISO 19264-1:2021 concerns imaging-system quality for reflective originals. Neither establishes that a particular OCR output captured all visible text. The full ISO standard was not reviewed.

## Vision judges and model agreement

### OCR-Quality: direct image-and-output checking

OCR-Quality reports 1,000 page images rendered at 300 DPI, one OCR system's outputs, and human labels from excellent to poor. Its rubric explicitly includes missing paragraphs, missed content, wrong language, and structural collapse. Its judge receives the image and prediction and returns a 0–1 quality rating [7, §§2.3, 5.2].

Its reported overall F1 values are 40.0 for its GPT-4o judge and 48.0 for its Consensus Entropy baseline. These are paper-specific results, not expected performance for this service. The report acknowledges single-annotator labels, one source OCR model, limited language coverage, and a small dataset. Its reported table does not fully explain how all score bands form the overall number. The dataset license is unspecified in the reviewed report [7, §§6.3, 8–9].

**Practical conclusion:** borrow the image-grounded rubric, then validate locally. Prefer observable issue descriptions and page regions to an unexplained numeric score. A checker needs `indeterminate` when the page image is unreadable. A label such as `no_issue_found` describes an inspection result without claiming verified correctness.

### Consensus Entropy: selective escalation

Consensus Entropy estimates uncertainty from similarities among multiple model outputs. CE-OCR combines model agreement with routing to a stronger model. The reviewed v3 paper reports a 15.2-point F1 improvement, expressed as a 42.1% relative gain, over its judge baseline in that evaluation. It also reports routing 7.3% of inputs in one configuration [6]. Those results do not prescribe a local threshold or predict technical-report recovery rates.

**Practical conclusion:** test a diverse fallback model on suspicious pages before paying for multiple OCR passes on every page. Compare literal content and table structure rather than semantic embeddings alone. Similar meaning can conceal a changed grade, coordinate, identifier, unit, or decimal. Agreement is supporting evidence, not proof. Do not adopt the paper's generative “rephrasing” step for faithful report transcription.

## Benchmarks and reference implementations

### OmniDocBench

OmniDocBench evaluates text, tables, formulas, and reading order rather than only a document-wide text score. Its paper discusses missed content, complex layouts, rotation, and hallucinated information. The repository and paper have changed across releases. Pin the dataset, normalization, matching code, and metric versions before comparing results [8]. Avoid combining historical paper counts with the current repository's dataset count.

### olmOCR-Bench and olmOCR 2

Ai2's benchmark checks text presence, text absence, natural reading order, table relationships, math, and baseline robustness such as repeated n-grams. These small, machine-checkable facts are useful for building report-specific regression fixtures [11]. Presence and table checks require known reference facts; they are not automatically available for arbitrary production pages.

olmOCR's policy deliberately removes some headers and footers. This service's evidence-preserving transcription has a different purpose, so reuse the testing approach rather than that filtering policy. The olmOCR 2 paper also describes a blank-page hallucination problem caused by omitted blank pages in training. Include blank pages when evaluating both the extractor and checker [11].

### Independent layout and text detection

PaddleOCR documents text detection as a separate module that outputs text-region polygons before recognition. DocLayNet annotates 11 layout classes, including text, picture, table, caption, and formula [10]. These distinguish “lots of visual content” from “lots of visible text.” Neither source makes a detector infallible.

DocLayNet reports that splitting pages rather than whole documents can overestimate performance. Split the local calibration and evaluation corpus by report, not by page [10].

## Recommended application

Use a small, explainable cascade:

1. Inspect native extraction and OCR for weak evidence, short content, broken structure, uncertainty markers, and disagreements. Inspect prose and table-cell content separately.
2. Route suspicious pages, plus a reproducible sample of unflagged pages, to an image-grounded checker.
3. Ask the checker to distinguish sparse content, omission, mismatch, and unreadable input. Retain cited image regions and short visible anchors.
4. Make a changed recovery attempt only when diagnosis supports one. Examples include a higher-resolution render or a different OCR model.
5. Publish the selected best-effort output with explicit assessment and recovery records. Unresolved quality concerns remain advisory.
6. Evaluate flag recall, false alarms, judge disagreement with human labels, recovery regressions, and cost. Include audits of unchecked output to estimate missed failures.

Initial length thresholds, sampling rates, model choices, and budgets remain proposed engineering policy. Evidence from the local corpus determines their useful values.

## Sources

1. [OCR-D quality assurance](https://ocr-d.de/en/spec/ocrd_eval#evaluation-metrics). Metric definitions, normalization, reference data, page granularity, and layout evaluation.
2. [Library of Congress ALTO](https://www.loc.gov/standards/alto/) and [layout and string-confidence documentation](https://www.loc.gov/standards/alto/techcenter/layout.html). Metadata standard, not a correctness threshold.
3. [PAGE XML](https://github.com/PRImA-Research-Lab/PAGE-XML) and [OCR-D PAGE conventions](https://ocr-d.de/en/spec/page). Region geometry, text alternatives, and confidence conventions.
4. [Tesseract command-line outputs](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html#tsv-output) and [improving quality](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html). TSV sentinel handling and OCR input preparation.
5. [OCRmyPDF advanced features](https://ocrmypdf.readthedocs.io/en/latest/advanced.html). Existing-text modes and their limitations.
6. [Consensus Entropy, arXiv:2504.11101v3](https://arxiv.org/html/2504.11101v3), §§3–4. Published agreement-based uncertainty and escalation. A [v4](https://arxiv.org/html/2504.11101v4) exists; numerical claims above intentionally identify the reviewed v3.
7. [OCR-Quality, arXiv:2510.21774v1](https://arxiv.org/html/2510.21774v1), §§2.3, 5.2, 6.3, 8–9. Direct judging precedent, reported results, and limitations. Treat as a research report, not a standard.
8. [OmniDocBench repository](https://github.com/opendatalab/OmniDocBench#evaluation) and [paper, arXiv:2412.07626v1](https://arxiv.org/html/2412.07626v1). Multidimensional parsing evaluation and failure examples. Versions differ.
9. [FADGI technical guidelines, third edition, May 2023](https://www.digitizationguidelines.gov/guidelines/FADGI%20Technical%20Guidelines%20for%20Digitizing%20Cultural%20Heritage%20Materials_3rd%20Edition_05092023.pdf), §§2.1, 6.7. [ISO 19264-1:2021 bibliographic scope](https://www.iso.org/standard/79172.html). ISO full text not reviewed.
10. [PaddleOCR text-detection documentation](https://www.paddleocr.ai/main/en/version3.x/module_usage/text_detection.html#1-overview), [DocLayNet repository](https://github.com/DS4SD/DocLayNet), and [DocLayNet paper](https://arxiv.org/abs/2206.01062). Text detection independent of recognition, region types, and document-wise evaluation splits.
11. [olmOCR-Bench specification](https://github.com/allenai/olmocr/tree/main/olmocr/bench#olmocr-bench-test-classes) and [olmOCR 2, arXiv:2510.19817v1](https://arxiv.org/html/2510.19817v1), §§2, 5. Concrete content tests and blank-page failure lessons.
