# PDF extraction and OCR robustness

Status: proposal. Policy decision: publish the selected best-effort attempt. Quality concerns are advisory and remain visible to downstream consumers. No parsing behavior is implemented by this document.

## Problem statement

A PDF extraction can complete normally while omitting most of a page. A typical failure is a short OCR transcript on a page containing several visible paragraphs. Other failures include a missing table, a dropped column, unreadable small labels, invented content, and wrong numeric strings.

The service needs to recognize suspicious output, compare it with the source page, and make a targeted recovery attempt. It also needs to explain what remains uncertain. A suspicious page must not lose its usable evidence while waiting for a better answer.

The initial policy is best effort. The service publishes the most defensible available extraction and attaches quality advisories. An unresolved quality concern does not, by itself, fail a job or change a completed extraction stage into a failure. This does not guarantee publication after a persistence failure, cancellation, or an unsettled provider operation. Those conditions prevent reliable publication independently of transcription quality.

## Context

### Current behavior

The current PDF adapter already retains native text, word boxes, tables, physical-page coordinates, rendered images, model provenance, and separate figure transcription and interpretation.

| Current integration point | Relevant behavior |
|---|---|
| `preprocessing/formats/pdf.py:239–249` | Extracts native text, word boxes, and tables |
| `preprocessing/formats/pdf.py:260–267` | Requests OCR for empty native text or embedded-image area totaling at least half the page |
| `preprocessing/formats/pdf.py:287–300` | Accepts structured OCR, including empty `text` and empty `tables`, as successful |
| `preprocessing/formats/pdf.py:139` | Renders at at most 2 pixels per PDF point, equivalent to 144 nominal DPI, and reduces scale to meet the pixel limit |
| `preprocessing/formats/pdf.py:346–355` | Accepts any nonempty figure transcription without a completeness check |
| `preprocessing/service.py:669–735` | Records and settles each model operation through the coordinator |
| `preprocessing/engine.py:519` | Supports an operation's `modelRole`; profiles currently expose only `vision` |
| `preprocessing/engine.py:136–155` | Bounds prompt bytes and aggregate image bytes and pixels |
| `preprocessing/results.py:77–81` | Determines job success from required stages; requires nonempty usable products, currently excluding `page-image` |
| `preprocessing/api.py:196–228` | Returns a bounded MCP summary that currently omits quality findings |

Embedded-image area is a routing proxy, not a measure of visible text. Its current sum can double-count overlapping images. Native text is also fallible: an existing OCR layer can contain only a footer or incorrect characters. Checking only pages already routed to OCR would miss suspicious native-only extraction.

The current model boundary uses a one-step, tool-disabled extraction agent. Its system instruction requires faithful transcription and separates interpretation. A sense-check operation can reuse this boundary after explicitly allowing assessment of supplied extraction against supplied images in `preprocessing/engine.py:SYSTEM` and its configuration validator. It does not need a separate autonomous runtime.

### Research basis

- OCR-D's CER and WER measure errors against reference text. They are evaluation tools, not reference-free runtime detectors [1].
- ALTO and PAGE preserve geometry, alternatives, confidence, and processing provenance. They do not certify correctness or define portable confidence thresholds [2].
- OCR-Quality directly studies image-plus-output judging. Its reported results and single-annotator limitations make local judge evaluation essential [3].
- Consensus Entropy supports agreement-based uncertainty and selective routing. Agreement can still conceal shared errors [4].
- OmniDocBench and olmOCR-Bench show that text, tables, formulas, and reading order need distinct checks [5–6].
- Tesseract and FADGI describe input quality. Better images can help OCR, but imaging conformance is not output assurance [7–8].

No reviewed standard provides a universal “too little text” threshold. The numbers below are proposed pilot settings, not established accuracy guarantees.

## Goals

- Detect likely omissions in both native extraction and model OCR.
- Distinguish legitimate sparse pages from failed transcription.
- Use a bounded image-grounded checker to diagnose suspicious output.
- Recover selectively through a changed render or an alternate model.
- Publish the best-effort output with source evidence, attempt history, and explicit advisories.
- Measure missed failures, false alarms, recovery regressions, latency, and extra model usage.
- Expose quality settings and budgets through editable administrator profiles.

## Non-goals

- Guarantee that every character or table is correct.
- Require human approval before publication.
- Run a multi-model OCR ensemble on every page.
- Normalize, interpret, or repair report facts from domain knowledge.
- Replace PDF parsing, model-operation settlement, or generation publication infrastructure.
- Add ALTO or PAGE export without an interchange requirement.

