"""Coordinator SIGKILL and retained flock evidence, using real OS processes."""
import asyncio
import fcntl
import json
import os
import signal
import sys
from pathlib import Path

import pytest

COORDINATOR = r'''
import asyncio, fcntl, json, os, sys
from pathlib import Path
from preprocessing.engine import OwnedEngine
async def main():
    root = Path(sys.argv[1])
    owner = os.open(root / 'owner.lock', os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    engine = OwnedEngine(root / 'engine', executable=sys.argv[2], cancel_seconds=.6,
                         environment={'TERM_MARKER': str(root / 'term')})
    engine.owner_fd = owner
    await engine._ensure_started('opencode/space-bunny-free')
    print(json.dumps({'process': engine.process.pid}), flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
'''

FAKE = r'''
import json, os, signal, subprocess, sys
if '--version' in sys.argv:
    print('opencode v2.0.19'); sys.exit()
child = subprocess.Popen([sys.executable, '-c',
    "import os,signal,time; from pathlib import Path; "
    "signal.signal(signal.SIGTERM, lambda *a: Path(os.environ['TERM_MARKER']).touch()); "
    "print('ready',flush=True); time.sleep(300)"],
    start_new_session=True, stdout=subprocess.PIPE)
child.stdout.readline()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    request=json.loads(line)
    if request.get('method') == 'initialize':
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{
            'protocolVersion':1,'agentInfo':{'version':'2.0.19'}}}),flush=True)
'''


def descendants(pid):
    result = []
    try:
        children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
    except FileNotFoundError:
        return result
    for child in map(int, children):
        result.append(child)
        result.extend(descendants(child))
    return result


def living(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] != 'Z'
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('jitter', [0, .017, .043])
async def test_coordinator_sigkill_fences_tree_and_retains_lock(tmp_path, jitter):
    executable = tmp_path / 'fake-opencode'
    executable.write_text(f'#!{sys.executable}\n' + FAKE)
    executable.chmod(0o700)
    await death_case(tmp_path, str(executable), jitter, stubborn=True)


@pytest.mark.skipif(not os.environ.get('PREPROCESSING_TEST_OPENCODE'), reason='explicit pinned CLI required')
async def test_pinned_acp_and_private_server_die_with_coordinator(tmp_path):
    await death_case(tmp_path, os.environ['PREPROCESSING_TEST_OPENCODE'], .013, stubborn=False)


async def death_case(root, executable, jitter, *, stubborn):
    coordinator = await asyncio.create_subprocess_exec(
        sys.executable, '-u', '-c', COORDINATOR, str(root), executable,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    pids = []
    lock = None
    try:
        line = await asyncio.wait_for(coordinator.stdout.readline(), 30)
        assert line, (await coordinator.stderr.read()).decode()
        guardian_pid = json.loads(line)['process']
        pids = descendants(coordinator.pid)
        assert len(pids) >= 2, pids
        if not stubborn:
            commands = [Path(f'/proc/{pid}/cmdline').read_bytes() for pid in pids]
            assert any(b'serve\x00--stdio' in command for command in commands), commands
        lock = os.open(root / 'owner.lock', os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        await asyncio.sleep(jitter)
        coordinator.kill()
        await coordinator.wait()
        if stubborn:
            async with asyncio.timeout(2):
                while not (root / 'term').exists():
                    await asyncio.sleep(.005)
            # Detached, TERM-resistant child is still alive: replacement must wait.
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with asyncio.timeout(5):
            while any(living(pid) for pid in pids):
                await asyncio.sleep(.01)
        assert all(not Path(f'/proc/{pid}').exists() for pid in pids if pid != guardian_pid)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if coordinator.returncode is None:
            coordinator.kill()
            await coordinator.wait()
        for pid in pids:
            if living(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if lock is not None:
            os.close(lock)
