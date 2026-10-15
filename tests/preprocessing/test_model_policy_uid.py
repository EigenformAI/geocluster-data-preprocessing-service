"""Real Linux traversal permissions; no mocked filesystem metadata or reader code."""
import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from preprocessing import model_policy

# A private chroot avoids changing /run or relaxing /tmp's writable-directory
# rejection. Import the exact processor module selected by pytest before chroot,
# then drop supplementary groups and both IDs in the reader child. The root
# parent stays outside chroot solely to own and clean up the temporary fixture.
_PROBE = r'''
import errno
import json
import os
import runpy
import shutil
import sys
import tempfile
from pathlib import Path

module_path = Path(sys.argv[1]).resolve()
read_trusted_json = runpy.run_path(str(module_path))['read_trusted_json']
jail = Path(tempfile.mkdtemp(prefix='uid996-reader-', dir=sys.argv[2]))

def probe(path):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.chroot(jail)
            os.chdir('/')
            os.setgroups([])
            os.setgid(996)
            os.setuid(996)
            result = {'uid': os.geteuid(), 'gid': os.getegid(), 'groups': os.getgroups()}
            # Confirm the fixture requires execute-only traversal: O_RDONLY must
            # fail at this protected ancestor for this actual unprivileged UID.
            try:
                fd = os.open('/run/runtime/preprocessing', os.O_RDONLY | os.O_DIRECTORY)
            except OSError as error:
                result['directoryReadErrno'] = error.errno
            else:
                os.close(fd)
                result['directoryReadErrno'] = None
            try:
                result['value'] = read_trusted_json(path)
            except ValueError:
                result['value'] = None
            os.write(write_fd, json.dumps(result).encode())
        finally:
            os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        raw = os.read(read_fd, 65536)
    finally:
        os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert status == 0, status
    return json.loads(raw)

try:
    directory = jail / 'run/runtime/preprocessing'
    directory.mkdir(parents=True)
    for ancestor in (jail, jail / 'run', jail / 'run/runtime', directory):
        os.chown(ancestor, 0, 996)
        ancestor.chmod(0o710)
    policy = directory / 'model-policy.json'
    policy.write_text('{"allowed":true}')
    os.chown(policy, 0, 996)
    policy.chmod(0o640)
    results = {'safe': probe('/run/runtime/preprocessing/model-policy.json')}

    runtime = jail / 'run/runtime'
    runtime.chmod(0o730)
    results['groupWritableAncestor'] = probe('/run/runtime/preprocessing/model-policy.json')
    runtime.chmod(0o712)
    results['worldWritableAncestor'] = probe('/run/runtime/preprocessing/model-policy.json')
    runtime.chmod(0o710)

    policy.chmod(0o660)
    results['writableFile'] = probe('/run/runtime/preprocessing/model-policy.json')
    policy.chmod(0o640)
    link = directory / 'linked-policy.json'
    link.symlink_to('model-policy.json')
    results['symlinkFile'] = probe('/run/runtime/preprocessing/linked-policy.json')
    (jail / 'run/linked-runtime').symlink_to('runtime', target_is_directory=True)
    results['symlinkAncestor'] = probe('/run/linked-runtime/preprocessing/model-policy.json')

    assert all(ancestor.stat().st_mode & 0o777 == 0o710
               for ancestor in (jail, jail / 'run', runtime, directory))
    assert policy.stat().st_mode & 0o777 == 0o640
    print(json.dumps({'module': str(module_path), 'cases': results}))
finally:
    shutil.rmtree(jail)
'''


def test_uid996_reads_trusted_0710_ancestors_0640_file_and_rejects_unsafe_handoffs(tmp_path):
    if sys.platform != 'linux' or not hasattr(os, 'O_PATH'):
        pytest.skip('Linux O_PATH and real UID/GID transitions are required')
    command = []
    if os.geteuid() != 0:
        sudo = shutil.which('sudo')
        if sudo is None or subprocess.run([sudo, '-n', 'true'], capture_output=True, timeout=5, check=False).returncode:
            pytest.skip('root or passwordless sudo is required for the real UID996 fixture')
        command = [sudo, '-n']
    module = Path(model_policy.__file__).resolve()
    result = subprocess.run([*command, sys.executable, '-I', '-c', _PROBE, str(module), str(tmp_path)],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['module'] == str(module)
    for case in report['cases'].values():
        assert (case['uid'], case['gid'], case['groups']) == (996, 996, [])
        assert case['directoryReadErrno'] == errno.EACCES
    assert report['cases']['safe']['value'] == {'allowed': True}
    for name in ('groupWritableAncestor', 'worldWritableAncestor', 'writableFile', 'symlinkFile', 'symlinkAncestor'):
        assert report['cases'][name]['value'] is None, name
