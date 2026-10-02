# Preprocessing implementation handoff

## Implemented scope

The standalone extraction service is implemented in the `preprocessing` Python
package. `uv` manages the environment and dependency lock. The `preprocess`
command exposes one-file processing, HTTP and Streamable HTTP MCP service mode,
status lookup, and publication reconciliation. The current extraction adapter is
`OpenCodeServerEngine`, using the pinned OpenCode 2.0.19 native HTTP API.

Pinned upstream extraction provenance remains at commit
`2c8a05864ce26b49ec88a78bfd0772e97e949958`. The legacy tracked `pipeline/`,
`analysis/`, four report-analysis skills, provider/PEACE scripts, legacy test,
and stale usage/environment guides are retired. Other untracked documentation
and evaluation evidence are preserved. The maintained package is `preprocessing`.

### Service behavior

- One SQLite owner admits idempotent jobs and launches one short-lived parser.
- Sources are copied, hashed, and committed before model dispatch. Reruns can use
  retained exact-byte snapshots.
- PDF, CSV, TSV, XLSX, and XLSM have explicit adapters. XLS remains unadvertised.
- CSV values and workbook typed cells preserve source spelling, precision,
  coordinates, formulas, caches, hidden-sheet state, and date-system metadata.
- PDF products retain physical pages, text, words, table bounds, figure images,
  OCR, and separate interpretations. Figure discovery is independently replaceable.
- Manifests, bounded indexes, complete downloads, previews, and `AGENTS.md`
  support downstream file-tool and MCP navigation.
- Result sealing precedes database visibility. Shared `documents/` publication
  is separately repairable without model calls.
- Cancellation waits for local settlement. Unknown engine settlement prevents
  further admission. Restart interrupts previous unfinished jobs after fencing.
- Production requires an explicit HTTP descriptor and explicit models. Native
  jobs check engine readiness but create no sessions or prompts.
- `Service` binds operation persistence before recovery. Candidate session/input
  IDs and dispatch intent are durable before HTTP mutations. SQLite remains the
  authority; `.preprocessing/engine-lease.json` is its fsynced cleanup projection.
- Startup examines all unsettled operations, including terminal-job unknown
  outcomes. Recovery does not replay prompts. `Service.reconcile_engine()` can
  safely unblock admission after settlement and readiness succeed.
- Production preprocessing owns no server. Standalone deployment has a dedicated
  external `StandaloneEngineOwner`; development has an explicit isolated launcher.
  Both launch ordinary HTTP `opencode serve`. The guardian handles private server
  process lifetime. Runtime deployment connects to its shared external server.

The service never dispatches through a report-analysis broker or direct provider
SDK. Standalone and development launchers validate a controlled extraction-only
configuration and forward only named credentials. Isolation of an already-running
runtime server remains the runtime owner's responsibility.

## Standalone packaging acceptance, 2026-10-01

Optional runnable packaging now lives in `deployment/standalone/`: a multi-stage
Dockerfile, allowlisted context, explicit Compose profile, deployment-only stdlib
pair supervisor, and operating instructions. The final image installs the locked
production dependencies and pinned native OpenCode 2.0.19 binary, with no Node,
npm, uv, or development dependencies. The default Python wheel still packages only
`preprocessing`; it gains no engine or container dependency. Runtime deployment
can omit the standalone packaging entirely.

The supervisor waits for a fresh `engineInstanceId`, starts the service after
owner admission, and stops the service before the owner. Timeout escalation is a
failed process shutdown requiring recovery, not manufactured job settlement.
Uvicorn's requested post-teardown SIGTERM is accepted. The systemd example now
allows 180 seconds for service shutdown instead of 30.

Decision: red-green lifecycle tests, existing real-pin tests, fresh-wheel inspection
and installation, actual image/Compose execution, subprocess jitter, and sampled
PSS/RSS/cgroup memory. The full suite reported **269 passed in 56.82s**. After the
review's forced-shutdown reaping assertion, deployment tests reported **11 passed
in 5.48s**; Ruff and `git diff --check` passed. Implementation review returned
**accepted, no findings** after three non-blocking corrections.

