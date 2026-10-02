# Standalone deployment acceptance, 2026-10-01

Status: **DONE_WITH_CONCERNS**. Opt-in packaging, process lifecycle, installed-wheel
behavior, and deterministic container extraction are verified. Live OCR remains
blocked on explicit account provisioning. Host-published loopback access and the
shared-runtime incremental memory objective are not verified.

## Implemented deployment

`deployment/standalone/Dockerfile` builds the production wheel and installs its
locked dependencies plus the native OpenCode **2.0.19** binary. The final image
contains no Node, npm, uv, pytest, Ruff, or openpyxl. The standalone Compose profile
uses UID/GID 10001, init, persistent service and engine volumes, a private descriptor
tmpfs, read-only configuration/inputs, authentication, and pair restart supervision.

The supervisor waits for a fresh `engineInstanceId`, starts the service after owner
admission, and stops the service before the owner. Configurable defaults are
300 seconds for startup, 180 seconds for service shutdown, and 30 seconds for
owner shutdown. Forced exit requires subsequent recovery. The systemd example's
service stop allowance is corrected from 30 to 180 seconds.

The default wheel remains engine-independent: **81,735 bytes**, 20 application
modules and four `.dist-info` files, with ten direct production dependencies.
No deployment supervisor or container assets ship in it. Its SHA-256 is
`d882f358704fe0f343badc2afa658de873b7208d1e7dae95306abaff70bee298`.
The optional amd64 image occupies **406,772,024 bytes** and has identity
`sha256:51236ce16de8b01374974b0bcfebd05ad5a05f63a6b7c8bf5bb0c1a310ee8f2f`.
Disk image size is not resident memory.

## Maintained verification

Decision: new red-green lifecycle tests, relevant existing tests, direct installed
wheel and container execution, seeded subprocess jitter, and memory sampling.

- A no-op supervisor first failed the fresh-descriptor test with
  `pair must remain supervised until shutdown, not return success early`.
- Installed-wheel observation exposed Uvicorn's post-teardown SIGTERM exit. The
  regression test first failed with `expected 0, got 1`; the supervisor now accepts
  its requested signal exit while keeping job settlement a separate claim.
- `PREPROCESSING_TEST_OPENCODE=/tmp/opencode/preprocessing-cli-2.0.19/node_modules/@opencode/cli-linux-x64/bin/opencode uv run pytest -q`
  reported **269 passed in 56.82s**.
- After review added the forced-exit reaping assertion,
  `uv run pytest tests/preprocessing/test_standalone_deployment.py -q` reported
  **11 passed in 5.48s**. This includes 15 seeded stop/exit jitter cases.
- `uv run ruff check preprocessing tests/preprocessing scripts/probe_engine.py deployment/standalone/supervise.py`
  reported **All checks passed!**. `git diff --check` passed.

The static implementation review accepted the complete nine-path diff. Its three
non-blocking observations were corrected: document coordinated version updates,
document health-check overrides, and assert reaping on forced shutdown. Follow-up
review returned **accepted, no findings**. The final reviewed diff is 33,847 bytes,
SHA-256 `bd9a841c0291b7c177ab0d5c0264ab1aba27d1f40c5a368f974fe004481341fb`.

## Clean installation

A fresh Python 3.13 environment outside the checkout installed the wheel,
not an editable project, and 35 production dependencies exported from `uv.lock`.
All 36 installed distribution versions matched the lock. Wheel RECORD hashes,
sizes, module bytes, and declared dependencies were inspected. Both console help
commands returned 0. Dependency checking reported:

```text
Checked 36 packages in 1ms
All installed packages are compatible
```

The installed production CSV command connected to an external pinned owner and
returned `state: succeeded`, `documents.state: ready`, and `error: null`. Snapshot
bytes, six product hashes in sealed/shared copies, publication, remote/local status,
and reconciliation were verified. There were zero persisted model operations.

The disposable aid's final whole-workflow invocation aborted on a premature
catalog read. It also exposed the then-incorrect supervisor signal classification.
Its individual installation/native checks passed; the aid as a whole is not
reported as green. The corrected supervisor's actual image lifecycle passed below.

## Actual image and Compose execution

Build command:

```sh
docker build -q -f deployment/standalone/Dockerfile -t preprocessing-standalone:local .
```

The disposable runner invoked the actual Compose file and `standalone` profile:

```sh
uv run python /tmp/opencode/accept_standalone_container.py
```

Its decisive output was:

```text
PASS: image native CSV/workbook, scanned and dense PDFs with 12 image turns, HTTP/MCP, products/hashes, ordered stop, crash/replacement without replay
```

This used Docker **29.5.2**, Linux kernel
**6.6.87.2-microsoft-standard-WSL2**, amd64, and Docker's default seccomp profile.
No privileged or unconfined workaround was used. Actual startup, parsing, writer
loss, guardian cleanup, and restart exercised pidfds and parent-death behavior.

| Input | Outcome | Verified product downloads |
| --- | --- | ---: |
| CSV, 100,000 data rows × eight columns | Succeeded, publication ready; no inference. | 6 |
| Workbook, 10,000 rows × eight columns | Succeeded, publication ready; no inference. | 6 |
| Three scanned pages, 2,400 × 3,200 source images at 300 dpi | Succeeded; nine image operations. | 31 |
| One dense page, 4,000 × 5,400 source image at 150 dpi | Succeeded; three image operations. | 13 |

