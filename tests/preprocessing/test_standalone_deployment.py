"""Observable lifecycle contracts for the optional deployment-only supervisor."""
import asyncio
import importlib.util
import json
import os
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'standalone_supervisor', Path(__file__).resolve().parents[2] / 'deployment/standalone/supervise.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)

CHILD = '''import json, os, signal, sys, time
from pathlib import Path
name, directory, delay, ignore, exit_after = sys.argv[1:]
root = Path(directory)
delay, exit_after = float(delay), float(exit_after)
stopped = False
def stop(signum, frame):
    global stopped
    (root / (name + '.term')).touch()
    if ignore != 'yes':
        stopped = True
signal.signal(signal.SIGTERM, stop)
(root / (name + '.started')).touch()
(root / (name + '.pid')).write_text(str(os.getpid()))
began = time.monotonic()
if name == 'owner':
    while not stopped and time.monotonic() - began < delay:
        time.sleep(.005)
    if not stopped:
        temporary = root / 'descriptor.tmp'
        temporary.write_text(json.dumps({'engineInstanceId': 'fresh', 'version': '2.0.19', 'mode': 'standalone'}))
        temporary.replace(root / 'descriptor.json')
else:
    assert json.loads((root / 'descriptor.json').read_text())['engineInstanceId'] == 'fresh'
(root / (name + '.ready')).touch()
while not stopped:
    if exit_after >= 0 and time.monotonic() - began >= exit_after:
        (root / (name + '.done')).touch()
        sys.exit(0)
    time.sleep(.005)
if name == 'service':
    time.sleep(delay)
if name == 'owner' and (root / 'service.started').exists():
    assert (root / 'service.done').exists(), 'owner stopped before service settled'
(root / (name + '.done')).touch()
if ignore == 'reraise':
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
'''


def command(name, root, *, delay=0, ignore=False, exit_after=-1, reraise=False):
    return [sys.executable, '-c', CHILD, name, str(root), str(delay),
            'reraise' if reraise else 'yes' if ignore else 'no', str(exit_after)]


async def until(path):
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(.005)


def assert_reaped(root):
    for name in ('owner', 'service'):
        if (root / (name + '.pid')).exists():
            with pytest.raises(ProcessLookupError):
                os.kill(int((root / (name + '.pid')).read_text()), 0)


async def start_pair(root, stopped, *, owner_delay=0, service_delay=0, **kwargs):
    return await supervisor.run_pair(
        command('owner', root, delay=owner_delay), command('service', root, delay=service_delay),
        root / 'descriptor.json', stopped, startup_seconds=2, shutdown_seconds=1,
        owner_shutdown_seconds=1, **kwargs)


async def test_pair_waits_for_fresh_descriptor_and_settles_service_before_owner(tmp_path):
    (tmp_path / 'descriptor.json').write_text(json.dumps(
        {'engineInstanceId': 'stale', 'version': '2.0.19', 'mode': 'standalone'}))
    stopped = asyncio.Event()
    task = asyncio.create_task(start_pair(tmp_path, stopped, owner_delay=.2, service_delay=.1))
    try:
        await asyncio.sleep(.03)
        assert not task.done(), 'pair must remain supervised until shutdown, not return success early'
        await until(tmp_path / 'owner.started')
        assert not (tmp_path / 'service.started').exists(), 'stale descriptor must not admit service'
        await until(tmp_path / 'service.ready')
        stopped.set()
        assert await asyncio.wait_for(task, 3) == 0
        assert (tmp_path / 'service.done').exists() and (tmp_path / 'owner.done').exists()
        assert_reaped(tmp_path)
    finally:
        stopped.set()
        await asyncio.wait_for(task, 3)


async def test_stop_during_startup_never_launches_service(tmp_path):
    stopped = asyncio.Event()
    task = asyncio.create_task(start_pair(tmp_path, stopped, owner_delay=1))
    try:
        await until(tmp_path / 'owner.started')
        stopped.set()
        assert await asyncio.wait_for(task, 3) == 0
        assert (tmp_path / 'owner.done').exists()
        assert not (tmp_path / 'service.started').exists()
    finally:
        stopped.set()
        await asyncio.wait_for(task, 3)


