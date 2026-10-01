import json
import os
from pathlib import Path

import pytest

from preprocessing.errors import ServiceError


def config():
    from preprocessing.engine import SYSTEM
    deny = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
    return {
        'model': 'fixture/vision', 'default_agent': 'preprocessing', 'permissions': deny,
        'agents': {'preprocessing': {'mode': 'primary', 'hidden': False,
                                    'system': SYSTEM, 'steps': 1, 'permissions': deny}},
        'providers': {'fixture': {'name': 'Fixture', 'package': '@ai-sdk/openai-compatible',
                                'settings': {'baseURL': 'https://example.invalid/v1',
                                            'apiKey': '{env:DEV_TEST_TOKEN}'},
                                'models': {'vision': {'name': 'Vision', 'capabilities': {'tools': False, 'input': ['text', 'image'], 'output': ['text']},
                                                     'limit': {'context': 4096, 'output': 512}}}}},
        'plugins': ['-opencode.agent', '-opencode.plan'], 'skills': [],
        'mcp': {'servers': {}}, 'snapshots': False, 'warming': False,
        'compaction': {'auto': False}, 'update': 'disable',
    }


def test_private_environment_forwards_only_named_credentials(tmp_path, monkeypatch):
    from preprocessing.dev import isolated_environment
    monkeypatch.setenv('DEV_TEST_TOKEN', 'explicit-secret')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'ambient-secret')
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', 'ambient-config')
    env, directory = isolated_environment(tmp_path / 'private', config(), ['DEV_TEST_TOKEN'])
    assert env['DEV_TEST_TOKEN'] == 'explicit-secret'
    assert 'ANTHROPIC_API_KEY' not in env
    assert 'explicit-secret' not in env['OPENCODE_CONFIG_CONTENT']
    assert json.loads(env['OPENCODE_CONFIG_CONTENT']) == config()
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME',
                'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR'):
        assert os.path.commonpath([str(tmp_path), env[key]]) == str(tmp_path)
        assert os.stat(env[key]).st_mode & 0o777 == 0o700
    assert directory.is_absolute()
    assert json.loads(Path(env['OPENCODE_MODELS_PATH']).read_text()) == {}
    assert env['OPENCODE_DISABLE_MODELS_FETCH'] == '1'


def test_development_refuses_fly_before_creating_state(tmp_path, monkeypatch):
    from preprocessing.dev import DevelopmentEngine
    monkeypatch.setenv('FLY_MACHINE_ID', 'machine')
    with pytest.raises(ServiceError, match='Fly'):
        DevelopmentEngine(tmp_path / 'private', executable='/explicit/opencode',
                          config=config(), credential_env=['DEV_TEST_TOKEN'], input_roots=[])
    assert not (tmp_path / 'private').exists()


@pytest.mark.parametrize('change', [
    {'warming': True}, {'plugins': ['unexpected']},
    {'agents': {'preprocessing': {'system': 'wrong'}}},
    {'providers': {'fixture': {'options': {'apiKey': 'literal-secret'}}}},
])
def test_uncontrolled_context_rejected(tmp_path, change):
    from preprocessing.dev import validate_config
    with pytest.raises(ServiceError):
        validate_config({**config(), **change}, ['DEV_TEST_TOKEN'])


@pytest.mark.asyncio
@pytest.mark.parametrize('keep_alive', [False, True])
async def test_lazy_launch_and_explicit_idle_lifetime(tmp_path, monkeypatch, keep_alive):
    import asyncio

    import preprocessing.engine
    from preprocessing.dev import DevelopmentEngine
    events = []

    class Adapter:
        def __init__(self, descriptor, **kwargs):
            self.descriptor = descriptor

        def bind(self, callback):
            self.callback = callback

        async def ready(self, profile):
            events.append('ready')
            return {'ready': True}

        async def close(self):
            events.append('adapter-close')

    monkeypatch.setattr(preprocessing.engine, 'OpenCodeServerEngine', Adapter)
    engine = DevelopmentEngine(tmp_path / 'private', executable='/explicit/opencode',
                               config=config(), credential_env=['DEV_TEST_TOKEN'], input_roots=[],
                               idle_seconds=0, keep_alive=keep_alive)

    async def start(**kwargs):
        events.append('server-start')
        engine.server.descriptor = {'engineInstanceId': 'fake-boot'}
        return engine.server.descriptor

    async def close():
        events.append('server-close')

    monkeypatch.setattr(engine.server, 'start', start)
    monkeypatch.setattr(engine.server, 'close', close)
    engine.bind(lambda *args: None)
    assert events == []
    assert await engine.ready({'models': {'vision': 'fixture/vision'}}) == {'ready': True}
    await asyncio.sleep(.01)
    if keep_alive:
        assert events == ['server-start', 'ready']
    else:
        assert events == ['server-start', 'ready', 'adapter-close', 'server-close']
    await engine.close()
    assert 'server-close' in events


