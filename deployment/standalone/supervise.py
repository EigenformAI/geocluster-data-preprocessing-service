"""Opt-in standalone pair supervisor; excluded from the production wheel."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import signal
import sys
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def descriptor_instance(path):
    """Apply the CLI's bounded read policy without importing the service stack."""
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        value = json.loads(raw) if len(raw) <= 4 * 1024 * 1024 else None
        if (isinstance(value, dict) and value.get('version') == '2.0.19'
                and value.get('mode') == 'standalone'
                and isinstance(value.get('engineInstanceId'), str) and value['engineInstanceId']):
            return value['engineInstanceId']
    except (OSError, ValueError, RecursionError):
        pass
    return None


async def stop_child(name, process, waiter, seconds):
    if process is None:
        return True
    requested = process.returncode is None
    if process.returncode is None:
        print(f'Standalone: stopping {name}.', file=sys.stderr)
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(waiter), seconds)
    except TimeoutError:
        print(f'Standalone: {name} shutdown timed out; forcing exit, recovery required.', file=sys.stderr)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await waiter
        return False
    # Uvicorn re-raises the captured SIGTERM after its application teardown.
    # This is a requested process stop, not proof of a job's settlement state.
    return process.returncode == 0 or (requested and process.returncode == -signal.SIGTERM)


async def run_pair(owner_command, service_command, descriptor_path, stopped, *,
                   startup_seconds=300, shutdown_seconds=180, owner_shutdown_seconds=30):
    """Monitor the pair; settlement of the service precedes owner shutdown."""
    previous = descriptor_instance(descriptor_path)
    owner = service = owner_wait = service_wait = None
    stop_wait = asyncio.create_task(stopped.wait())
    result = 0
    try:
        owner = await asyncio.create_subprocess_exec(*owner_command, start_new_session=True)
        owner_wait = asyncio.create_task(owner.wait())
        deadline = asyncio.get_running_loop().time() + startup_seconds
        ready = False
        while True:
            if owner_wait.done():
                print('Standalone: engine owner exited before admission.', file=sys.stderr)
                result = 1
                break
            if stopped.is_set():
                break
            current = descriptor_instance(descriptor_path)
            if current is not None and current != previous:
                ready = True
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                print('Standalone: fresh engine descriptor startup timed out.', file=sys.stderr)
                result = 1
                break
            await asyncio.wait({owner_wait, stop_wait}, timeout=min(.05, remaining),
                               return_when=asyncio.FIRST_COMPLETED)
        if ready and not stopped.is_set() and not owner_wait.done():
            service = await asyncio.create_subprocess_exec(*service_command, start_new_session=True)
            service_wait = asyncio.create_task(service.wait())
            done, _ = await asyncio.wait({owner_wait, service_wait, stop_wait},
                                         return_when=asyncio.FIRST_COMPLETED)
            if owner_wait in done or service_wait in done:
                print('Standalone: supervised child exited unexpectedly; stopping the pair.', file=sys.stderr)
                result = 1
    finally:
        try:
            if not await stop_child('service', service, service_wait, shutdown_seconds):
                result = 1
        finally:
            try:
                if not await stop_child('engine owner', owner, owner_wait, owner_shutdown_seconds):
                    result = 1
            finally:
                stop_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_wait
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def health(port, token_env):
    """Check authenticated dependency readiness, not listener availability alone."""
    token = os.environ.get(token_env, '')
    if not token or any(not 33 <= ord(char) <= 126 for char in token):
        return 1
    request = Request(f'http://127.0.0.1:{port}/preprocessing/capabilities',
                      headers={'Authorization': 'Bearer ' + token})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        value = json.loads(raw) if len(raw) <= 4 * 1024 * 1024 else None
        return 0 if isinstance(value, dict) and value.get('engineAvailable') is True else 1
    except (OSError, ValueError, RecursionError):
        return 1


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError('Use a finite positive number of seconds.')
    return seconds


def parser():
    result = argparse.ArgumentParser(description='Opt-in standalone owner/service supervision (Linux).')
    commands = result.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Run the dedicated owner and production service together.')
    for name in ('data-root', 'workspace', 'engine-state', 'descriptor', 'engine-config', 'opencode-executable'):
        run.add_argument('--' + name, type=Path, required=True)
    run.add_argument('--credential-env', action='append', default=[])
    run.add_argument('--model', required=True)
    run.add_argument('--profile', type=Path)
    run.add_argument('--host', default='0.0.0.0')
    run.add_argument('--startup-seconds', type=positive_seconds, default=300)
    run.add_argument('--shutdown-seconds', type=positive_seconds, default=180)
    run.add_argument('--owner-shutdown-seconds', type=positive_seconds, default=30)
    check = commands.add_parser('health', help='Check authenticated service and engine readiness.')
    for command in (run, check):
        command.add_argument('--port', type=int, default=8765)
        command.add_argument('--token-env', default='PREPROCESSING_TOKEN')
    return result


async def run(args):
    stopped = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopped.set)
    owner = [sys.executable, '-m', 'preprocessing.engine_owner',
             '--state-dir', str(args.engine_state),
             '--projection', str(args.data_root / '.preprocessing/engine-lease.json'),
             '--descriptor', str(args.descriptor), '--opencode-executable', str(args.opencode_executable),
             '--engine-config', str(args.engine_config)]
    for name in args.credential_env:
        owner.extend(['--credential-env', name])
    service = [sys.executable, '-m', 'preprocessing.cli', 'serve',
               '--data-root', str(args.data_root), '--workspace', str(args.workspace),
               '--engine-descriptor', str(args.descriptor), '--model', args.model,
               '--host', args.host, '--port', str(args.port), '--token-env', args.token_env]
    if args.profile:
        service.extend(['--profile', str(args.profile)])
    return await run_pair(owner, service, args.descriptor, stopped,
                          startup_seconds=args.startup_seconds, shutdown_seconds=args.shutdown_seconds,
                          owner_shutdown_seconds=args.owner_shutdown_seconds)


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    if not 1 <= args.port <= 65535:
        arguments.error('--port must be between 1 and 65535')
    if args.command == 'health':
        return health(args.port, args.token_env)
    token = os.environ.get(args.token_env, '')
    if not token or any(not 33 <= ord(char) <= 126 for char in token):
        arguments.error('--token-env must name a nonempty printable ASCII token without spaces')
    try:
        return asyncio.run(run(args))
    except (OSError, ValueError):
        print('Standalone: launch failed; inspect paths, permissions, and child logs.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
