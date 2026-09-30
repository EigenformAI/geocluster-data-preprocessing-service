"""Linux owned-process supervisor. Executed by absolute path with stdlib only.

The coordinator exclusively holds the lease pipe's write end. EOF (including
SIGKILL) fences descendants. The inherited flock descriptor stays open until all
children have been reaped. This process must never supervise a borrowed engine.
"""
import contextlib
import ctypes
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

LIBC = ctypes.CDLL(None, use_errno=True)


def _pidfd_open(pid):
    fd = LIBC.pidfd_open(pid, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_open failed')
    return fd


def _pidfd_signal(fd, sig):
    if LIBC.pidfd_send_signal(fd, sig, None, 0) < 0:
        raise OSError(ctypes.get_errno(), 'pidfd_send_signal failed')


def _children(pid):
    try:
        return [int(value) for value in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
    except FileNotFoundError:
        return []


def _descendants(pid):
    result = []
    for child in _children(pid):
        result.extend(_descendants(child))
        result.append(child)
    return result


def _signal_owned(sig):
    # Subreaping also captures descendants that start separate sessions/groups.
    # PIDfds prevent a scanned PID from later targeting an unrelated reused PID.
    for pid in _descendants(os.getpid()):
        try:
            fd = _pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            # Reconfirm ancestry after opening the identity-stable handle.
            if pid in _descendants(os.getpid()):
                with contextlib.suppress(ProcessLookupError):
                    _pidfd_signal(fd, sig)
        finally:
            os.close(fd)


def _reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def run(lease_fd, owner_fd, grace, command):
    # PR_SET_CHILD_SUBREAPER: orphaned grandchildren become our children, not PID 1.
    if LIBC.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'cannot establish engine subreaper')
    check_fd = _pidfd_open(os.getpid())
    try:
        _pidfd_signal(check_fd, 0)
    finally:
        os.close(check_fd)
    if owner_fd >= 0:
        os.fstat(owner_fd)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = None
    try:
        # Parent may have died before the guardian finished executing.
        if select.select([lease_fd], [], [], 0)[0] and not os.read(lease_fd, 1):
            return 0
        child = subprocess.Popen(command, start_new_session=True, close_fds=True)
        while not stopping and child.poll() is None:
            if select.select([lease_fd], [], [], .05)[0] and not os.read(lease_fd, 1):
                break
        return child.returncode or 0
    finally:
        # Never release the lock just because a deadline elapsed. Persistent local
        # failure leaves this guardian holding the lock and blocks replacement.
        deadline = time.monotonic() + grace
        while True:
            try:
                _signal_owned(signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL)
            except OSError:
                # A resource/permission failure must not release ownership early.
                pass
            if _reap():
                break
            time.sleep(.02)
        os.close(lease_fd)
        if owner_fd >= 0:
            os.close(owner_fd)


if __name__ == '__main__':
    raise SystemExit(run(int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), sys.argv[4:]))