The image completed CSV, workbook, scanned PDF, and dense PDF jobs. Twelve image
turns used a deterministic loopback provider; 56 initial product downloads matched
manifest hashes and sizes. HTTP/MCP authentication, projection cleanup, writer
death, container replacement with a fresh engine identity, interrupted-job retention,
no prompt replay, preserved products, and ordered exit-0 shutdown passed.

Acceptance has explicit concerns. This host's published loopback port timed out;
transport checks used the private container address. The free Muse fallback
rejected an anonymous credential with **401 `Invalid API key`**, blocking live OCR.
Dense near-16MP rendering reached **961,910,784 bytes full standalone peak PSS**,
with **680,078,336 bytes median PSS** in the following 60-second idle window.
These dedicated measurements do not establish the shared-runtime incremental
500,000,000-byte target. Arm64 execution and installed systemd/Fly integration
remain unverified. Detailed evidence is in `docs/research/standalone-acceptance.md`.

## HTTP implementation verification, 2026-10-01

The implementation uses one pinned HTTP adapter in production, standalone, and
development modes. The production wheel declares `httpx` and registers
`preprocess` and `preprocess-engine-owner`. Production commands reread the private
descriptor at idle readiness, dispatch, and recovery boundaries.

Verification decision: meaningful red-green tests, existing behavior tests,
pinned-server protocol checks, jitter/process-death tests, and an isolated wheel
installation. The implementation review accepted the changes. Follow-up
corrections reject incomplete owner state, avoid unchanged idle-state writes,
and defer inactive-job recovery while another job is active.

- `PREPROCESSING_TEST_OPENCODE=/tmp/opencode/preprocessing-cli-2.0.19/node_modules/@opencode/cli-linux-x64/bin/opencode uv run pytest -q`
  reported **258 passed in 52.18s** after the three review follow-up tests.
- `uv run ruff check preprocessing tests/preprocessing scripts/probe_engine.py`
  reported **All checks passed!**. `git diff --check` passed.
- `uv build --wheel` produced the lean wheel. Installation into a fresh environment
  passed production imports, dependency metadata, wheel-content inspection, and
  both CLI help commands.
- The real pinned server completed text and image turns through a loopback-only
  deterministic OpenAI-compatible provider. SQLite contained text and usage before
  session deletion. The 3×3 image reached the provider as a data URI. The unrelated
  chat session, inbox, and transcript were preserved.
- The completed-turn test first failed because a nonempty message-page cursor was
  mistaken for evidence of more results. The pin emits a cursor anchor even on
  a short final page. The corrected adapter enforces its page bound by length.
- Owner restart evidence was consumed by a replacement service without replay.
  Thirty jitter cases checked predecessor-only fencing and late candidate IDs.
- The follow-up review accepted all three corrections. The initial dev cancellation
  finding was withdrawn after the reviewer traced confirmed private-process death.
  The complete review diff contains 402,268 bytes with SHA-256
  `bb7cb9f008b1528ec0bce51dce89797a13394c166d30216122b600826273b244`.

Live OCR quality, shared-runtime gateway wiring, deployed Fly and systemd behavior,
paired-chat latency, and the incremental 500 MB memory objective remain unverified.

## Earlier retirement-task verification, 2026-10-01

Decision: existing tests and direct observation. No new maintained tests were
added for deletions or the disposable live probe. Application code and packaging
were not edited by this retirement/documentation task.

- `uv run pytest tests/preprocessing -q` reported **4 failed, 156 passed, 6 skipped
  in 33.13s**. One API fixture starts `Service` without the required engine. Three
  guardian cases imported removed `OwnedEngine`. Those fixtures were subsequently
  migrated and the full suite passed as recorded above.
