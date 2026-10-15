import json
import os
import time

import pytest


def test_partial_owner_state_requires_explicit_repair(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    root = tmp_path / 'owner'
    root.mkdir(mode=0o700)
    state = root / 'owner.json'
    state.write_text(json.dumps({'engineInstanceId': 'possibly-live-engine', 'entries': []}))
    before = state.read_bytes()
    with pytest.raises(ValueError, match='state'):
        StandaloneEngineOwner(root, tmp_path / 'lease.json')
    assert state.read_bytes() == before


async def test_owner_publishes_safe_descriptor_with_zero_enabled_candidates(tmp_path):
    import asyncio
    import subprocess
    import sys
    from pathlib import Path

    from test_dev import config, fake_opencode
    executable = fake_opencode(tmp_path, '', None, empty_catalog=True)
    engine_config = tmp_path / 'config.json'
    engine_config.write_text(json.dumps(config()))
    descriptor = tmp_path / 'descriptor.json'
    owner = subprocess.Popen([sys.executable, '-m', 'preprocessing.engine_owner',  # noqa: ASYNC220 -- supervised owner lifecycle
        '--state-dir', str(tmp_path / 'owner'), '--projection', str(tmp_path / 'absent-lease.json'),
        '--descriptor', str(descriptor), '--opencode-executable', str(executable),
        '--engine-config', str(engine_config), '--credential-env', 'DEV_TEST_TOKEN'],
        env={**os.environ, 'DEV_TEST_TOKEN': 'private-token'}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        async with asyncio.timeout(6):
            while not descriptor.exists():
                assert owner.poll() is None
                await asyncio.sleep(.02)
        observed = json.loads(descriptor.read_text())
        assert observed['version'] is None
        assert observed['contextVerified'] is True
        assert observed['recovery']['extractionExcluded'] is True
        assert Path(observed['directory']).is_absolute()
        assert owner.poll() is None
    finally:
        owner.terminate()
        await asyncio.to_thread(owner.wait, timeout=6)
        owner.stderr.close()
    assert not descriptor.exists()


async def test_unchanged_owner_poll_does_not_rewrite_durable_state(tmp_path, monkeypatch):
    import preprocessing.engine_owner as module
    writes = []
    atomic = module.atomic_json

    def record(path, value):
        writes.append(path)
        atomic(path, value)

    monkeypatch.setattr(module, 'atomic_json', record)
    owner = module.StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json')
    owner.save()
    count = len(writes)
    for _ in range(3):
        assert await owner.reconcile()
    assert len(writes) == count
    owner.state['fencedGenerations'].append('old-generation')
    owner.save()
    assert len(writes) == count + 1


def binding(directory):
    return {'id': 'operation_owned', 'jobId': 'job_owned', 'generation': 'old',
            'engineInstanceId': 'surviving-engine', 'sessionId': 'ses_owned', 'inputId': 'msg_owned',
            'requestHash': 'a' * 64, 'agent': 'preprocessing',
            'model': {'providerID': 'fixture', 'id': 'vision'},
            'location': {'directory': str(directory)}, 'dispatchIntent': True, 'admissionAcknowledged': True}


class OwnedHTTP:
    def __init__(self, entry):
        self.entry = entry
        self.events = []
        self.deleted = False
        self.outage = False

    async def __call__(self, method, path, body=None):
        from preprocessing.engine_evidence import operation_marker
        self.events.append((method, path))
        if self.outage:
            raise OSError('transient transport outage')
        if path.startswith('/api/session?'):
            return 200, {'data': [], 'cursor': {}}
        if path.endswith('/inbox'):
            return 200, {'data': []}
        if '/message?' in path:
            return 200, {'data': [
                {'id': self.entry['inputId'], 'type': 'user', 'metadata': {'requestHash': self.entry['requestHash']}},
                {'id': 'msg_answer', 'type': 'assistant', 'agent': self.entry['agent'], 'model': self.entry['model'],
                 'content': [{'type': 'text', 'text': 'captured transcription'}], 'time': {'completed': 2},
                 'finish': 'stop', 'tokens': {'input': 17, 'output': 4}, 'cost': .001},
                {'id': 'msg_idle', 'type': 'idle', 'outcome': 'succeeded'},
            ], 'cursor': {}}
        if path.endswith('/interrupt'):
            return 200, {'interrupted': True}
        if path.endswith('/wait'):
            return 204, None
        if method == 'DELETE':
            self.deleted = True
            return 204, None
        if self.deleted:
            return 404, None
        return 200, {'data': {'id': self.entry['sessionId'], 'agent': self.entry['agent'],
                              'model': self.entry['model'], 'location': self.entry['location'],
                              'permissions': [{'action': '*', 'resource': '*', 'effect': 'allow'},
                                              {'action': '*', 'resource': '*', 'effect': 'deny'}],
                               'metadata': {'preprocessing': operation_marker(self.entry)}}}


def projection(entry, expires_at=0, generation='old', writer=None):
    return {'generation': generation, 'expiresAt': expires_at, 'directory': entry['location']['directory'],
            'writer': writer or {'pid': 123, 'starttime': '456'},
            'engineInstanceId': entry['engineInstanceId'], 'entries': [entry]}


def test_history_filter_rereads_atomic_projection(tmp_path):
    from preprocessing.engine_owner import filter_history
    path = tmp_path / 'lease.json'
    path.write_text(json.dumps({'entries': [{'sessionId': 'ses_owned'}]}))
    history = [{'id': 'ses_owned'}, {'id': 'ses_chat'}]
    assert filter_history(history, path) == [{'id': 'ses_chat'}]
    path.write_text(json.dumps({'entries': [{'sessionId': 'ses_chat'}]}))
    assert filter_history(history, path) == [{'id': 'ses_owned'}]


@pytest.mark.asyncio
async def test_fencing_precedes_cleanup_and_absence_remains_pending(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    events = []

    async def fence(writer):
        events.append('fence')
        return True

    async def request(method, path, body=None):
        events.append((method, path))
        return 404, None

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json',
                                  request=request, fence_writer=fence)
    owner.state['engineInstanceId'] = 'boot-current'
    projection = {'generation': 'old', 'engineInstanceId': 'boot-current',
                  'expiresAt': 0, 'writer': {'pid': 123, 'starttime': '456'},
                  'directory': str(tmp_path),
                  'entries': [{**binding(tmp_path), 'engineInstanceId': 'boot-current'}]}
    owner.projection_path.write_text(json.dumps(projection))
    assert not await owner.reconcile()
    assert events[0] == 'fence'
    assert 'old' in owner.state['fencedGenerations']
    assert 'old' in owner.state['pendingGenerations']
    assert not any(method == 'DELETE' for method, *_ in events[1:])
    # Reinspection is required: a single absent read is not a permanent tombstone.
    before = len(events)
    assert not await owner.reconcile()
    assert len(events) > before


@pytest.mark.asyncio
async def test_unfenced_writer_never_receives_cleanup(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    events = []

    async def fence(writer):
        return False

    async def request(*args):
        events.append(args)
        return 200, {}

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json',
                                  request=request, fence_writer=fence)
    owner.projection_path.write_text(json.dumps({
        'generation': 'old', 'expiresAt': 0, 'writer': {'pid': 123, 'starttime': '456'},
        'engineInstanceId': 'old-engine', 'entries': [{'sessionId': 'ses_owned', 'inputId': 'msg_owned'}],
    }))
    assert not await owner.reconcile()
    assert events == []
    assert owner.state['fencedGenerations'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('unexpected_child', [False, True])
async def test_only_terminal_owned_history_can_be_deleted(tmp_path, unexpected_child):
    from preprocessing.engine_owner import StandaloneEngineOwner
    events = []
    deleted = False

    async def fence(writer):
        return True

    async def request(method, path, body=None):
        nonlocal deleted
        events.append((method, path))
        if path.startswith('/api/session?'):
            return 200, {'data': [{'id': 'ses_chat'}] if unexpected_child else [], 'cursor': {}}
        if path.endswith('/inbox'):
            return 200, {'data': []}
        if '/message?' in path:
            return 200, {'data': [{'id': 'msg_owned', 'type': 'user', 'metadata': {'requestHash': 'a' * 64}},
                                  {'id': 'msg_idle', 'type': 'idle', 'outcome': 'interrupted'}]}
        if path.endswith('/interrupt'):
            return 200, {'interrupted': True}
        if method == 'DELETE':
            deleted = True
            return 204, None
        if deleted:
            return 404, None
        return 200, {'data': {'id': 'ses_owned', 'agent': 'preprocessing',
                              'location': {'directory': str(tmp_path)},
                              'model': {'providerID': 'fixture', 'id': 'vision'},
                              'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}],
                              'metadata': {'preprocessing': {'operationId': 'operation_owned', 'jobId': 'job_owned',
                                                            'requestHash': 'a' * 64, 'engineInstanceId': 'surviving-engine',
                                                            'inputId': 'msg_owned', 'generation': 'old'}}}}

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json',
                                  request=request, fence_writer=fence)
    owner.projection_path.write_text(json.dumps({
        'generation': 'old', 'expiresAt': 0, 'directory': str(tmp_path),
        'writer': {'pid': 123, 'starttime': '456'}, 'engineInstanceId': 'surviving-engine',
        'entries': [binding(tmp_path)],
    }))
    assert await owner.reconcile() is not unexpected_child
    assert deleted is not unexpected_child
    assert not any('resume=' in path for _, path in events)
    if not unexpected_child:
        assert owner.recovery()['cleanupReceipts'][0]['evidence']['outcome'] == 'interrupted'


@pytest.mark.asyncio
async def test_dead_unexpired_writer_receipt_survives_restart_and_prunes_after_reconciliation(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)
    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=http)
    owner.projection_path.write_text(json.dumps(projection(entry, time.time() + 3600)))
    assert await owner.reconcile()
    receipt, = owner.recovery()['cleanupReceipts']
    assert receipt == {'operationId': entry['id'], **{key: entry[key] for key in
                       ('jobId', 'generation', 'engineInstanceId', 'sessionId', 'inputId', 'requestHash')},
                       'evidence': {'text': 'captured transcription', 'usage': {'tokens': {'input': 17, 'output': 4}, 'cost': .001},
                                    'outcome': 'succeeded', 'inputId': 'msg_owned'},
                       'settlement': {'settled': True, 'sessionClosed': True, 'sessionId': 'ses_owned', 'remoteOutcome': 'unknown'}}
    restarted = StandaloneEngineOwner(owner.root, owner.projection_path, request=http)
    assert restarted.recovery()['cleanupReceipts'] == [receipt]
    assert await restarted.reconcile()
    assert not any(path.endswith('/prompt') for _, path in http.events)
    renewed = projection(entry, time.time() + 3600, generation='replacement')
    from preprocessing.engine_owner import process_identity
    renewed['writer'] = {'pid': os.getpid(), 'starttime': process_identity(os.getpid())}
    renewed['entries'] = []
    restarted.projection_path.write_text(json.dumps(renewed))
    assert await restarted.reconcile()
    assert restarted.recovery()['cleanupReceipts'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['operationId', 'jobId', 'generation', 'engineInstanceId', 'inputId', 'requestHash'])
async def test_full_marker_mismatch_prevents_owner_mutations(tmp_path, field):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)

    async def request(method, path, body=None):
        status, response = await http(method, path, body)
        if path == '/api/session/ses_owned':
            response['data']['metadata']['preprocessing'][field] = 'wrong'
        return status, response

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=request)
    owner.projection_path.write_text(json.dumps(projection(entry)))
    assert not await owner.reconcile()
    assert all(method == 'GET' for method, _ in http.events)
    assert owner.recovery().get('cleanupReceipts', []) == []


