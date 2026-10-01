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


class Context:
    def __init__(self, output, limit, response_bytes=256 * 1024):
        self.output, self.limit = output, limit
        # JSON escaping can expand every output byte sixfold, plus envelope metadata.
        self.ipc_bytes = response_bytes * 8 + 65536

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
    context = Context(output, limits['outputBytes'], limits['responseBytes'])
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