- `uv run ruff check preprocessing/cli.py tests/preprocessing/test_cli.py scripts/probe_engine.py`
  reported **All checks passed!**. `git diff --check` passed.
- Help commands passed for production `process` and `serve`, development `process`
  and `serve`, `status`, `reconcile`, the standalone owner module, and the HTTP probe.
- The actual pinned server accepted a development CSV run with explicit
  `deployment/vision` and the controlled example configuration. The result was
  `state: succeeded`, `documents.state: ready`, and model extraction was
  `not_applicable`. The explicit fixture credential supported catalog readiness;
  no inference was requested from the placeholder provider endpoint.
- A disposable direct command check launched the external owner module and ran
  production `process`, production `serve`, capabilities, remote `status` and
  `reconcile`, and local admin `status` and `reconcile`. All passed. Its decisive
  output included `PASS: external owner module + production process: succeeded,
  documents.ready` and `PASS: owner shutdown withdrew descriptor and settled
  private process lifetime`.
- A disposable mock-HTTP check ran the refreshed probe through real `Service`
  persistence. Text, image, pending cancellation, and reuse passed. Four SQLite
  evidence records were settled, and the projection cleared. Wrong text was
  rejected with exit 1. The check inspected durable ownership before session
  creation. This verifies probe mechanics, not live inference.
- `PREPROCESSING_TEST_OPENCODE=/tmp/opencode/preprocessing-cli-2.0.19/node_modules/.bin/opencode uv run pytest tests/preprocessing/test_engine_http_real.py -q`
  reported **1 passed in 1.24s**. That test parks an image without inference,
  cleans owned history, and preserves an unrelated session.

The disposable command and probe checks live under `/tmp/opencode/` as
`preprocessing-retirement-commands.py` and `preprocessing-retirement-verify.py`.
They are session verification aids, not maintained tests or deployed scripts.
That earlier task did not run a live provider OCR probe, Fly runtime integration,
concurrent-chat benchmark, OCR memory measurement, or fresh wheel-only installation.
The production dependency and wheel checks were completed afterward.

## Historical verification evidence

The following results describe the 2026-09-30 owned-ACP implementation and retained
dataset evaluations. They are historical evidence, not verification of the current
HTTP adapter, shared runtime, or OCR memory target.

Historical decision: red-green behavior tests, direct provider and dataset
execution, and static implementation review.

Historical full-suite command:

```sh
PREPROCESSING_TEST_OPENCODE=/tmp/opencode/preprocessing-cli-2.0.19/node_modules/.bin/opencode uv run pytest tests/preprocessing -q
```

Result: **136 passed in 32.88s**. Ruff reported **All checks passed!**.
`git diff --check` passed. `uv build --wheel` produced
`dist/data_preprocessing_service-0.1.0-py3-none-any.whl`.

Tests cover cross-surface replay, changed-source rejection, typed values, bounded
reads, publication failures, cancellation jitter, parser and real-engine owner
death, session persistence before prompting, and manifest integrity.

The final implementation review decision was **accepted**, with no findings.
Corrections addressed event-loop blocking, wide-table envelopes, native-job
cancellation with an unavailable engine, metadata output accounting, failed
snapshot cleanup, declared dependencies, and stale documentation.

### Dundas execution

| Input | Observed outcome |
| --- | --- |
| Dundas mineralised-intersections workbook | Five sheets completed; 1,153 cells checked against the source. |
| Mailman intersections CSV | 26 records completed; 208 fields checked against the source. |
| MtBelches report | Four physical pages completed; one coherent figure and two model operations. |
| Drilling shipment manifest | Two physical pages completed; four model operations. |

Native artifact checks covered 48 product hashes and 36 navigation links. PDF
checks covered 130 product hashes and 83 links. An independent downstream agent
followed the generated catalogs and guides to answer source, table, and figure
questions without reading the original files.