## Approaches considered

### Approach A: suspicious-output checks with one targeted recovery (minimal viable)

Summary: compute cheap signals, send suspicious pages and a small audit sample to a vision checker, and allow one diagnosed recovery. Retain all candidates and publish a selected attempt with advisory quality metadata.

Complexity: M. Risk: medium.

Pros:

- Fits the existing adapter, model coordinator, products, and findings.
- Addresses the short-output example without introducing another model-serving dependency.
- Explains decisions in terms of specific page evidence.
- Makes cost and uncertainty visible from the first release.

Cons:

- Image-heavy routing produces false alarms on maps and photographs.
- A normal-length transcript can still omit a column.
- The checker can share extraction errors, especially when the same model is used.

Reuses: `PDFAdapter`, `Renderer`, `ExtractionContext.model`, operation persistence, model roles, page inventory, and generation sealing.

### Approach B: independent text detection and region-level reconciliation (ideal architecture)

Summary: detect text, table, and picture regions independently of recognition. Compare extraction coverage by region and recover missing regions using high-resolution crops or an alternate backend.

Complexity: L. Risk: medium.

Pros:

- Provides stronger evidence that visible text exists despite a short transcript.
- Can identify an omitted column even when total output is long.
- Routes recovery to the affected region and preserves page coordinates.

Cons:

- Adds model packaging, runtime cost, and another source of false negatives.
- Requires tested crop transforms, reading-order reconstruction, and overlap deduplication.
- True geometric coverage needs transcript-to-region alignment. Current model OCR has no boxes.

Reuses: physical-page provenance, native word boxes, replaceable detector patterns, and model-operation boundaries. Text detection is separate from the existing figure detector because their evidence means different things.

### Approach C: independent second reading before full diagnosis

Summary: for suspicious pages, request an independent transcription from another model or a conventional OCR engine. Compare literal content and structure, then ask the checker only about disagreements.

Complexity: M–L. Risk: medium.

Pros:

- Reduces dependence on subjective single-output scoring.
- Supplies a useful alternate candidate directly.
- Conventional OCR can supply boxes and word-confidence distributions.

Cons:

- Pays for a second extraction before learning that a page is legitimately sparse.
- Different serialization and table representations complicate comparisons.
- Shared omissions can still produce agreement.

Reuses: named model roles and immutable attempt products. A conventional backend would require a new worker-local integration.

## Decision

Implement Approach A first, in assessment mode before enabling recovery. Add Approach B only when labeled failures show that the cheap routing signals miss important regions. Evaluate Approach C as a recovery strategy rather than a mandatory pass.

Complete ordinary extraction and validate baseline candidates before starting optional quality work. This second pass establishes the baseline's row, evidence-size, and time usage. It prevents earlier optional checks from spending resources needed to extract later pages. Load candidate content one unit at a time from disk. Only small signal and identity records need remain in memory. Materialize selected model-table exports and the final page inventory after selection.

The checker is a bounded model operation. Application code owns routing, budgets, candidate selection, and publication. A different checker model is preferred when available; same-model checking remains configurable, and the report records that limitation.

### Processing flow

This is the logical flow for one source unit. The quality pass starts after baseline extraction completes for the whole document. Publication occurs once after final selection.

```text
native extraction and existing OCR
             |
       validated candidate
             |
 cheap signals + reproducible audit sample
             |
   no check needed --------> publish candidate, quality=unchecked
             |
     image-grounded check
             |
  no issue / sparse --------> publish candidate + assessment
             |
  omission / mismatch / unreadable input
             |
  diagnosed recovery, if enabled and within budget
             |
 validate candidate + compare against source and baseline
             |
 select best effort -------> publish selected candidate + advisories
```

Assessment mode runs detection and diagnosis without replacing output. Recovery mode additionally permits a changed extraction attempt. Disabled mode records that quality checking was disabled. In all modes, a completed extraction retains its execution outcome even when a quality advisory is unresolved.

### Detection signals

Run signal collection for every physical page. It is cheap for native-only pages. Render those pages only when they require checking or enter the audit sample.

