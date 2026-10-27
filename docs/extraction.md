# Transcription response acceptance

Page OCR accepts one complete JSON object with exactly `text` and `tables`.
`text` is a string. Each table contains `rows` and optional normalized `bounds`.
Rows contain strings or nulls. Numeric spellings remain strings. The row matrix
defines one-based indices and unit spans. Explicit indices, spans, and unknown
fields are rejected rather than ignored. Supplied bounds must be finite positive
rectangles within 0..1000. Row, column, and dense-grid memory limits apply before
candidate allocation. Empty pages use `{"text":"","tables":[]}`.

Figure OCR accepts exactly `{"text":"..."}`. An empty string means that no text
is visible. Figure interpretation remains a separate product and operation.

Invalid responses remain raw evidence. They do not become transcription
products, including empty failed placeholders. Findings identify rejected
responses as `INVALID_EXTRACTION_RESPONSE`. JSON fences, commentary prefixes,
duplicate keys, unsupported fields, and non-finite constants are invalid. There
is no embedded-JSON salvage or wording blacklist.

The engine retains assistant message boundaries, finish reasons, and operation
identity. Multiple assistant answers are not concatenated. Completed truncated,
refused, empty, and ambiguous answers fail extraction promptly. Supplier cleanup
still determines settlement independently of extraction acceptance.

## Native baseline applicability

After the existing occurrence and bounds filtering, an empty native-word list
produces `baselineCheck: {"state":"not-applicable","reason":"no-native-word-evidence"}`.
It does not produce `incompatible-baseline-row-count`. Nonempty lists still run
the existing baseline checks. Other geometry findings remain independent.

No-native-word evidence does not confirm a table. Confirmation still requires
independent agreeing reads under the existing comparison rules. Source headers
remain rows. A source header plus three data rows has four logical rows and
`header: "not-inferred"`.
