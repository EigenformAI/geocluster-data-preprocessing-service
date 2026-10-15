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
from preprocessing.dev import PrivateServer
async def main():
    root = Path(sys.argv[1])
    if sys.argv[3] == 'real':
        server = PrivateServer(root / 'engine', sys.argv[2], json.loads((root / 'config.json').read_text()), ['DEV_TEST_TOKEN'])
        await server.start()
        process = server.process
        lock_path = root / 'engine/server.lock'
    else:
        lock_path = root / 'owner.lock'
        owner = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        read_fd, lease = os.pipe()
        import preprocessing.engine_guardian
        guardian = Path(preprocessing.engine_guardian.__file__)
        process = await asyncio.create_subprocess_exec(sys.executable, '-u', str(guardian),
            str(read_fd), str(owner), '.6', sys.argv[2], pass_fds=(read_fd, owner),
            env={'TERM_MARKER': str(root / 'term')}, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        os.close(read_fd)
        assert await process.stdout.readline() == b'ready\n'
    print(json.dumps({'process': process.pid, 'lock': str(lock_path)}), flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
'''

FAKE = r'''
import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c',
    "import os,signal,time; from pathlib import Path; "
    "signal.signal(signal.SIGTERM, lambda *a: Path(os.environ['TERM_MARKER']).touch()); "
    "print('ready',flush=True); time.sleep(300)"],
    start_new_session=True, stdout=subprocess.PIPE)
child.stdout.readline()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('ready', flush=True)
time.sleep(300)
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
async def test_pinned_development_server_dies_with_coordinator(tmp_path):
    from test_dev import config
    (tmp_path / 'config.json').write_text(json.dumps(config()))
    await death_case(tmp_path, os.environ['PREPROCESSING_TEST_OPENCODE'], .013, stubborn=False)


async def death_case(root, executable, jitter, *, stubborn):
    coordinator = await asyncio.create_subprocess_exec(
        sys.executable, '-u', '-c', COORDINATOR, str(root), executable, 'fake' if stubborn else 'real',
        env={**os.environ, 'DEV_TEST_TOKEN': 'guardian-probe-only'},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    pids = []
    lock = None
    try:
        line = await asyncio.wait_for(coordinator.stdout.readline(), 30)
        assert line, (await coordinator.stderr.read()).decode()
        binding = json.loads(line)
        guardian_pid = binding['process']
        pids = descendants(coordinator.pid)
        assert len(pids) >= 2, pids
        if not stubborn:
            commands = [Path(f'/proc/{pid}/cmdline').read_bytes() for pid in pids]
            assert any(b'serve\x00--hostname' in command for command in commands), commands
        lock = os.open(binding['lock'], os.O_RDWR)
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