@pytest.mark.parametrize('invalid', ['selection', 'session', 'metadata'])
async def test_invalid_session_binding_retains_owner_responsibility_without_receipt(tmp_path, invalid):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)

    async def request(method, path, body=None):
        status, response = await http(method, path, body)
        if path == '/api/session/ses_owned':
            if invalid == 'selection':
                response['data']['model'] = {'providerID': 'wrong', 'id': 'wrong'}
            elif invalid == 'session':
                response['data'] = []
            else:
                response['data']['metadata'] = []
        return status, response

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=request)
    owner.projection_path.write_text(json.dumps(projection(entry)))
    assert not await owner.reconcile()
    assert owner.state['pendingGenerations']['old']['entries'] == [entry]
    assert owner.recovery()['cleanupReceipts'] == []
    assert all(method == 'GET' for method, _ in http.events)


@pytest.mark.asyncio
async def test_child_appearing_during_interruption_prevents_recursive_deletion(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)
    checks = 0

    async def request(method, path, body=None):
        nonlocal checks
        if path.startswith('/api/session?'):
            checks += 1
            if checks > 1:
                return 200, {'data': [{'id': 'ses_unexpected_child'}], 'cursor': {}}
        return await http(method, path, body)

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=request)
    owner.projection_path.write_text(json.dumps(projection(entry)))
    assert not await owner.reconcile()
    assert not http.deleted
    assert owner.recovery()['cleanupReceipts'] == []