async def test_requested_sigterm_reraised_after_teardown_is_an_orderly_stop(tmp_path):
    """Uvicorn restores and re-raises SIGTERM after application shutdown."""
    stopped = asyncio.Event()
    task = asyncio.create_task(supervisor.run_pair(
        command('owner', tmp_path), command('service', tmp_path, reraise=True),
        tmp_path / 'descriptor.json', stopped, startup_seconds=2,
        shutdown_seconds=1, owner_shutdown_seconds=1))
    try:
        await until(tmp_path / 'service.ready')
        stopped.set()
        assert await asyncio.wait_for(task, 3) == 0
        assert (tmp_path / 'service.done').exists() and (tmp_path / 'owner.done').exists()
        assert_reaped(tmp_path)
    finally:
        stopped.set()
        await asyncio.wait_for(task, 3)


@pytest.mark.parametrize('exiting', ['owner', 'service'])
async def test_spontaneous_clean_child_exit_is_pair_failure(tmp_path, exiting):
    result = await supervisor.run_pair(
        command('owner', tmp_path, exit_after=.15 if exiting == 'owner' else -1),
        command('service', tmp_path, exit_after=.15 if exiting == 'service' else -1),
        tmp_path / 'descriptor.json', asyncio.Event(), startup_seconds=2,
        shutdown_seconds=1, owner_shutdown_seconds=1)
    assert result == 1, 'a spontaneous exit 0 must not masquerade as an operator-requested stop'
    assert (tmp_path / 'service.done').exists() if exiting == 'owner' else (tmp_path / 'owner.done').exists()


async def test_startup_timeout_stops_owner_and_reports_failure(tmp_path):
    result = await supervisor.run_pair(
        command('owner', tmp_path, delay=10), command('service', tmp_path),
        tmp_path / 'descriptor.json', asyncio.Event(), startup_seconds=.1,
        shutdown_seconds=1, owner_shutdown_seconds=1)
    assert result == 1
    assert (tmp_path / 'owner.done').exists()
    assert not (tmp_path / 'service.started').exists()


async def test_service_launch_failure_still_stops_owner(tmp_path):
    with pytest.raises(FileNotFoundError):
        await supervisor.run_pair(
            command('owner', tmp_path), [str(tmp_path / 'missing-program')],
            tmp_path / 'descriptor.json', asyncio.Event(), startup_seconds=2,
            shutdown_seconds=1, owner_shutdown_seconds=1)
    assert (tmp_path / 'owner.done').exists()


async def test_timeout_escalation_reports_failure_and_reaps_the_service(tmp_path, capsys):
    stopped = asyncio.Event()
    task = asyncio.create_task(supervisor.run_pair(
        command('owner', tmp_path), command('service', tmp_path, ignore=True),
        tmp_path / 'descriptor.json', stopped, startup_seconds=2,
        shutdown_seconds=.05, owner_shutdown_seconds=1))
    try:
        await until(tmp_path / 'service.ready')
        stopped.set()
        assert await asyncio.wait_for(task, 3) == 1
        assert (tmp_path / 'service.term').exists() and not (tmp_path / 'service.done').exists()
        assert 'service shutdown timed out' in capsys.readouterr().err
        assert (tmp_path / 'owner.term').exists()
        assert_reaped(tmp_path)
    finally:
        stopped.set()
        await asyncio.wait_for(task, 3)


async def test_jittered_stop_and_child_exit_always_settle_the_pair(tmp_path):
    rng = random.Random(731)
    for case in range(15):
        root = tmp_path / str(case)
        root.mkdir()
        stopped = asyncio.Event()
        service_exit = rng.uniform(.05, .15)
        task = asyncio.create_task(supervisor.run_pair(
            command('owner', root), command('service', root, exit_after=service_exit),
            root / 'descriptor.json', stopped, startup_seconds=2,
            shutdown_seconds=1, owner_shutdown_seconds=1))
        try:
            await until(root / 'service.ready')
            await asyncio.sleep(rng.uniform(.03, .18))
            stopped.set()
            assert await asyncio.wait_for(task, 3) in {0, 1}
            assert (root / 'owner.done').exists()
            assert_reaped(root)
        finally:
            stopped.set()
            await asyncio.wait_for(task, 3)


@pytest.mark.parametrize('ready', [False, True])
def test_health_requires_authenticated_engine_readiness(monkeypatch, ready):
    monkeypatch.setenv('DEPLOYMENT_TEST_TOKEN', 'fixture-token')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/preprocessing/capabilities'
            assert self.headers['Authorization'] == 'Bearer fixture-token'
            body = json.dumps({'engineAvailable': ready}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert supervisor.health(server.server_port, 'DEPLOYMENT_TEST_TOKEN') == (0 if ready else 1)
        assert supervisor.health(server.server_port, 'MISSING_DEPLOYMENT_TEST_TOKEN') == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