| Signal | Initial use | Caveat |
|---|---|---|
| Empty prose and no nonempty table cells | Check whenever a source render is available | Blank pages are legitimate |
| Short prose and image-heavy page, with little table-cell content | Check likely footer-only or thin-text-layer extraction | Image-heavy does not mean text-heavy |
| Native characters or words far below other pages of the same apparent type | Weak signal combined with other evidence | Section dividers, title pages, and maps differ naturally |
| Replacement characters, repeated output, or many uncertainty markers | Check suspect recognition or generation | Symbols and repeated numeric values can be valid |
| Native numeric anchors missing from OCR prose and table cells | Check conflicting evidence | Native extraction is another fallible witness; differing serialization can cause false alarms |
| Small render scale or unreadable glyphs | Record render limitation; consider changed rendering | Input quality alone does not prove omission |
| Independent text regions with too little extraction | Stronger second-phase routing signal | Requires a separately evaluated detector and alignment |

Keep `proseChars`, `tableCellChars`, `tableCount`, and `nonEmptyCellCount` separate. Count non-whitespace Unicode code points for the character signals. Do not sum prose and table-cell characters as if they were distinct content: the prompt may include the same table in both. Short prose with a fully populated table is not automatically incomplete. The checker receives both the prose and the complete table candidate.

A pilot can try `shortTextChars=200`, image-area union fraction `0.5`, and an audit sample rate of `0.02`. These values route inspection only. They do not declare content missing. Use clipped rectangle union area for new signals and for revised OCR routing, rather than summing overlapping images. Sample deterministically from source hash, page identity, scope, and policy version so reruns are reproducible. Record why a page was selected.

For the initial disagreement signal, retain literal native word tokens containing digits as anchors and test their presence in OCR prose and table-cell strings. Keep token boundaries and original spelling. Do not compare floats or semantic embeddings. Record the missing anchors with their native boxes. This is evidence of disagreement, not evidence that the native layer is correct. Limit the number of reported anchors without hiding the total conflict count.

Do not infer a reliable page type from the transcript alone. The checker classifies the visible page. Word-confidence signals are optional and absent for the current generative backend. Measure blank and section-divider prevalence in the pilot. The first release accepts some paid blank-page checks to avoid silently calling failed empty OCR a blank page. Budget skips remain visible.

### Checker contract

Input contains the source-unit identity, the exact rendered image, render metadata, candidate prose and tables, available native evidence, and routing signals. The image is the authority for visible content. Native text and candidate strings are comparison material, not instructions.

The check asks whether readable content is missing, materially changed, duplicated, out of order, or inconsistent with visible tables. It distinguishes dense text from graphics and legitimate sparse pages. It returns observable findings rather than repaired transcription or domain interpretation.

Example output for a missing body:

```json
{
	"schemaVersion": 1,
	"verdict": "incomplete",
	"pageKind": "mixed",
	"pageObservation": "The body paragraphs and a small table are readable at this render size.",
	"issues": [{
		"id": "issue_0001",
		"code": "MISSING_TEXT_REGION",
		"severity": "major",
		"imageBounds": [0.08, 0.20, 0.91, 0.77],
		"visibleAnchor": "Sample preparation",
		"observation": "Several readable body paragraphs are absent from the candidate."
	}],
	"suggestedAction": "higher_resolution"
}
```

Allowed verdicts: `no_issue_found`, `incomplete`, `mismatch`, `no_visible_text`, and `indeterminate`. Allowed page kinds: `text`, `table`, `figure`, `mixed`, `blank`, and `unknown`. `pageObservation` is a required bounded statement about the visible page and its readability. Suggested actions form an allowlist: `none`, `higher_resolution`, `alternate_model`, and `manual_review`. Issue severity is `major` or `minor`. Initial checker issue codes are `MISSING_TEXT_REGION`, `MISSING_TABLE`, `CONTENT_CHANGED`, `UNSUPPORTED_CONTENT`, `DUPLICATED_CONTENT`, `READING_ORDER_SUSPECT`, and `UNREADABLE_INPUT`. Application code adds `NATIVE_OCR_DISAGREEMENT` issues from the anchor signal and can derive `UNSUPPORTED_CONTENT` from a blank-page conflict. Record whether each issue came from the checker or application rules. Scope issue IDs by attempt and assessment, with stable unique local suffixes. Code checks types, finite coordinates, ordering, unit bounds, string sizes, array sizes, and enum membership. `incomplete` and `mismatch` require at least one issue.

`imageBounds` uses top-left-origin coordinates normalized to the exact input image, in `[0,1]`. Store that image's digest and render transform with the report. These are approximate reviewer locations, not precise OCR word boxes. Convert to PDF points only through the recorded transform. A readable anchor is optional. Its absence does not authorize inventing one.

