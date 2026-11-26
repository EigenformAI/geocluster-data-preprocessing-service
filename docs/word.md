# Word result products

The `.docx` adapter reads the stored XML in the main Word part. It does not
render, edit, fetch relationships, unpack the archive to disk, or execute fields,
macros, or embedded objects. Word jobs use the same service admission and
scheduling as other formats, but their extraction sends no model prompts.
Legacy `.doc`, macro-enabled `.docm`, and encrypted Word packages are unsupported.

## Read body text and tables

Use the existing result manifest and product routes:

| Kind | Product | Contents |
| --- | --- | --- |
| `document-text` | `document/body.txt` | UTF-8 main-body prose in paragraph order. A newline follows each paragraph. Table text is not duplicated here. |
| `document-blocks` | `document/blocks.jsonl` | Ordered paragraph, table, and omitted block records. Paragraphs have a zero-based UTF-8 `textOffset` and `textBytes`, excluding the final separator. Tables have a `tableId` from the manifest. |
| `typed-table`, `table-export` | Per-table JSONL and CSV | Physical source rows and cells in XML order. The normal table rows endpoint returns the same typed rows. Headers remain rows. |

Products and rows use `source.kind: "word-body"`. `source.part` is the package
path of the main part. `source.block` is the one-based physical position among
all direct body XML elements, including ignored metadata and section properties.
Positions are not renumbered after filtering, so they can exceed the coverage
total. Table IDs use the same physical body position. Table `row` and `cell`
positions are one-based among direct Word row and cell elements. These are
coordinates in the exact source snapshot, not page numbers or logical grid
positions. Merged cells remain as stored, without filling spans or aligning
ragged rows. CSV is a raw-value
export, not a sanitized spreadsheet or a reconstruction of Word layout.

## Interpret coverage

Coverage counts direct body blocks, excluding section properties and recognized
metadata-only annotations. A block with skipped content counts once as
`unsupported`, even if it has usable text or cells. The result is `partial` when
it retains usable evidence alongside such
blocks. The block map marks incomplete blocks and locates fully omitted blocks.
In paragraph and cell text, `U+FFFC` marks a gap where an unsupported element was
skipped. Check the block map before reading the prose as a continuous passage.

The adapter ignores these empty annotations in the document's Word namespace:

- Bookmark starts and ends, comment range starts and ends, proofing marks,
  and permission range starts and ends in the body, paragraphs, hyperlinks,
  simple fields, tables, rows, and cells.
- Comment references and last-rendered page breaks inside runs.

These annotations produce no block records, gaps, or findings. A paragraph
containing only annotations remains an empty paragraph record. Foreign namespace
namesakes, misplaced annotations, and annotations containing child elements or
non-whitespace text remain unsupported. Revision ranges and content-bearing
revision wrappers remain unsupported.

Each affected body block contributes one `WORD_CONTENT_OMITTED` finding, subject
to the result's 100-finding limit. The message identifies drawing content only
when every encountered omission in that block is a Word `drawing` element.
Unknown, opaque, or mixed omissions use the generic unsupported-content message.
The adapter does not inspect skipped descendants to classify them. It adds
“Stored text was retained.” only when it extracted non-whitespace text from that
same paragraph or table. Drawing findings do not identify a chart, picture, or
other visual type.

The adapter reads stored text, tabs, and breaks. It keeps hyperlink labels and
stored field values; it does not follow links or refresh fields. Drawings, text
boxes, equations, revisions, content controls, nested tables, and alternative
content are not expanded. Headers, footers, footnotes, endnotes, comment bodies,
formatting, generated numbering, the table grid, and pagination are outside
the coverage scope. A result is not a visual transcript.
Comment references are annotations, not comment text. Inline footnote and endnote
references remain unsupported because their displayed reference text is not
extracted.

These extraction semantics are recorded as `provenance.adapterVersion: "2"`.
Previously published results retain their original coverage and findings.

## Limits and errors

The normal input, expanded-byte, output, worker-memory, and job limits apply.
`rows` counts across all Word tables. `columns` caps physical cells per row.
The ZIP directory read is capped at 512 KiB and the package at 4,096 entries.
The two metadata XML parts are capped at 1 MiB each, the main part at 32 MiB,
and each parsed XML tree at 250,000 elements and 64 levels. At most 512 tables
are exported. Main parts must use a literal package-local path. Only stored
and deflated XML entries are read.

Limit violations fail with `RESOURCE_LIMIT` and do not publish a partial
generation. A malformed package, unsafe XML, or a package with no supported
prose or table rows fails with `INVALID_INPUT`. Unsupported formats or ZIP
features fail with `UNSUPPORTED_FORMAT`.

## Synthetic input

Generate a mixed paragraph-and-table fixture with
`uv run --locked python scripts/generate_word_fixture.py /path/to/synthetic.docx`.
Add `--prose-only` for a separate one-paragraph fixture.
