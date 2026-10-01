import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

PROJECT = Path(__file__).resolve().parents[2]


def cli(*arguments, env=None):
    return subprocess.run(
        [sys.executable, '-m', 'preprocessing.cli', *map(str, arguments)],
        cwd=PROJECT, env=env, capture_output=True, text=True, timeout=30, check=False,
    )


@pytest.fixture
def engine_descriptor(tmp_path):
    from preprocessing.engine import SYSTEM
    calls = []

    class Engine(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path == '/api/info':
                body = {'version': '2.0.19'}
            elif self.path.startswith('/api/agent?'):
                body = {'data': [{'id': 'preprocessing', 'mode': 'primary', 'system': SYSTEM,
                                 'steps': 1, 'permissions': [{'action': '*', 'resource': '*', 'effect': 'deny'}]}]}
            elif self.path.startswith('/api/model?'):
                body = {'data': [{'providerID': 'fixture', 'id': 'vision', 'enabled': True,
                                 'capabilities': {'input': ['text', 'image'], 'output': ['text']}}]}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Engine) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        descriptor = tmp_path / 'descriptor.json'
        descriptor.write_text(json.dumps({'serverUrl': f'http://127.0.0.1:{server.server_port}',
                                         'password': 'engine-only-secret', 'version': '2.0.19',
                                         'engineInstanceId': 'fixture-boot', 'mode': 'standalone',
                                         'owner': 'fixture-owner', 'directory': str(tmp_path.resolve()),
                                         'contextVerified': True,
                                         'recovery': {'managed': False, 'extractionExcluded': True,
                                                      'fencedGenerations': [], 'deadEngineInstances': []}}))
        try:
            yield descriptor, calls
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_process_native_publishes_exact_products_without_prompting(tmp_path, engine_descriptor):
    source = tmp_path / 'nested inputs' / 'sample.csv'
    source.parent.mkdir()
    source.write_text('id,value\n001,1.000000000000000001\n002,\n')
    root = tmp_path / 'output'
    arguments = ['process', source, '--output', root, '--model', 'fixture/vision',
                 '--engine-descriptor', engine_descriptor[0]]
    result = cli(*arguments)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary['state'] == 'succeeded'
    manifest_path = Path(summary['manifestPath'])
    documents_path = Path(summary['documentsPath'])
    assert manifest_path == root / '.preprocessing/jobs' / summary['jobId'] / 'sealed/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    assert manifest['source']['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest['source']['origin']['path'] == source.name
    assert documents_path == root / 'documents' / manifest['resultId']
    assert (documents_path / 'manifest.json').read_bytes() == manifest_path.read_bytes()
    assert (documents_path / 'AGENTS.md').is_file()
    table = manifest['tables'][0]
    rows = [json.loads(line) for line in (documents_path / table['typedPath']).read_text().splitlines()]
    assert rows[1]['cells'][0]['value'] == '001'
    assert rows[1]['cells'][1]['value'] == '1.000000000000000001'
    for product in manifest['products']:
        assert hashlib.sha256((documents_path / product['path']).read_bytes()).hexdigest() == product['sha256']
    assert engine_descriptor[1] and all(path.startswith(('/api/info', '/api/agent?', '/api/model?')) for path in engine_descriptor[1])

    # A second process must be able to acquire the released service owner lock.
    reconciled = cli('reconcile', '--data-root', root, '--job', summary['jobId'])
    assert reconciled.returncode == 0, reconciled.stderr
    assert json.loads(reconciled.stdout)['state'] == 'ready'
    assert manifest_path.read_bytes() == (documents_path / 'manifest.json').read_bytes()


@pytest.mark.parametrize('host', ['0.0.0.0', '::', '192.0.2.1', 'example.com'])
def test_serve_rejects_nonloopback_without_auth_before_opening_state(tmp_path, host):
    root = tmp_path / 'output'
    result = cli('serve', '--data-root', root, '--workspace', tmp_path, '--host', host)
    assert result.returncode == 1
    assert json.loads(result.stderr)['error']['code'] == 'AUTH_REQUIRED'
    assert not root.exists()


def test_explicit_empty_token_environment_is_rejected_on_loopback(tmp_path):
    environment = {**os.environ, 'PREPROCESSING_TEST_TOKEN': ''}
    result = cli('serve', '--data-root', tmp_path / 'output', '--workspace', tmp_path,
                 '--token-env', 'PREPROCESSING_TEST_TOKEN', env=environment)
    assert result.returncode == 1
    assert json.loads(result.stderr)['error']['code'] == 'AUTH_REQUIRED'