@pytest.mark.asyncio
async def test_close_cancels_active_operation_before_waiting_for_serial_lock(tmp_path):
    import asyncio
    from types import SimpleNamespace

    from preprocessing.dev import DevelopmentEngine
    engine = DevelopmentEngine(tmp_path / 'private', executable='/explicit/opencode', config=config(),
                               credential_env=['DEV_TEST_TOKEN'], input_roots=[], keep_alive=True)
    running = asyncio.Event()
    cancelled = asyncio.Event()
    events = []

    async def start(*args):
        running.set()
        await cancelled.wait()

    async def cancel(job_id):
        events.append(('cancel', job_id))
        cancelled.set()
        return {'settled': True}

    async def close():
        events.append('close')

    engine.engine = SimpleNamespace(start=start, cancel=cancel, close=close)
    task = asyncio.create_task(engine.start('operation_owned', 'job_owned', 'generation', {}, [], {}))
    await running.wait()
    try:
        await asyncio.wait_for(engine.close(), .3)
        await task
        assert events == [('cancel', 'job_owned'), 'close']
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_real_pin_catalog_and_guardian_shutdown(tmp_path, monkeypatch):
    import asyncio
    from urllib.parse import urlencode

    from preprocessing.dev import PrivateServer, http_request
    from preprocessing.engine import SYSTEM
    executable = os.environ.get('PREPROCESSING_TEST_OPENCODE')
    if not executable:
        pytest.skip('Set PREPROCESSING_TEST_OPENCODE to an explicit 2.0.19 executable')
    monkeypatch.setenv('DEV_TEST_TOKEN', 'probe-only-not-a-provider-key')
    ambient = tmp_path / 'ambient-home'
    global_config = ambient / '.config/opencode'
    global_config.mkdir(parents=True)
    sentinel = 'HARMLESS_AMBIENT_CONTEXT_SENTINEL'
    (global_config / 'opencode.json').write_text(json.dumps({
        'instructions': [str(global_config / 'AGENTS.md')],
        'agents': {'ambient-sentinel': {'mode': 'primary', 'system': sentinel}},
    }))
    (global_config / 'AGENTS.md').write_text(sentinel)
    (ambient / '.claude').mkdir()
    (ambient / '.claude/CLAUDE.md').write_text(sentinel)
    monkeypatch.setenv('HOME', str(ambient))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(ambient / '.config'))
    monkeypatch.setenv('OPENCODE_CONFIG', str(global_config / 'opencode.json'))
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    log = private / 'server.private.log'
    log.write_text('preexisting private startup log\n')
    log.chmod(0o644)
    server = PrivateServer(tmp_path / 'private', executable, config(), ['DEV_TEST_TOKEN'])
    first = await server.start()
    try:
        for _ in range(100):
            query = '?' + urlencode({'location[directory]': first['directory']})
            _, agents = await asyncio.to_thread(http_request, first, 'GET', '/api/agent' + query)
            _, models = await asyncio.to_thread(http_request, first, 'GET', '/api/model' + query)
            if any(item['id'] == 'preprocessing' for item in agents['data']) and models['data']:
                break
            await asyncio.sleep(.05)
        agent = next(item for item in agents['data'] if item['id'] == 'preprocessing')
        assert agent['system'] == SYSTEM and agent['steps'] == 1
        assert agent['permissions'][-1] == {'action': '*', 'resource': '*', 'effect': 'deny'}
        assert all(item['id'] != 'ambient-sentinel' for item in agents['data'])
        # The pinned config endpoint exposes discovery roots and loaded documents.
        status, effective = await asyncio.to_thread(http_request, first, 'GET', '/api/config' + query)
        assert status == 200 and isinstance(effective, list)
        assert sentinel not in json.dumps(effective)
        assert all(str(item['path']).startswith(str(private)) for item in effective if item['type'] == 'directory')
        assert log.stat().st_mode & 0o777 == 0o600
        assert any(item['id'] == 'vision' and item['providerID'] == 'fixture' and item['enabled']
                   and item['capabilities']['input'] == ['text', 'image'] for item in models['data'])
        assert first['recovery']['managed'] is False
        assert first['mode'] == 'development'
        # Exercise the owner's exact known-ID primitives without running a model.
        status, created = await asyncio.to_thread(http_request, first, 'POST', '/api/session', {
            'id': 'ses_owner_probe', 'agent': 'preprocessing',
            'model': {'providerID': 'fixture', 'id': 'vision'},
            'location': {'directory': first['directory']},
            'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}],
        })
        assert status == 200 and created['data']['id'] == 'ses_owner_probe'
        path = '/api/session/ses_owner_probe'
        status, admitted = await asyncio.to_thread(http_request, first, 'POST', path + '/prompt', {
            'id': 'msg_owner_probe', 'text': 'Do not execute: owner inbox probe.', 'resume': False,
        })
        assert status == 200 and admitted['data']['id'] == 'msg_owner_probe'
        status, inbox = await asyncio.to_thread(http_request, first, 'GET', path + '/inbox')
        assert status == 200 and any(item['id'] == 'msg_owner_probe' for item in inbox['data'])
        status, _ = await asyncio.to_thread(http_request, first, 'DELETE', path + '/inbox/msg_owner_probe')
        assert status == 204
        status, interrupted = await asyncio.to_thread(http_request, first, 'POST', path + '/interrupt')
        assert status == 200 and interrupted == {'interrupted': False}
        status, _ = await asyncio.to_thread(http_request, first, 'POST', '/api/experimental/session/ses_owner_probe/wait')
        assert status == 204
        status, transcript = await asyncio.to_thread(http_request, first, 'GET', path + '/context')
        assert status == 200 and isinstance(transcript['data'], list)
        status, _ = await asyncio.to_thread(http_request, first, 'DELETE', path)
        assert status == 204
        status, _ = await asyncio.to_thread(http_request, first, 'GET', path)
        assert status == 404
    finally:
        await server.close()
    with pytest.raises(OSError):
        await asyncio.to_thread(http_request, first, 'GET', '/api/info')
    replacement = PrivateServer(tmp_path / 'private', executable, config(), ['DEV_TEST_TOKEN'])
    try:
        second = await replacement.start()
        assert second['engineInstanceId'] != first['engineInstanceId']
        assert first['engineInstanceId'] in second['recovery']['deadEngineInstances']
    finally:
        await replacement.close()