@pytest.mark.asyncio
async def test_transient_owner_outage_retains_candidates_for_retry(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)
    http.outage = True
    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=http)
    owner.projection_path.write_text(json.dumps(projection(entry)))
    assert not await owner.reconcile()
    assert 'old' in owner.state['pendingGenerations']
    http.outage = False
    assert await owner.reconcile()
    assert owner.recovery()['cleanupReceipts'][0]['evidence']['text'] == 'captured transcription'


@pytest.mark.asyncio
@pytest.mark.parametrize('seed', range(30))
async def test_jittered_replacement_projection_retains_late_old_ids_without_fencing_new_writer(tmp_path, monkeypatch, seed):
    import asyncio
    import random

    import preprocessing.engine_owner as module
    from preprocessing.dev import atomic_json
    rng = random.Random(seed)
    root = tmp_path
    entry = binding(root)
    late = {**entry, 'id': 'operation_late', 'sessionId': 'ses_late', 'inputId': 'msg_late', 'requestHash': 'b' * 64}
    http = {entry['sessionId']: OwnedHTTP(entry), late['sessionId']: OwnedHTTP(late)}
    identities = {1001: 'old-start', 1002: 'new-start'}
    monkeypatch.setattr(module, 'process_identity', identities.get)
    fenced = []

    async def request(method, path, body=None):
        await asyncio.sleep(rng.random() * .001)
        if path.startswith('/api/session?'):
            return 200, {'data': [], 'cursor': {}}
        sid = next(sid for sid in http if '/' + sid in path)
        return await http[sid](method, path, body)

    async def fence(writer):
        await asyncio.sleep(rng.random() * .001)
        fenced.append(writer.copy())
        identities.pop(writer['pid'], None)
        replacement = projection(entry, time.time() + 3600, generation='new',
                                 writer={'pid': 1002, 'starttime': 'new-start'})
        replacement['entries'] = [entry, late]
        atomic_json(owner.projection_path, replacement)
        return True

    owner = module.StandaloneEngineOwner(root / 'owner', root / 'lease.json', request=request, fence_writer=fence)
    atomic_json(owner.projection_path, projection(entry, writer={'pid': 1001, 'starttime': 'old-start'}))
    assert await owner.reconcile()
    assert fenced == [{'pid': 1001, 'starttime': 'old-start'}]
    assert identities[1002] == 'new-start'
    assert {item['sessionId'] for item in owner.recovery()['cleanupReceipts']} == {'ses_owned', 'ses_late'}
    assert all(not path.endswith('/prompt') for transport in http.values() for _, path in transport.events)