def test_profile_overrides_merge_and_model_is_recorded_without_dispatch(tmp_path, engine_descriptor):
    source = tmp_path / 'sample.csv'
    source.write_text('001;002\n')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'limits': {'rows': 7}, 'options': {'delimiter': ';'}}))
    result = cli('process', source, '--output', tmp_path / 'output', '--profile', profile,
                 '--model', 'fixture/vision', '--engine-descriptor', engine_descriptor[0])
    assert result.returncode == 0, result.stderr
    manifest = json.loads(Path(json.loads(result.stdout)['manifestPath']).read_text())
    assert manifest['pipeline']['limits']['rows'] == 7
    assert manifest['pipeline']['limits']['columns'] == 16_384
    assert manifest['pipeline']['models']['vision'] == 'fixture/vision'
    assert len(manifest['tables'][0]['columns']) == 2


@pytest.mark.parametrize('override', [
    {'limits': {'rows': 0}}, {'limits': {'pages': True}},
    {'limits': {'jobSeconds': 0.5}}, {'limits': {'rows': '5'}},
    {'limits': {'unknown': 5}}, {'options': {'figureDescriptions': 'false'}},
    {'options': {'encoding': 'not-an-encoding'}}, {'options': {'delimiter': '::'}},
    {'retry': {'retryAmbiguous': True}}, {'retry': {'attempts': -1}},
    {'retry': {'backoffSeconds': float('nan')}}, {'models': {'vision': 'invalid'}},
    {'apiKey': 'secret-value'}, {'environment': {'API_KEY': 'secret-value'}},
])
def test_invalid_profiles_fail_before_opening_state_without_echoing_values(tmp_path, override):
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps(override))
    root = tmp_path / 'output'
    result = cli('process', tmp_path / 'sample.csv', '--output', root, '--profile', profile)
    assert result.returncode == 1
    assert json.loads(result.stderr)['error']['code'] == 'INVALID_PROFILE'
    assert 'secret-value' not in result.stderr
    assert not root.exists()


def test_missing_source_returns_failed_job_and_exit_one(tmp_path, engine_descriptor):
    result = cli('process', tmp_path / 'absent.csv', '--output', tmp_path / 'output',
                 '--model', 'fixture/vision', '--engine-descriptor', engine_descriptor[0])
    assert result.returncode == 1
    summary = json.loads(result.stdout)
    assert summary['state'] == 'failed'
    assert summary['error']['code'] == 'SOURCE_NOT_FOUND'
    assert summary['manifestPath'] is None
    assert summary['documentsPath'] is None


def test_process_requires_descriptor_before_opening_state(tmp_path):
    result = cli('process', tmp_path / 'mixed.pdf', '--output', tmp_path / 'output',
                 '--model', 'fixture/vision', env={key: value for key, value in os.environ.items()
                                                 if key != 'PREPROCESSING_ENGINE_DESCRIPTOR'})
    assert result.returncode == 1
    assert json.loads(result.stderr)['error']['code'] == 'ENGINE_DESCRIPTOR_REQUIRED'
    assert not (tmp_path / 'output').exists()


def test_descriptor_environment_and_explicit_profile_selection(tmp_path, engine_descriptor):
    source = tmp_path / 'sample.csv'
    source.write_text('a,b\n1,2\n')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'models': {'vision': 'fixture/vision'}}))
    result = cli('process', source, '--output', tmp_path / 'output', '--profile', profile,
                 env={**os.environ, 'PREPROCESSING_ENGINE_DESCRIPTOR': str(engine_descriptor[0])})
    assert result.returncode == 0, result.stderr
    assert 'engine-only-secret' not in result.stdout + result.stderr


def test_implicit_model_is_rejected_before_opening_state(tmp_path):
    result = cli('process', tmp_path / 'sample.csv', '--output', tmp_path / 'output')
    assert result.returncode == 1
    assert json.loads(result.stderr)['error']['code'] == 'MODEL_REQUIRED'
    assert not (tmp_path / 'output').exists()