The first map run exceeded ten minutes because image fragments were treated as
independent figures. Caption-page grouping reduced page-2 candidates from 20 to
one. The corrected map run took 162.540 seconds; the shipment report took 23.942
seconds. Both used `opencode/space-bunny-free` without a paid fallback.

## Known extraction limits

The free model produced wrong map labels and a wrong geological interpretation.
OCR success means that the operation returned a transcription, not that every
character was verified. The native PDF table extractor missed a shipment body
row that remained available in page text. Table and figure recall are not proven
complete. Source images, page text, typed values, model provenance, and separate
interpretation products remain available for comparison.

Native figure discovery can miss uncaptioned vector graphics. Captions can
produce whole-page evidence rather than precise crops. Operators can override
geometry filters or replace the detector. Workbook pictures and charts are
outside initial coverage. Formula calculation and macro execution are excluded.

## Current HTTP ownership and remaining integration

The user selected runtime-owned standalone HTTP for both chat and extraction.
The runtime source is separate and outside this implementation scope. It must
replace its planned ACP ownership and supply one supported private descriptor.
Starting another engine or inspecting private process credentials is not a handoff.

These integrations belong to the runtime and frontend workstreams:

1. Shared standalone HTTP server ownership and HTTP chat integration. Deliver
   authentication, engine boot identity, canonical extraction directory, verified
   context, and truthful recovery evidence to preprocessing.
2. Supervise one preprocessing writer, fence it before abandoned-session cleanup,
   rescan the durable projection on startup, and retain uncertain cleanup IDs.
   Filter extraction sessions from chat history using the current projection.
3. Accepted-upload receipts and recoverable run-or-skip handoff.
4. Independent extraction activity, capacity, Machine lifetime, and shutdown wiring.
5. Shared-document immutability, private-state exclusions, and R2 transfer.
6. Browser upload and manual-processing flows, with desktop and mobile tests.
7. Shared-engine contention, interactive latency, restart behavior, and memory
   measurements against paired controls on the actual deployment.

The standalone owner is runnable as `preprocess-engine-owner` or
`python -m preprocessing.engine_owner`. Its private descriptor gates admission
after prior writer fencing and cleanup. The service refreshes descriptor evidence
without restarting its process. `POST /preprocessing/engine/reconcile` checks
known ownership and reopens admission only when settlement and readiness succeed.
The predecessor projection remains intact until owner evidence permits replacement.
Cleanup receipts preserve terminal evidence across owner-side deletion.

Historical idle service PSS was 48.56 MiB. The corrected map's peak sampled process
tree PSS was 534.96 MiB. Sampling can miss brief peaks; the shared cgroup peak was
not attributable to this service. No deployment memory allowance was agreed.
The current objective is less than 500,000,000 bytes of incremental runtime peak
PSS, including added engine and supervision growth. Historical ACP measurements
and unloaded/native HTTP observations do not prove that OCR target. Standalone
and development require separate full-footprint measurements. Full Fly integration
and representative live HTTP OCR memory evidence remain unverified.

Usage records retain operation and session correlation and observed counters.
They remain incomplete provider-accounting evidence. Detailed billing, pricing,
Prefect orchestration, and a compiled coordinator are not implemented.

## Reading order

Read `README.md` for commands, `preprocessing/contracts.py` for adapter contracts,
and `preprocessing/service.py` for job ownership. Read `docs/engine.md` and
`preprocessing/engine_owner.py` before implementing runtime HTTP ownership and
projection cleanup. Skim the format modules and import log to locate
extraction algorithms and their provenance. Dataset measurements and quality
observations are recorded in `docs/research/dundas-dogfood.md`.

The four inspected published generations are copied to
`gdrive-lexikat:preprocessing-service-review-2026-09-30/`, in `map`, `shipment`,
`workbook`, and `csv` directories. These review copies preserve the exact
dogfood generations, including their recorded quality limitations. Later
manifest-formatting and preview improvements do not rewrite those generations.