Use `indeterminate` if the supplied image is unreadable or the input is incomplete. Require a sparse-page observation for `no_visible_text`. Derive published quality state in application code rather than copying the verdict blindly:

- `no_visible_text` with any non-whitespace candidate prose or nonempty table cell becomes `suspect`, with `UNSUPPORTED_CONTENT`. Its permitted next action is alternate-model extraction or manual review. Page blankness does not validate a fabricated transcript.
- A numeric-anchor disagreement remains advisory even if a first-pass checker returns `no_issue_found`. The first release does not let that bare verdict resolve the conflict. Keep `suspect` until a grounded recovery comparison addresses the corresponding issue, or leave it unresolved.
- An unreadable image cannot support `no_visible_text` or `no_issue_found`. An inconsistent response is invalid and leaves quality indeterminate.

The engine currently limits prompts to `textBytes`, normally 32 KiB, while OCR output can be larger. Calculate prompt size before dispatch, including both complete candidates for comparison. A baseline check can fit while the comparison exceeds the limit. Never truncate candidate text and then claim to have assessed the whole page. The first release records `CHECK_INPUT_TOO_LARGE`, skips replacement, and retains the baseline. Region checks or explicitly increased profile limits can address this later. Input image pixel and byte limits apply to the aggregate attachments, not each image independently.

### Recovery and candidate selection

Default to one recovery candidate per source unit. Every attempt is an intentional new operation after the preceding operation has settled. Quality recovery is separate from replaying an ambiguous provider request and from the currently unused generic `retry` settings. Administrators can increase the recovery and call budgets. Further attempts require an unused materially different configured strategy, such as higher-resolution rendering followed by the alternate model. Stop when no changed strategy remains. Compare each new candidate with the current selected best effort, retaining the original baseline and all prior assessments.

1. **Changed rendering:** if diagnosis points to small text or downsampling, render the original page at a higher requested scale. A pilot can request 300 nominal DPI, subject to the configured pixel and image-byte limits. Record achieved scale. Do not label an unchanged render a resolution recovery. Upscaling an already poor scan does not create missing detail.
2. **Alternate model:** if diagnosis points to omissions or structure despite a readable image, transcribe the source again with `models.ocrRecovery`. Supply the diagnosis as locations to inspect, not invented replacement text. The source image remains authoritative.
3. **Later region recovery:** add direct high-resolution region rendering, orientation correction, and crops when evaluation justifies them. Cropping an existing low-resolution full-page render is not resolution recovery. Tiling and stitching are outside the first release.
4. **Stop:** if recovery is disabled, unavailable, unchanged, over budget, or yields no defensible improvement, publish the retained candidate and an advisory. A manual-review recommendation creates evidence for later review, without holding the job open.

Validate each candidate's prose and tables before selection. Validate cell types, row and column counts, candidate bytes, and remaining publication capacity as well as JSON shape. Reserve capacity for all baseline candidates and their table exports before admitting an optional replacement. Selection may use only a candidate that fits those constraints. A settled optional candidate that violates them is rejected with `recoveryState: stopped` and a limit advisory. It must not reach `pdf_table` and turn a recovery into a fatal row-limit error. Keep the baseline if the replacement is malformed or comparison is indeterminate. Never choose the longest response merely because it is longer.

Compare baseline and recovery against the best available source render, with neutral candidate labels and no model identity in the prompt. Choose and record the A/B order reproducibly from source-unit and policy digests to avoid always placing the recovery last. Ask for repaired original issues and newly lost or changed content, especially numbers, units, tables, and reading order.

The comparison response contains exactly two candidate assessments, each identified by alias `A` or `B` and using the same verdict, page-kind, and issue schema as the initial check. It also contains `preferredAlias: A | B | neither`, `resolvedReferenceIssueIds`, and a bounded `preferenceEvidence` list of observations with image bounds. The reference is the currently selected attempt, initially the original baseline. Application code checks aliases, referenced issue IDs, and nonempty source-grounded preference evidence. It maps aliases back to attempt IDs and records the reference identity. Any new major issue, unresolved input limitation, or structurally invalid comparison prevents automatic replacement. The model does not return an authoritative `selectedAttemptId`.

Selection uses the following order:

- Prefer a schema-valid recovery when image-grounded comparison identifies resolved issues and no new major content loss or contradiction.
- An improved but still incomplete candidate can be selected when that comparison supports the improvement. Keep its unresolved quality advisory.
- Keep the baseline for ties, ungrounded preference, new major errors, or an indeterminate comparison. Preserve both candidates for inspection.
- If there is no usable baseline and one valid recovery, publish it with its actual assessment state. Do not call a failed baseline a successful attempt.