@pytest.mark.asyncio
async def test_owner_does_not_publish_old_engine_death_before_retained_server_lock(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import preprocessing.engine_owner as module
    from preprocessing.dev import atomic_json
    root = tmp_path / 'owner'
    root.mkdir(mode=0o700)
    atomic_json(root / 'owner.json', {'engineInstanceId': 'old-boot',
                'guardian': {'pid': 99999999, 'starttime': 'old-start'}, 'deadEngineInstances': [],
                'fencedGenerations': [], 'pendingGenerations': {}})

    class LockedServer:
        def __init__(self, *args):
            pass

        async def start(self, **kwargs):
            assert kwargs['recovery']['deadEngineInstances'] == []
            assert json.loads((root / 'owner.json').read_text())['deadEngineInstances'] == []
            raise BlockingIOError('Guardian descendants still hold server lock')

        async def close(self):
            pass

    monkeypatch.setattr(module, 'PrivateServer', LockedServer)
    monkeypatch.setattr(module, 'load_config', lambda *args: {})
    args = SimpleNamespace(state_dir=root, projection=tmp_path / 'absent.json', descriptor=tmp_path / 'descriptor.json',
                           opencode_executable='/pin/opencode', engine_config=tmp_path / 'config.json',
                           credential_env=[], evidence_bytes=256 * 1024)
    with pytest.raises(BlockingIOError):
        await module._run(args)
    assert json.loads((root / 'owner.json').read_text())['deadEngineInstances'] == []


@pytest.mark.asyncio
async def test_delete_readback_outage_restores_staged_evidence_before_issuing_receipt(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)

    async def request(method, path, body=None):
        if http.deleted and method == 'GET':
            raise OSError('Lost delete readback')
        return await http(method, path, body)

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=request)
    owner.projection_path.write_text(json.dumps(projection(entry)))
    assert not await owner.reconcile()
    assert http.deleted
    assert owner.recovery()['cleanupReceipts'] == []
    restarted = StandaloneEngineOwner(owner.root, owner.projection_path, request=http)
    assert await restarted.reconcile()
    assert restarted.recovery()['cleanupReceipts'][0]['evidence']['text'] == 'captured transcription'