def test_real_pin_dev_process_uses_isolated_http_dependency(tmp_path):
    executable = os.environ.get('PREPROCESSING_TEST_OPENCODE')
    if not executable:
        pytest.skip('Set PREPROCESSING_TEST_OPENCODE to an explicit 2.0.19 executable')
    source = tmp_path / 'sample.csv'
    source.write_text('a,b\n1,2\n')
    result = cli('dev', 'process', source, '--output', tmp_path / 'output',
                 '--model', 'deployment/vision', '--opencode-executable', executable,
                 '--engine-config', PROJECT / 'deployment/examples/extraction-engine.json',
                 '--credential-env', 'DEV_OPENCODE_PROVIDER_TOKEN', '--idle-seconds', '0',
                 env={**{key: value for key, value in os.environ.items() if not key.startswith('FLY_')},
                      'DEV_OPENCODE_PROVIDER_TOKEN': 'probe-only-not-a-provider-key'})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['state'] == 'succeeded'
    assert 'probe-only-not-a-provider-key' not in result.stdout + result.stderr


def test_serve_authenticates_http_and_remote_status_uses_existing_owner(tmp_path, engine_descriptor):
    root = tmp_path / 'output'
    token = 'test-only-token'
    environment = {**os.environ, 'PREPROCESSING_TEST_TOKEN': token}
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, '-m', 'preprocessing.cli', 'serve', '--data-root', str(root),
         '--workspace', str(tmp_path), '--port', str(port), '--token-env', 'PREPROCESSING_TEST_TOKEN',
         '--model', 'fixture/vision', '--engine-descriptor', str(engine_descriptor[0])],
        cwd=PROJECT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    url = f'http://127.0.0.1:{port}'
    opener = build_opener(ProxyHandler({}))
    try:
        deadline = time.monotonic() + 15
        while True:
            assert process.poll() is None, process.communicate()
            try:
                request = Request(url + '/preprocessing/capabilities', headers={'Authorization': 'Bearer ' + token})
                with opener.open(request, timeout=1) as response:
                    capabilities = json.load(response)
                break
            except (URLError, OSError):
                if time.monotonic() > deadline:
                    pytest.fail('CLI server did not become ready')
                time.sleep(.05)
        assert '.csv' in capabilities['extensions']
        assert capabilities['engineAvailable'] is True
        with pytest.raises(HTTPError) as denied:
            opener.open(url + '/preprocessing/capabilities', timeout=2)
        assert denied.value.code == 401
        denied.value.close()
        status = cli('status', '--url', url, '--data-root', root,
                     '--token-env', 'PREPROCESSING_TEST_TOKEN', env=environment)
        assert status.returncode == 0, status.stderr
        assert json.loads(status.stdout)['jobs'] == []
        local = cli('status', '--data-root', root)
        assert local.returncode == 1
        assert json.loads(local.stderr)['error']['code'] == 'OWNER_ACTIVE'
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert token not in stdout + stderr
    # Uvicorn may re-raise SIGTERM after graceful shutdown; inspect ownership directly.
    assert cli('status', '--data-root', root).returncode == 0


@pytest.mark.parametrize('response_status', [200, 404, 307])
def test_remote_reconcile_sends_authenticated_post_without_local_fallback(tmp_path, response_status):
    requests = []

    class Owner(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers.get('Authorization')))
            body = json.dumps({'state': 'ready', 'path': 'documents/result_example'}).encode()
            self.send_response(response_status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Location', '/unexpected-redirect')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    # This is a protocol fixture, not evidence that the current API exposes the route.
    with ThreadingHTTPServer(('127.0.0.1', 0), Owner) as owner:
        thread = threading.Thread(target=owner.serve_forever, daemon=True)
        thread.start()
        try:
            root = tmp_path / 'must-not-open'
            result = cli('reconcile', '--data-root', root, '--job', 'job_example',
                         '--url', f'http://127.0.0.1:{owner.server_port}',
                         '--token-env', 'PREPROCESSING_TEST_TOKEN',
                         env={**os.environ, 'PREPROCESSING_TEST_TOKEN': 'test-only-token'})
            assert requests == [('/preprocessing/jobs/job_example/reconcile', 'Bearer test-only-token')]
            assert not root.exists()
            if response_status == 200:
                assert result.returncode == 0, result.stderr
                assert json.loads(result.stdout)['state'] == 'ready'
            else:
                assert result.returncode == 1
                assert json.loads(result.stderr)['error']['code'] == 'OWNER_HTTP_ERROR'
            assert 'test-only-token' not in result.stdout + result.stderr
        finally:
            owner.shutdown()
            thread.join(timeout=5)