This is a defensible best-effort selection rule, not a proof of nonregression. Its error rate must be measured separately from the extractor's accuracy.

Defer model-table publication until the candidate is selected. Today `pdf_table` writes model tables before page OCR is accepted. Recovery must not append all candidates' tables as if they were distinct source tables. Keep native tables as separate native evidence. Candidate JSON retains rejected model tables; ordinary OCR table products come only from the selected candidate and link to its attempt identity.

### Assessment, execution, and advisories

Execution state answers whether extraction ran. Quality state answers what inspection found. Keep those concepts separate.

| `qualityState` | Meaning |
|---|---|
| `unchecked` | No image-grounded check was completed; reason includes disabled, not selected, budget, or unavailable input |
| `no_issue_found` | The checker found no issue in the supplied scope; correctness is not certified |
| `no_visible_text` | The checker found no visible text in the supplied scope and recorded why |
| `suspect` | An omission, mismatch, or unresolved conflicting witness remains |
| `indeterminate` | The checker ran but could not assess the supplied evidence reliably |

Separate `recoveryState`: `disabled`, `not_needed`, `improved`, `no_improvement`, or `stopped`, with a required reason when disabled or stopped. Assessment mode records `disabled` with reason `assessment_mode`. An attempted recovery whose comparison is indeterminate records `stopped` with reason `comparison_indeterminate`. `no_improvement` means comparison completed without a defensible improvement. A selected attempt may be `improved` and still `suspect`. Keep initial findings in the history and mark repaired ones resolved rather than dropping them.

Quality-only failures produce per-unit advisory codes such as `OCR_COMPLETENESS_SUSPECT`, `OCR_RENDER_LIMITED`, `OCR_CHECK_UNAVAILABLE`, `OCR_RECOVERY_STOPPED`, or `OCR_REVIEW_RECOMMENDED` in quality reports. Add one aggregate quality finding with counts and an inventory link to the manifest's capped findings list. Avoid one manifest finding per quality issue, which could crowd out execution failures. Existing extraction failures still describe actual failures. Use a `pdf-quality-assessment` stage with `required: false`, so missing assessment does not determine job success. Do not increase physical-page failure counts for quality-only concerns.

Publishing best effort also needs an explicit image-only case. `make_manifest` currently rejects a PDF with only empty text products and `page-image` evidence. Permit a valid listed PDF page image to satisfy evidence usability when no transcript is usable. Publish empty or unavailable text honestly, retain the image, and explain why. Do not fabricate a figure solely to pass the current usable-product check. Input, persistence, or unsettled-operation failures still follow the service's existing failure and settlement handling.

### Evidence and downstream visibility

Add listed, hashed products under the generation:

```text
text/page_0007.quality.json
text/page_0007.attempt_01.json
text/page_0007.attempt_02.json
text/page_0007.png                   # listed source render for checking
text/page_0007.attempt_02.png         # when a changed render exists
text/page_0007.ocr.txt                # selected OCR prose, when available
quality/units.jsonl                  # pages and, later, figure scopes
```

Attempt JSON contains the exact bounded candidate prose and tables, method, render reference, model and operation identity, schema-validation state, and assessment references. Quality JSON contains policy version, signals, input scope, checker result, selected attempt, selection reason, unresolved issues, and recovery stop reason. Retain provider response evidence through the existing operation ledger. If an invalid response cannot fit published evidence limits, list an attempt record with the rejection and evidence availability rather than claiming the full response is published. Do not request or publish hidden model reasoning.

Retain every image actually supplied to a checker as a listed product, including renders made for native-only pages and audit-selected pages. Reuse an existing page image when its exact digest and transform match. Use attempt-specific names for changed renders. If retention capacity is unavailable, skip the optional check and record the gap. A digest alone cannot let a downstream reader inspect a cited region.

Add `selectedTextPath`, `selectedAttemptId`, `qualityState`, `qualityPath`, and `recoveryState` to the page inventory. Preserve `nativeTextPath` even if model OCR is selected. Native-only candidates use the same assessment shape without fabricating a model operation. `page_0007/attempt_01` is always the baseline, including a native-only baseline. Each attempt ID maps to exactly one listed attempt JSON product; its render is a separate image product that can share the attempt suffix. Subsequent attempts use increasing suffixes.