@pytest.mark.asyncio
async def test_real_pin_parent_loss_stops_server(tmp_path, monkeypatch):
    import asyncio
    import subprocess
    import sys
    import time

    from preprocessing.dev import PrivateServer, http_request

    executable = os.environ.get('PREPROCESSING_TEST_OPENCODE')
    if not executable:
        pytest.skip('Set PREPROCESSING_TEST_OPENCODE to an explicit 2.0.19 executable')
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(config()))
    root = tmp_path / 'private'
    code = '''import asyncio, json, sys
from pathlib import Path
from preprocessing.dev import PrivateServer, atomic_json
async def run():
    root, executable, config_path = sys.argv[1:]
    server = PrivateServer(root, executable, json.loads(Path(config_path).read_text()), ['DEV_TEST_TOKEN'])
    descriptor = await server.start()
    atomic_json(Path(root) / 'handoff.json', descriptor)
    print('ready', flush=True)
    await asyncio.sleep(60)
asyncio.run(run())
'''
    environment = {**os.environ, 'DEV_TEST_TOKEN': 'probe-only'}
    parent = subprocess.Popen([sys.executable, '-c', code, str(root), executable, str(config_path)],  # noqa: ASYNC220 -- independent launcher under test
                              env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not (root / 'handoff.json').exists():
            assert parent.poll() is None
            assert time.monotonic() < deadline
            await asyncio.sleep(.05)
        descriptor = json.loads((root / 'handoff.json').read_text())
        parent.kill()
        parent.wait(timeout=5)
        for _ in range(150):
            try:
                await asyncio.to_thread(http_request, descriptor, 'GET', '/api/info')
            except OSError:
                break
            await asyncio.sleep(.05)
        else:
            pytest.fail('Guardian did not stop the server after launcher SIGKILL')
        # Root replacement lock is also released only after descendant death.
        await asyncio.sleep(.2)
        monkeypatch.setenv('DEV_TEST_TOKEN', 'probe-only')
        replacement = PrivateServer(root, executable, config(), ['DEV_TEST_TOKEN'])
        try:
            next_descriptor = await replacement.start()
            assert descriptor['engineInstanceId'] in next_descriptor['recovery']['deadEngineInstances']
        finally:
            await replacement.close()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        parent.communicate(timeout=5)