@pytest.mark.asyncio
async def test_owner_evidence_limit_can_be_raised_without_deleting_oversized_evidence(tmp_path):
    from preprocessing.engine_owner import StandaloneEngineOwner
    entry = binding(tmp_path)
    http = OwnedHTTP(entry)
    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=http, evidence_bytes=16)
    owner.projection_path.write_text(json.dumps(projection(entry)))
    assert not await owner.reconcile()
    assert not http.deleted
    assert owner.recovery()['cleanupReceipts'] == []
    owner.evidence_bytes = 1024
    assert await owner.reconcile()
    assert http.deleted


@pytest.mark.asyncio
async def test_receipt_descriptor_bound_defers_deletion_until_consumed_receipts_prune(tmp_path):
    from preprocessing.engine_owner import (
        MAX_DESCRIPTOR_BYTES,
        StandaloneEngineOwner,
        process_identity,
    )
    entries = [{**binding(tmp_path), 'id': f'operation_{index}', 'sessionId': f'ses_{index}',
                'inputId': f'msg_{index}', 'requestHash': f'{index:064x}'} for index in range(18)]
    transports = {entry['sessionId']: OwnedHTTP(entry) for entry in entries}

    async def request(method, path, body=None):
        if path.startswith('/api/session?'):
            return 200, {'data': [], 'cursor': {}}
        sid = next(segment for segment in path.split('/') if segment.split('?')[0] in transports).split('?')[0]
        status, response = await transports[sid](method, path, body)
        if '/message?' in path:
            response['data'][1]['content'][0]['text'] = 'x' * (240 * 1024)
        return status, response

    owner = StandaloneEngineOwner(tmp_path / 'owner', tmp_path / 'lease.json', request=request)
    owner.descriptor = {'engineInstanceId': 'surviving-engine', 'recovery': owner.recovery()}
    owner.descriptor_path = tmp_path / 'descriptor.json'
    current = projection(entries[0])
    current['entries'] = entries
    owner.projection_path.write_text(json.dumps(current))
    assert not await owner.reconcile()
    assert len(owner.descriptor_path.read_bytes()) <= MAX_DESCRIPTOR_BYTES
    receipts = owner.recovery()['cleanupReceipts']
    assert 0 < len(receipts) < len(entries)
    outstanding = [entry for entry in entries if not transports[entry['sessionId']].deleted]
    new = projection(outstanding[0], time.time() + 3600, generation='replacement',
                     writer={'pid': os.getpid(), 'starttime': process_identity(os.getpid())})
    new['entries'] = outstanding
    owner.projection_path.write_text(json.dumps(new))
    await owner.reconcile()
    assert await owner.reconcile()
    assert {receipt['sessionId'] for receipt in owner.recovery()['cleanupReceipts']} == {entry['sessionId'] for entry in outstanding}
    assert len(owner.descriptor_path.read_bytes()) <= MAX_DESCRIPTOR_BYTES