Write canonical `.ocr.txt` and selected model-table products once, after selection. `selectedTextPath` points to the selected native text or OCR prose. Distinguish baseline operation status from selected-product status: add `baselineOcrStatus` for the original OCR operation, and let `ocrStatus` describe execution of the selected OCR attempt when one exists. A quality concern never sets either execution status to failed. Native-only selection has `ocrStatus: not_applicable`.

Propagate operation IDs through `Service._model` and `ExtractionContext.model`, which currently expose model/session metadata without the coordinator's operation identity. Store exact requested and returned models, image digests, prompt versions, source bounds, and effective render scale.

The manifest includes counts by quality state, unresolved issue count, checked fraction, stop-reason counts, and a link to the full quality inventory. Compute these from `quality/units.jsonl`, not from capped findings. The inventory contains one record per page initially and can include separate figure scopes later. Its small common records support aggregation without rereading every detailed report. Add an optional `quality` summary field to the extraction contract and validate it against the listed inventory. Include this compact summary before expandable products in the bounded MCP response. Navigation links to per-page quality records and states that `no_issue_found` is an inspection result. Update `build_indexes`' product-kind whitelist for quality and attempt products. Full per-unit evidence remains available even after the existing 100-finding cap is reached.

### Configuration and budgets

Proposed profile additions, with illustrative pilot settings:

```json
{
	"models": {
		"vision": "provider/primary-vision",
		"ocrCheck": "provider/checker-vision",
		"ocrRecovery": "provider/recovery-vision"
	},
	"options": {
		"pdfQuality": {
			"mode": "assess",
			"shortTextChars": 200,
			"imageAreaFraction": 0.5,
			"auditSampleRate": 0.02,
			"recoveryDpi": 300,
			"maxRecoveryAttemptsPerUnit": 1,
			"maxQualityModelCallsPerUnit": 3,
			"maxQualityModelCallsPerJob": 60,
			"maxQualityEvidenceBytes": 67108864,
			"finalizationReserveSeconds": 30
		}
	}
}
```

`mode` allows `off`, `assess`, and `recover`. The initial enabled deployment uses `assess`. The example permits one recovery, but none runs in assessment mode. Three additional calls cover initial diagnosis, recovery extraction, and candidate comparison. Primary OCR and figure interpretation are existing work, outside this additional-call count.

Thresholds and sampling are advisory routing policy. Call budgets are enforced dispatch limits for the selected profile and are administrator-overridable. Allow zero to disable recovery or additional calls explicitly. Accept nonnegative character thresholds, rates in `[0,1]`, and positive render DPI. Profiles can inspect every page with an audit rate of 1. Model-role selection is explicit when the corresponding operation is enabled. `--model` may fill enabled roles with the same model, with that shared-model limitation recorded. Extend `OpenCodeServerEngine.ready`'s image-input requirement from the literal `vision` role to every enabled vision role, including `ocrCheck` and `ocrRecovery`.

An audit-selected page runs the same initial diagnosis as a signal-selected page. Audit selection does not add a separate check. If that check discovers a defect, recovery eligibility is identical. All optional model calls count toward the same unit and job budgets, so diagnosis plus recovery plus comparison still totals three calls at the default recovery limit. With a higher limit, K recovery candidates need at most `1 + 2K` additional calls. Derive `policyVersion` from an explicit algorithm/prompt version and a digest of the resolved quality settings and models. Record it in evidence and use its digest for sampling.

Stop starting optional quality work before it consumes the time or output space needed to publish existing evidence. After baseline extraction, allocate an overridable `maxQualityEvidenceBytes` allowance from remaining `limits.outputBytes`, preserving capacity for baseline exports, quality summaries, and navigation. Optional evidence writes consume only that allowance. Check remaining `limits.jobSeconds` against `limits.modelSeconds` and `finalizationReserveSeconds`. The illustrative 64 MiB evidence allowance and 30-second reserve require local measurement; neither guarantees enough time or space. Bound reviewer JSON, candidate JSON, and attempt images before writing them. A locally predicted optional-input or output limit records an advisory before dispatch.

After dispatch, a candidate-local constraint violation can become an advisory only when the coordinator confirms settlement. A known-settled oversized response or candidate with too many rows is rejected without invoking fatal baseline writers. OSError, memory exhaustion, input-integrity failure, cancellation, persistence failure, and unsettled outcomes retain existing service handling. In particular, `test_output_limit_retains_session_instead_of_truncating_success` documents an engine output-limit case with an unsettled session. That case still interrupts the job. Never catch it as ordinary optional-budget exhaustion. The advisory policy cannot bypass operation ownership or fabricate settlement.

