"""One short-lived parser, bounded IPC, no SQLite or provider credentials."""
import ctypes
import json
import os
import resource
import signal
import sys
import threading
import time
from pathlib import Path

from preprocessing.errors import ServiceError
from preprocessing.table_recovery import RECOVERY_EVENT_FIELDS, validate_page_range


class Context:
    def __init__(self, output, limit, response_bytes=256 * 1024, *, work_deadline=None, resume=None):
        self.output, self.limit = output, limit
        self.resume = resume
        # Coordinator and subprocess share the host monotonic clock. This value
        # is transient IPC only, never a reusable persisted timestamp.
        self.work_deadline = work_deadline
        # JSON escaping can expand every output byte sixfold, plus envelope metadata.
        self.ipc_bytes = response_bytes * 8 + 65536

    def work_remaining(self):
        return float('inf') if self.work_deadline is None else max(0, self.work_deadline - time.monotonic())

    def event(self, name, **data):
        if not isinstance(name, str) or not name.startswith('table.'):
            return
        compact = {key: value for key, value in data.items() if key in RECOVERY_EVENT_FIELDS
                   and type(value) in {str, int, float, bool, type(None)}}
        print(json.dumps({'type': 'event', 'name': name, 'data': compact}, allow_nan=False), flush=True)

    def check_output(self):
        total = 0
        for path in self.output.rglob('*'):
            if path == self.output / 'extraction.json':
                continue
            if path.is_file():
                total += path.stat().st_size
                if total > self.limit:
                    raise ServiceError('RESOURCE_LIMIT', 'Staged files exceed the output-byte limit.')

    def model(self, operation, input_refs):
        self.check_output()
        print(json.dumps({'type': 'model', 'operation': operation, 'inputRefs': input_refs}), flush=True)
        raw = sys.stdin.buffer.readline(self.ipc_bytes + 1)
        if not raw:
            raise ServiceError('RUNTIME_INTERRUPTED', 'Coordinator disconnected.')
        if len(raw) > self.ipc_bytes or not raw.endswith(b'\n'):
            raise ServiceError('RESOURCE_LIMIT', 'Model reply exceeds the configured IPC limit.')
        reply = json.loads(raw)
        if 'error' in reply:
            error = reply['error']
            raise ServiceError(error['code'], error['message'])
        return reply['result']


def main():
    # Linux parent-death signal closes the parser lifetime race; the inherited
    # owner lock also blocks replacement ownership until this process exits.
    parent = int(os.environ['PREPROCESSING_PARENT_PID'])
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
        sys.exit(70)
    request = json.loads(sys.stdin.buffer.readline(1024 * 1024))
    limits = request['profile']['limits']
    resource.setrlimit(resource.RLIMIT_AS, (limits['workerMemoryBytes'], limits['workerMemoryBytes']))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits['outputBytes'], limits['outputBytes']))
    resource.setrlimit(resource.RLIMIT_CPU, (limits['jobSeconds'], limits['jobSeconds'] + 1))
    from preprocessing.formats import get_adapter
    from preprocessing.indexes import build_indexes
    output = Path(request['output'])
    resume = None
    if request.get('resumePath'):
        resume = json.loads(Path(request['resumePath']).read_text())
    context = Context(output, limits['outputBytes'], limits['responseBytes'],
                      work_deadline=request.get('workDeadline'), resume=resume)
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(.05):
            try:
                context.check_output()
            except ServiceError as exc:
                print(json.dumps({'type': 'error', 'error': exc.as_dict()}), flush=True)
                os._exit(75)
            except OSError:
                # Main extraction reports I/O failures. A disappearing temporary
                # image is expected while the adapter advances to another page.
                time.sleep(.01)
    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()
    try:
        adapter = get_adapter(Path(request['snapshot']['filename']).suffix)
        if request['profile'].get('_tableRecoveryOverride') is not None and '.pdf' not in adapter.extensions:
            raise ServiceError('UNSUPPORTED_OPTION', 'tableRecovery overrides require a PDF source.')
        policy = request['profile']['options']['tableRecovery']
        if '.pdf' in adapter.extensions and policy['forcePages']:
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(request['snapshot']['path']) as document:
                validate_page_range(policy, len(document))
        extraction = adapter.extract(request['snapshot'], request['profile'], output, context)
        build_indexes(extraction, output, limits)
        context.check_output()
        stopped.set()
        monitor_thread.join()
        (output / 'extraction.json').write_text(json.dumps(extraction), encoding='utf-8')
        print(json.dumps({'type': 'complete'}), flush=True)
    except ServiceError as exc:
        print(json.dumps({'type': 'error', 'error': exc.as_dict()}), flush=True)
    except (MemoryError, OSError):
        print(json.dumps({'type': 'error', 'error': ServiceError('RESOURCE_LIMIT', 'Parser resource limit or I/O failure.').as_dict()}), flush=True)
    except Exception:  # noqa: BLE001 -- process boundary must not disclose input content
        print(json.dumps({'type': 'error', 'error': ServiceError('EXTRACTION_FAILED', 'Input could not be extracted.').as_dict()}), flush=True)
    finally:
        stopped.set()


if __name__ == '__main__':
    main()