The runner checked source hashes and all **56** initial product download hashes
and sizes against their manifests. Scanned-page provider images were 1,152 × 1,536.
The dense page exercised the renderer near its configured 16-million-pixel limit;
the adapter subsequently reduced its provider images to 1,482 × 2,000. Source
renders and model payload dimensions are different measurements.

The deterministic provider returned fixed text and valid page-OCR JSON. This
establishes transport and persistence behavior, not visual transcription accuracy.
Authenticated HTTP, installed CLI status/reconcile, and real SDK MCP list/get
passed. A request without the bearer credential returned 401. After completed
turns the ownership projection was empty.

The runner then held one provider request and killed the exact service writer.
The supervisor stopped the owner, Compose replaced the pair, and the new
`engineInstanceId` differed. The previous job remained `interrupted` with
`RUNTIME_INTERRUPTED`. Exactly one held provider request was observed: **no replay**.
The previous PDF's 31 product downloads remained identical, and a new CSV job
succeeded. Ordered shutdown returned container exit **0**, with service shutdown
before owner shutdown. Local admin status passed after stopping the pair.

### Host network limitation

The Compose mapping is configured as `127.0.0.1:<port>:8765`. On this host, both
attempted published loopback connections timed out while private-container HTTP
and the in-container health check worked. The first runner timed out at startup.
Subsequent transport acceptance used the container's private bridge address.
Host-published loopback access is therefore **not accepted on this environment**.
The host firewall/network configuration was not changed by this task.

## Sampled standalone memory

The in-container sampler read process `smaps_rollup` approximately every 50 ms,
including init, pair supervisor, service, parser, external owner, guardian, and
engine. It excluded the observer's own PSS/RSS from process totals. Actual intervals
were longer than 50 ms; the largest dense-page gap was **86.493 ms**. A separate
container cgroup peak includes the observer, filesystem cache, and kernel costs.

Values are decimal MB. These are observations from one fixture run per envelope,
not a workload-independent memory guarantee:

| Phase | Median PSS | Peak PSS | Peak summed RSS |
| --- | ---: | ---: | ---: |
| Warm standalone idle | 345.757 | 345.765 | 379.372 |
| CSV | 344.634 | 365.614 | 406.729 |
| Workbook | 361.391 | 361.485 | 402.600 |
| Three scanned pages | 498.733 | 521.880 | 568.472 |
| Dense near-16MP render | 867.271 | **961.911** | 1,008.710 |
| 60-second idle window after dense processing | **680.078** | 806.198 | 839.897 |

The dense-page cgroup peak was **1,118,629,888 bytes**. Its 582 process samples
had no unreadable PIDs. One transient PID was unreadable in the scanned-page
window and another in the post-job idle window; these windows can underestimate
brief process totals. An earlier scanned-only run had 77 samples with no unreadable
PIDs and peak PSS **524,146,688 bytes**.

The full dedicated footprint exceeds a preferred 500 MB envelope, especially for
dense renders. Session deletion did not return the measured footprint to warm
idle within 60 seconds. Operators can lower the existing `pixels` limit or select
another profile; no default runtime dependency or speculative memory optimization
was introduced. The shared-runtime **incremental** 500,000,000-byte objective
needs paired measurements on that actual runtime. These standalone values do not
prove or disprove that shared-runtime objective. Five-minute retention and a
separate development-launcher full-footprint run were not measured.

## Live provider blocker

No approved API credential or isolated OpenAI subscription account is provisioned
for the controlled owner. The explicitly approved fallback
`muse-spark-1.3-contributor-free` appeared in the public Zen catalog. The actual
pinned Responses provider rejected its named anonymous credential:

```json
{"type":"provider.auth","message":"Invalid API key.","status":401}
```

The text prerequisite returned no text, so image and report OCR were not
attempted. Service evidence recorded the failed attempt and confirmed
`settled: true`, `sessionClosed: true`; owner shutdown returned 0 and withdrew
the descriptor. No paid fallback or personal auth-store import occurred.
Live Luna or Muse quality and provider-specific memory require explicit accepted
authentication. Catalog readiness alone does not establish inference readiness.

## Evidence locations

The final container's JSON report, memory samples, logs, input fixtures, manifests,
and shared document generations are retained at
`gdrive-lexikat:preprocessing-service-review-2026-10-01/standalone/`.
The source fixture content and model outputs are synthetic.

Local acceptance aids and outputs remain under `/tmp/opencode/`:

- `standalone-container-070ff2a7/`: final scanned/dense run.
- `standalone-container-b16f2eb2/`: earlier scanned-only run.
- `preprocessing-standalone-acceptance-20261001-v1/`: wheel/native evidence.
- `muse-free-verification-CoJn8hSX/`: redacted provider rejection and ledger evidence.
- `accept_standalone_container.py`: disposable container verification aid.
- `standalone-packaging-review.md`: complete static review packet.

These aids are not maintained commands. Read `deployment/standalone/README.md`
for the runnable deployment and `deployment/standalone/supervise.py` for lifecycle
behavior. Skim raw samples and manifests to audit the measurements and hashes.

## Remaining verification

Live OCR, published loopback routing on this host, arm64 execution, installed
systemd lifecycle, shared-runtime/chat/Fly integration, five-minute memory retention,
and paired incremental-runtime measurements remain outstanding.