In the quality pass, inspect signal-selected units before audit-only units. Preserve deterministic source order within each group. Record the actual checked fraction and budget skips. Budget exhaustion can still leave later units unchecked; do not claim inspection coverage that was not achieved. Money estimates remain optional because current usage reporting can be incomplete. Publish observed calls and usage without claiming an exact cost.

## Implementation plan

### Phase 1: evidence and assessment

- Introduce a small candidate representation that keeps prose and tables together until selection. Persist baseline candidate records during ordinary extraction, then run the optional quality pass before canonical model-table exports and final inventory writing. Add pure signal calculation and checker-response validation in a PDF quality module. Keep rendering and `context.model` at the existing effect boundary.
- Add profile validation, enabled model roles, explicit assessment-compatible system instructions, and prompt versions in `contracts.py`, `cli.py`, and the engine configuration validation and tests.
- Check suspicious native-only and OCR pages. Add deterministic audit selection. Detect oversized checker input before dispatch.
- Persist quality reports, attempt evidence, advisory findings, an optional quality stage, and operation references. Publish image-only PDF evidence when text is unavailable.
- Add quality summaries to `results.py`, `indexes.py`, and `api.py`. Ensure summary truncation cannot silently hide the presence of unresolved issues.

Acceptance: a dense-page omission gets a source-grounded advisory. A legitimate sparse page gets a sparse assessment. Blank-page hallucinations and unresolved numeric-anchor conflicts remain suspect. Disabling or failing a settled checker retains the completed extraction and reports the assessment gap. An image-only page publishes inspectable evidence. Every completed check has a retained input image.

### Phase 2: targeted recovery

- Make requested render scale configurable and record achieved scale.
- Add the single diagnosed recovery candidate and grounded candidate comparison.
- Write selected model tables once. Retain rejected candidates separately.
- Stop on no improvement, indeterminate comparison, or exhausted budgets. Expose the best-effort selection and remaining issues.

Acceptance: a source-supported improvement can replace the baseline. A longer hallucinated answer, numeric regression, malformed table, or inconclusive comparison cannot replace a usable baseline. An improved-but-incomplete attempt still carries its advisory.

### Phase 3: stronger detection where measured failures justify it

- Evaluate independent text-region detection, especially missed columns and small table text.
- Add direct region rendering and alignment before claiming geometric coverage.
- Extend the same assessment contract to figure OCR. Page-level acceptance must not imply that every legend or axis label was checked.
- Add orientation-specific recovery or an OCR backend when observed failures and benchmarks justify the dependency.

Initial figures retain existing execution semantics and have `unchecked` quality unless explicitly assessed. Do not apply prose-density thresholds to diagrams. Detection of missing figures remains a separate discovery problem.

## Observability

Record events for signal selection, check verdict, quality-call refusal, recovery start and stop, and candidate selection. Link each event to the physical page, scope, attempt, operation, policy version, and evidence product. Keep source content in evidence artifacts rather than general service logs.

Report:

- Checked and unchecked units by scope and reason.
- Suspect and indeterminate units, before and after recovery.
- Missing-region and table-omission findings, plus sparse-page false alarms.
- Recovery improvement and regression rates from human-labeled evaluation.
- Additional calls, observed usage, elapsed time, and p50/p95 overhead.
- Achieved render scale and render/input limits.
- Missed defects found by unflagged-page audits.

Do not report all successful jobs as quality-checked or all `no_issue_found` units as accurate. Advisory quality is distinct from extraction execution and publication success.

## Verification strategy

For this proposal: direct source inspection and primary-source research, followed by static design review. No behavioral test is warranted because no parser or service code changes.

For implementation, use red-green tests only for maintained observable behavior:

1. A dense-page short transcript routes to checking, including empty structured OCR and thin native text layers.
2. A blank page, map, sparse title page, or table-only page avoids a false missing-body verdict in labeled evaluation. Stub tests verify routing and verdict handling, not actual vision ability.
3. Settled checker failure, malformed JSON, oversized check or comparison input, exhausted optional budgets, and candidate-local row or byte overruns retain the baseline and expose a gap advisory. Unknown settlement still blocks publication.
4. A grounded improved candidate is selected, while malformed, unsupported longer, or numerically degraded candidates are rejected. Selected tables publish once with attempt provenance.
5. Unresolved quality does not change a completed extraction's execution outcome. An image-only PDF publishes its source image honestly.
6. Quality warnings and links remain discoverable in bounded summaries and after finding-cap overflow.
7. A new recovery operation starts only after settlement. Existing ambiguous-outcome tests still reject mutation replay. Signal and audit checks share the same three-call maximum. Every enabled model role requires image capability.
8. Render transforms preserve rotated MediaBox and CropBox alignment. Budget and changed-render tests reject fake resolution improvements. Optional work cannot consume baseline publication capacity. A no-visible-text verdict with candidate content and a no-issue verdict with unresolved numeric-anchor conflict both remain suspect.