@pytest.mark.asyncio
async def test_pidfd_fences_exact_writer_and_pid_reuse_does_not_signal(tmp_path):
    import subprocess
    import sys

    from preprocessing.engine_owner import fence_writer, process_identity
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])  # noqa: ASYNC220 -- independent writer under test
    try:
        identity = process_identity(process.pid)
        assert await fence_writer({'pid': process.pid, 'starttime': identity + 'wrong'})
        assert process.poll() is None
        assert await fence_writer({'pid': process.pid, 'starttime': identity})
        assert process.wait(timeout=5) < 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.asyncio
async def test_real_standalone_owner_admission_and_boot_replacement(tmp_path):
    import asyncio
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    executable = os.environ.get('PREPROCESSING_TEST_OPENCODE')
    if not executable:
        pytest.skip('Set PREPROCESSING_TEST_OPENCODE to an explicit 2.0.19 executable')
    descriptor_path = tmp_path / 'descriptor.json'
    command = [sys.executable, '-m', 'preprocessing.engine_owner',
               '--state-dir', str(tmp_path / 'owner'), '--projection', str(tmp_path / 'missing-lease.json'),
               '--descriptor', str(descriptor_path), '--opencode-executable', executable,
               '--engine-config', str(Path(__file__).resolve().parents[2] / 'deployment/examples/extraction-engine.json'),
               '--credential-env', 'DEV_OPENCODE_PROVIDER_TOKEN']
    previous = None
    for _ in range(2):
        process = subprocess.Popen(command, env={**os.environ, 'DEV_OPENCODE_PROVIDER_TOKEN': 'probe-only'},  # noqa: ASYNC220 -- supervised external owner under test
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while not descriptor_path.exists():
                assert process.poll() is None
                assert time.monotonic() < deadline
                await asyncio.sleep(.05)
            descriptor = json.loads(descriptor_path.read_text())
            assert descriptor['mode'] == 'standalone'
            assert descriptor['contextVerified'] is True
            assert descriptor['recovery']['managed'] is False
            assert descriptor_path.stat().st_mode & 0o777 == 0o600
            if previous:
                assert descriptor['engineInstanceId'] != previous
                assert previous in descriptor['recovery']['deadEngineInstances']
            previous = descriptor['engineInstanceId']
        finally:
            process.terminate()
            stdout, stderr = await asyncio.to_thread(process.communicate, timeout=15)
            assert 'probe-only' not in stdout + stderr
        assert process.returncode == 0
        assert not descriptor_path.exists()


@pytest.mark.asyncio
async def test_real_pin_owner_receipt_consumed_by_replacement_service_without_replay(tmp_path, monkeypatch):
    import asyncio
    import copy
    import subprocess
    import sys
    from pathlib import Path

    from preprocessing.contracts import default_profile
    from preprocessing.dev import PrivateServer, atomic_json, http_request
    from preprocessing.engine import OpenCodeServerEngine
    from preprocessing.engine_owner import StandaloneEngineOwner
    from preprocessing.service import Service
    executable = os.environ.get('PREPROCESSING_TEST_OPENCODE')
    if not executable:
        pytest.skip('Set PREPROCESSING_TEST_OPENCODE to an explicit 2.0.19 executable')
    monkeypatch.setenv('DEV_OPENCODE_PROVIDER_TOKEN', 'probe-only')
    config = json.loads((Path(__file__).resolve().parents[2] / 'deployment/examples/extraction-engine.json').read_text())
    server = PrivateServer(tmp_path / 'server', executable, config, ['DEV_OPENCODE_PROVIDER_TOKEN'])
    descriptor = await server.start(mode='standalone')
    handoff = tmp_path / 'handoff.json'
    atomic_json(handoff, descriptor)
    root = tmp_path / 'service'
    profile = default_profile()
    profile['models'] = {'vision': 'deployment/vision'}
    probe = OpenCodeServerEngine(descriptor)
    replacement = None
    try:
        for _ in range(100):
            if (await probe.ready(profile))['ready']:
                break
            await asyncio.sleep(.05)
        code = '''import asyncio, json, os, sys
from pathlib import Path
from preprocessing.contracts import default_profile
from preprocessing.dev import atomic_json
from preprocessing.engine import OpenCodeServerEngine
from preprocessing.engine_evidence import operation_marker
from preprocessing.service import Service
async def run():
    root, handoff = map(Path, sys.argv[1:])
    descriptor = json.loads(handoff.read_text())
    profile = default_profile(); profile['models'] = {'vision': 'deployment/vision'}
    engine = OpenCodeServerEngine(descriptor)
    service = Service(root, root, profile=profile, engine=engine)
    await service.start()
    record = {'id': 'operation_receipt', 'jobId': 'job_receipt', 'generation': service.generation,
        'engineInstanceId': descriptor['engineInstanceId'], 'sessionId': 'ses_receipt', 'inputId': 'msg_receipt',
        'requestHash': 'a' * 64, 'agent': 'preprocessing', 'model': {'providerID': 'deployment', 'id': 'vision'},
        'location': {'directory': descriptor['directory']}, 'dispatchIntent': True,
        'mutationAttempted': True, 'state': 'prepared', 'settlement': {'settled': False}}
    service.store.operation(record); service._write_projection()
    await engine._http('POST', '/api/session', payload={'id': record['sessionId'], 'agent': record['agent'],
        'model': record['model'], 'location': record['location'], 'metadata': {'preprocessing': operation_marker(record)},
        'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}]})
    await engine._http('POST', '/api/session/ses_receipt/prompt', payload={'id': record['inputId'],
        'text': 'Park this input; do not run inference.', 'metadata': {'requestHash': record['requestHash']}, 'resume': False})
    await service._record_operation(record['id'], record['jobId'], {'admissionAcknowledged': True, 'state': 'admitted'})
    atomic_json(root / 'record.json', {**record, 'admissionAcknowledged': True, 'state': 'admitted'})
    os._exit(0)
asyncio.run(run())
'''
        writer = subprocess.Popen([sys.executable, '-c', code, str(root), str(handoff)],  # noqa: ASYNC220 -- service writer process loss under test
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _, stderr = await asyncio.to_thread(writer.communicate, timeout=15)
            assert writer.returncode == 0, stderr
        finally:
            if writer.poll() is None:
                writer.kill()
                await asyncio.to_thread(writer.communicate)
        record = json.loads((root / 'record.json').read_text())
        terminal = OwnedHTTP(record)
        owner_calls = []

        async def request(method, path, body=None):
            owner_calls.append((method, path))
            if '/message?' in path:
                # Parked prompts avoid inference. Only the terminal transcript is
                # synthesized; ownership, inbox, interruption, wait and deletion
                # all use the real pinned server.
                return await terminal(method, path, body)
            return await asyncio.to_thread(http_request, descriptor, method, path, body)

        owner = StandaloneEngineOwner(tmp_path / 'owner', root / '.preprocessing/engine-lease.json', request=request)
        assert await owner.reconcile()
        assert any(path.endswith('/inbox/msg_receipt') for _, path in owner_calls)
        assert (await asyncio.to_thread(http_request, descriptor, 'GET', '/api/session/ses_receipt'))[0] == 404
        recovery_descriptor = copy.deepcopy(descriptor)
        recovery_descriptor['recovery'] = owner.recovery()
        engine = OpenCodeServerEngine(recovery_descriptor)
        calls = []
        original_http = engine._http

        async def trace(method, path, **kwargs):
            calls.append((method, path))
            return await original_http(method, path, **kwargs)

        monkeypatch.setattr(engine, '_http', trace)
        replacement = Service(root, root, profile=profile, engine=engine)
        await replacement.start()
        saved = replacement.store.get_operation(record['id'])
        assert saved['settlement']['settled'] is True
        assert saved['evidence']['text'] == 'captured transcription'
        assert replacement.settlement_unknown is False
        assert all(method == 'GET' for method, _ in calls)
        assert all(not path.endswith('/prompt') for _, path in owner_calls)
        assert await owner.reconcile()
        assert owner.recovery()['cleanupReceipts'] == []
    finally:
        if replacement:
            await replacement.close()
        await probe.close()
        await server.close()
