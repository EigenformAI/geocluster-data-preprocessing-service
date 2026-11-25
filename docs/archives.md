# Archive processing

One `.zip`, `.tar`, `.tar.gz`, or `.tgz` source produces one immutable result.
Suffix matching is case-insensitive. Submit the full workspace-relative source
path. The processor retains the original archive snapshot and neither writes
members to the workspace nor starts jobs for them.

## Products and member provenance

The result lists `archive/inventory.json` as `archive-inventory`
(`application/json`). Download it from the product's `contentUrl`. The
Markdown evidence inventory links to it and to the table previews.

The inventory object has `schemaVersion: 1`, `format` (`zip`, `tar`, or `tar.gz`),
and `members` in container order. Each member has:

| Field | Meaning |
| --- | --- |
| `id` | Generated result-local ID, such as `member_0001` |
| `name` | Full member-relative display name, not a filesystem or URL target |
| `kind` | `file`, `directory`, or `other` |
| `bytes` | Container-declared member size |
| `status` | `processed`, `skipped`, or `failed` |
| `reason` | `null` on success; otherwise a disposition code |
| `tableIds` | The corresponding manifest table IDs; empty if no table was published |

Only regular CSV and TSV entries produce tables. They use UTF-8 (with optional
BOM) and the delimiter selected by suffix. As for standalone delimited input,
typed cells retain decoded lexemes as text and headers remain source rows.
CSV downloads contain raw, unsanitized values. Each table has the normal
typed-row endpoint and JSONL and CSV downloads. The table, its preview and data
products, and typed rows have `source.archiveMember: {id, name}`. Row locators inside each
member are member-local. The manifest's top-level `source` is the archive,
and `provenance.archive.format` identifies the container.

Directory entries are structural. Their reason is `directory`. Other entries
without tables have reason `non-regular`, `unsupported-format`, `empty-table`,
`encrypted`, `unsupported-compression`, `unsupported-zip-feature`, or
`invalid-delimited`. Only `invalid-delimited` has status `failed`. Nested
archives, Word, workbooks, and PDFs are inventory-only. The archive worker
does not call a model. Plain gzip, 7z, and RAR are not archive input formats.

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `succeeded` | Full inventory and at least one table. All non-directory entries yielded tables. |
| `partial` | Full inventory, with skipped or failed entries or no usable tables. Empty and inventory-only archives have a downloadable inventory. |
| `failed` | No sealed result. `INVALID_INPUT` means malformed or unsafe input, `UNSUPPORTED_FORMAT` means unsupported container features, and `RESOURCE_LIMIT` means exhausted budget. Processing does not alter the original. |

`coverage.unit` is `archive-member`. Coverage counts directory entries as
completed, skipped non-directory entries as unsupported, and invalid
delimited members as failed. Sealed enumeration is complete and
`notProcessed` is zero.

## Bounds and supported container features

| Limit | Maximum |
| --- | --- |
| Container entries | 128, including TAR extended headers |
| Member name | 512 UTF-8 bytes and 32 segments |
| ZIP metadata | 1 MiB central-directory read and 1 MiB of local headers |
| TAR extended metadata | 16 KiB |
| Expanded or consumed bytes | 64 MiB or the lower profile limit |
| A staged delimited input | 8 MiB |
| Temporary inputs plus output, or sealed manifest plus products | 64 MiB or the lower profile output limit |
| Serialized archive manifest | 384 KiB |
| Table rows | 250,000 across the archive or the lower profile row limit |
| Table columns | 256 per table or the lower profile column limit; 4,096 across the archive |

The configured input size, whole-job time, and worker memory limits also
apply. TAR consumption includes headers, padding, and skipped entry data.
Stored or deflated ZIP entries are checked against their declared sizes and
checksums. Encrypted ZIP entries and unsupported methods are inventoried by
declared size without verification of their payloads.

TAR supports ordinary file, directory, and non-regular headers, GNU long names
and link names, and PAX path, link, and select descriptive metadata. Sparse
layouts and unrecognized PAX extensions fail the job. A conventional TAR
leading `./` and a directory's final slash are not part of its display name.
Unsafe, duplicate, or ambiguous names fail the entire archive. No entry name
becomes a target path.