Run relevant existing PDF format, index, API, service, profile, manifest, and engine tests. Existing named tests include `test_scanned_pdf_structured_ocr_preserves_table_lexemes_and_model_provenance`, `test_pdf_crop_box_does_not_shift_media_box_figure_evidence`, `test_pdf_resource_limits_do_not_become_partial_ocr_results`, `test_mcp_result_summary_is_byte_bounded_and_links_to_full_manifest`, and `test_lost_ack_readback_never_replays_mutations`.

### Evaluate actual robustness separately from plumbing

Build a first corpus of approximately 300 pages across at least 20 reports, with both natural failures and good outputs. Include dense prose, tables, maps, captions, blank pages, multi-column layouts, rotated scans, tiny numeric text, and mixed native/scanned pages. The counts are a pilot scope, not a statistical guarantee. Split calibration and held-out evaluation by whole report. Have two reviewers independently label a representative subset and adjudicate disagreements.

Create additional controlled defects: delete a paragraph, omit a column, remove a table, duplicate a block, alter a decimal or unit, return empty structured output, and introduce hallucinated text. Keep controlled-defect results separate from natural-failure prevalence. Use known content-presence and table facts as in olmOCR-Bench. Add reference transcripts for a smaller subset to measure explicitly defined CER, deletion rate, numeric-string accuracy, and table fidelity.

Compare cheap signals alone, signals plus same-model checker, signals plus a different checker, and diagnosed recovery. Report severe-omission recall, false alarms on good pages, judge false acceptance, indeterminate rate, recovery improvement, and recovery-introduced errors. Measure cost and latency rather than making unbenchmarked performance claims.

Proposed pilot objectives are at least 95% severe-omission detection recall and at most 5% false alarms on good pages. Report denominators and uncertainty intervals by page type. Small samples cannot establish those rates reliably. Measure remaining error among automatically selected outputs and sample unflagged outputs for human review. If the objectives or nonregression evidence are weak, retain assessment mode and improve the detector or checker before enabling automatic replacement.

## Open questions

- Which explicitly configured checker and recovery models perform best on representative reports? Model diversity is preferred but remains an empirical choice.
- What routing thresholds and optional-work budgets meet the actual latency and cost objectives? No local labeled corpus or benchmark was run for this proposal.
- Does region detection materially improve missed-column detection enough to justify its runtime dependency?

Best-effort publication with advisories is decided. These empirical questions do not block assessment-mode implementation.

## References

1. [OCR-D quality assurance](https://ocr-d.de/en/spec/ocrd_eval).
2. [ALTO metadata](https://www.loc.gov/standards/alto/techcenter/layout.html) and [OCR-D PAGE conventions](https://ocr-d.de/en/spec/page).
3. [OCR-Quality: image-grounded quality assessment](https://arxiv.org/html/2510.21774v1), §§5, 8–9.
4. [Consensus Entropy: agreement and selective routing](https://arxiv.org/html/2504.11101v3), §§3–4. Published results are not local assurance targets.
5. [OmniDocBench](https://github.com/opendatalab/OmniDocBench).
6. [olmOCR-Bench tests](https://github.com/allenai/olmocr/tree/main/olmocr/bench#olmocr-bench-test-classes) and [olmOCR 2](https://arxiv.org/html/2510.19817v1).
7. [Tesseract quality guidance](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html).
8. [FADGI technical guidelines, May 2023](https://www.digitizationguidelines.gov/guidelines/FADGI%20Technical%20Guidelines%20for%20Digitizing%20Cultural%20Heritage%20Materials_3rd%20Edition_05092023.pdf), §§2.1, 6.7.
9. [Independent text detection](https://www.paddleocr.ai/main/en/version3.x/module_usage/text_detection.html#1-overview) and [DocLayNet](https://arxiv.org/abs/2206.01062). Useful for the later region-aware approach.
