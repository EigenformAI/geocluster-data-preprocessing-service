from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import secrets
import socket
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from preprocessing.engine import (
    DEFAULT_CANCEL_SECONDS,
    DEFAULT_RPC_SECONDS,
    dispatch_allowance,
    finish_allowance,
)
from preprocessing.engine_version import cli_version, compatibility
from preprocessing.errors import ServiceError

DENY = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
VERSION_PROBE_SECONDS = 10


def _invalid():
    return ServiceError('INVALID_ENGINE_CONFIG', 'Use a private extraction-only configuration with explicit provider models and named credential interpolation.')


def validate_config(config, credential_env):
    """Accept only controlled extraction context, never ambient instructions/plugins."""
    from preprocessing.engine import SYSTEM
    allowed = {'model', 'default_agent', 'permissions', 'agents', 'providers', 'mcp',
               'plugins', 'skills', 'snapshots', 'compaction', 'warming', 'update'}
    agent = {'mode': 'primary', 'hidden': False, 'system': SYSTEM, 'permissions': DENY, 'steps': 1}
    if (not isinstance(config, dict) or config.keys() - allowed
            or config.get('agents') != {'preprocessing': agent}
            or config.get('default_agent') != 'preprocessing' or config.get('permissions') != DENY
            or config.get('plugins') != ['-opencode.agent', '-opencode.plan']
            or config.get('mcp') != {'servers': {}} or config.get('skills') != []
            or config.get('snapshots') is not False or config.get('warming') is not False
            or config.get('compaction') != {'auto': False} or config.get('update') != 'disable'):
        raise _invalid()
    names = set(credential_env)
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or
           name.startswith(('OPENCODE_', 'XDG_', 'FLY_')) or
           name in {'HOME', 'PATH', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'LD_PRELOAD', 'NODE_OPTIONS'}
           for name in names):
        raise _invalid()
    providers = config.get('providers')
    if not isinstance(providers, dict) or not providers:
        raise _invalid()
    for provider in providers.values():
        if not isinstance(provider, dict) or provider.keys() - {'name', 'package', 'settings', 'models'}:
            raise _invalid()
        settings = provider.get('settings', {})
        models = provider.get('models', {})
        if (not isinstance(settings, dict) or settings.keys() - {'baseURL', 'apiKey'}
                or not isinstance(settings.get('baseURL'), str)
                or settings.get('apiKey') not in {f'{{env:{name}}}' for name in names}
                or not isinstance(provider.get('package'), str) or not isinstance(models, dict) or not models):
            raise _invalid()
        if len(models) > 128:
            raise _invalid()
        for model_id, model in models.items():
            caps = model.get('capabilities', {}) if isinstance(model, dict) else {}
            if (not isinstance(model, dict) or model.keys() - {'name', 'capabilities', 'limit', 'variants'}
                    or not isinstance(model_id, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,256}', model_id)
                    or not isinstance(caps, dict) or set(caps) != {'tools', 'input', 'output'}
                    or caps.get('tools') is not False or caps.get('input') not in (['text'], ['text', 'image'], ['image', 'text'])
                    or caps.get('output') != ['text']
                    or not isinstance(model.get('limit'), dict)
                    or set(model['limit']) not in ({'context', 'output'}, {'context', 'input', 'output'})
                    or any(type(model['limit'].get(key)) is not int or not 0 < model['limit'][key] <= 2**31 - 1
                            for key in model['limit'])):
                raise _invalid()
            limits = model['limit']
            if 'input' in limits and limits['input'] > limits['context'] - limits['output']:
                raise _invalid()
            variants = model.get('variants', [])
            if not isinstance(variants, list) or len(variants) > 32:
                raise _invalid()
            identities = set()
            for variant in variants:
                if not isinstance(variant, dict) or set(variant) != {'id', 'settings'}:
                    raise _invalid()
                identity, settings = variant['id'], variant['settings']
                if (not isinstance(identity, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', identity) is None
                        or identity in identities or not isinstance(settings, dict) or set(settings) != {'reasoning'}
                        or not isinstance(settings['reasoning'], dict) or set(settings['reasoning']) != {'effort'}
                        or not isinstance(settings['reasoning']['effort'], str)
                        or settings['reasoning']['effort'] not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}):
                    raise _invalid()
                identities.add(identity)
    selected = config.get('model', '')
    if not isinstance(selected, str) or '/' not in selected:
        raise _invalid()
    provider, model = selected.split('/', 1)
    model, marker, variant = model.partition('#')
    declaration = providers.get(provider, {}).get('models', {}).get(model)
    if (declaration is None or marker and (not variant or
            variant not in [item['id'] for item in declaration.get('variants', [])])):
        raise _invalid()
    return config


def load_config(path, credential_env):
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise _invalid()
        return validate_config(json.loads(raw), credential_env)
    except (ValueError, OSError):
        raise _invalid() from None


def isolated_environment(root, config, credential_env):
    validate_config(config, credential_env)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_mode & 0o077:
        raise ServiceError('INVALID_ENGINE_CONFIG', 'The engine state directory must be private (mode 0700).')
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'NO_COLOR': '1'}
    for key, name in {'HOME': 'home', 'XDG_CONFIG_HOME': 'config', 'XDG_DATA_HOME': 'data',
                      'XDG_CACHE_HOME': 'cache', 'XDG_STATE_HOME': 'state',
                      'XDG_RUNTIME_DIR': 'runtime', 'TMPDIR': 'tmp'}.items():
        directory = root / name
        if directory.is_symlink():
            raise _invalid()
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        env[key] = str(directory)
    directory = root / 'workspace'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise _invalid()
    env.update({'XDG_CONFIG_DIRS': env['XDG_CONFIG_HOME'], 'XDG_DATA_DIRS': env['XDG_DATA_HOME'],
                'TMP': env['TMPDIR'], 'TEMP': env['TMPDIR'],
                'OPENCODE_CONFIG_PROJECT_DISABLE': '1', 'OPENCODE_DISABLE_MODELS_FETCH': '1',
                'OPENCODE_CONFIG_CONTENT': json.dumps(config),
                'OPENCODE_MODELS_PATH': str(root / 'models.empty.json')})
    atomic_json(root / 'models.empty.json', {})
    for name in credential_env:
        value = os.environ.get(name)
        if not value:
            raise ServiceError('AUTH_REQUIRED', 'Every explicitly named engine credential environment variable must be set.')
        env[name] = value
    return env, directory.resolve()


def atomic_json(path, value):
    """Private durable handoff; atomic replacement plus parent-directory fsync."""
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def http_request(descriptor, method, path, body=None):
    auth = base64.b64encode((descriptor.get('username', 'opencode') + ':' + descriptor['password']).encode()).decode()
    headers = {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'}
    request = Request(descriptor['serverUrl'] + path, None if body is None else json.dumps(body).encode(), headers, method=method)
    try:
        response = build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2)
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('Engine response exceeds owner inspection bound')
        return response.code, json.loads(raw) if raw else None


class PrivateServer:
    """One ordinary HTTP server under a parent-loss guardian, never ACP/managed."""
    def __init__(self, root, executable, config, credential_env):
        self.root = Path(root).resolve()
        self.executable = Path(executable)
        if not self.executable.is_absolute():
            raise ServiceError('INVALID_ENGINE_CONFIG', 'An absolute pinned OpenCode executable path is required.')
        self.config, self.credential_env = config, credential_env
        self.process = None
        self.lease_fd = None
        self.descriptor = None
        self.lock_fd = None

    async def _spawn(self, command, env, cwd, **kwargs):
        read_fd, write_fd = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-u', str(Path(__file__).with_name('engine_guardian.py')),
                str(read_fd), str(self.lock_fd), '2', *map(str, command),
                pass_fds=(read_fd, self.lock_fd), env=env, cwd=cwd, start_new_session=True, **kwargs)
            return process, write_fd
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)

    async def start(self, mode='development', recovery=None):
        import fcntl
        if sys.platform != 'linux':
            raise ServiceError('ENGINE_UNAVAILABLE', 'Private server ownership requires Linux pidfds.')
        env, directory = isolated_environment(self.root, self.config, self.credential_env)
        self.lock_fd = os.open(self.root / 'server.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            identity_path = self.root / 'server-identity.json'
            previous = json.loads(identity_path.read_text()) if identity_path.exists() else {}
            recovery = recovery or {'managed': False, 'extractionExcluded': True,
                                    'fencedGenerations': [], 'deadEngineInstances': []}
            # Acquiring the guardian-retained lock proves all prior descendants died.
            for identity in [*previous.get('deadEngineInstances', []), previous.get('engineInstanceId')]:
                if identity and identity not in recovery['deadEngineInstances']:
                    recovery['deadEngineInstances'].append(identity)
            version, lease = await self._spawn([self.executable, '--version'], env, directory,
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            actual_version, version_warnings = None, []
            try:
                async with asyncio.timeout(VERSION_PROBE_SECONDS):
                    output = bytearray()
                    while chunk := await version.stdout.read(1025 - len(output)):
                        output.extend(chunk)
                        if len(output) > 1024:
                            break
                    if len(output) > 1024:
                        version_warnings.append('cli_version_probe_oversized')
                    else:
                        await version.wait()
                        actual_version = cli_version(output) if version.returncode == 0 else None
                        if version.returncode:
                            version_warnings.append('cli_version_probe_failed')
            except TimeoutError:
                version_warnings.append('cli_version_probe_timeout')
            finally:
                os.close(lease)
                async def discard_stdout():
                    while await version.stdout.read(65536):
                        pass  # Drain a paused pipe without retaining probe output.
                try:
                    await asyncio.wait_for(asyncio.gather(version.wait(), discard_stdout()), 5)
                except TimeoutError:
                    raise ServiceError('ENGINE_UNAVAILABLE', 'Version probe descendants could not be reaped; inspect the owned guardian before retrying.') from None
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            password = secrets.token_urlsafe(32)
            env['OPENCODE_PASSWORD'] = password
            fd = os.open(self.root / 'server.private.log', os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                self.process, self.lease_fd = await self._spawn(
                    [self.executable, 'serve', '--hostname', '127.0.0.1', '--port', port],
                    env, directory, stdout=fd, stderr=fd, stdin=asyncio.subprocess.DEVNULL)
            finally:
                os.close(fd)
            self.descriptor = {'serverUrl': f'http://127.0.0.1:{port}', 'username': 'opencode',
                               'password': password, 'version': actual_version,
                               'engineInstanceId': uuid.uuid4().hex, 'mode': mode,
                               'owner': str(self.root),
                               'directory': str(directory), 'contextVerified': True,
                               'recovery': recovery}
            atomic_json(identity_path, {'engineInstanceId': self.descriptor['engineInstanceId'],
                                        'deadEngineInstances': recovery['deadEngineInstances']})
            deadline = asyncio.get_running_loop().time() + 30
            while self.process.returncode is None and asyncio.get_running_loop().time() < deadline:
                try:
                    status, body = await asyncio.to_thread(http_request, self.descriptor, 'GET', '/api/info')
                    if status == 200:
                        info = body.get('data', body) if isinstance(body, dict) else None
                        if not isinstance(info, dict):
                            raise ServiceError('ENGINE_UNAVAILABLE', 'The private OpenCode HTTP info response is invalid.')
                        try:
                            evidence = compatibility(cli=actual_version, http=info.get('version'), warnings=version_warnings)
                        except ValueError as error:
                            raise ServiceError('ENGINE_UNAVAILABLE', str(error)) from None
                        self.descriptor.update(version=evidence['httpVersion'] or actual_version,
                                               compatibility=evidence)
                        return self.descriptor
                except (OSError, ValueError):
                    pass
                await asyncio.sleep(.05)
            raise ServiceError('ENGINE_UNAVAILABLE', 'The private OpenCode listener did not become ready; inspect its private log.')
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.lease_fd is not None:
            os.close(self.lease_fd)
            self.lease_fd = None
        if self.process is not None:
            # Guardian retains its lock until every descendant has died.
            await self.process.wait()
            self.process = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


class DevelopmentEngine:
    def __init__(self, root, *, executable, config, credential_env, input_roots,
                 idle_seconds=60, keep_alive=False):
        if any(key.startswith('FLY_') for key in os.environ):
            raise ServiceError('DEV_FORBIDDEN', 'Development engine launch is refused inside Fly.')
        if type(idle_seconds) not in {int, float} or not math.isfinite(idle_seconds) or idle_seconds < 0:
            raise _invalid()
        validate_config(config, credential_env)
        self.server = PrivateServer(root, executable, config, credential_env)
        self.input_roots = input_roots
        self.rpc_seconds = DEFAULT_RPC_SECONDS
        self.cancel_seconds = DEFAULT_CANCEL_SECONDS
        self.idle_seconds, self.keep_alive = idle_seconds, keep_alive
        self.engine = None
        self._binding = None
        self._lock = asyncio.Lock()
        self._idle = None
        self._closed = False
        self._dead_instances = []
        self._fresh = False
        self._active_job = None

    @property
    def descriptor(self):
        return self.server.descriptor or {'mode': 'development'}

    @property
    def dispatch_allowance_seconds(self):
        return dispatch_allowance(self.rpc_seconds)

    @property
    def finish_allowance_seconds(self):
        return finish_allowance(self.rpc_seconds, self.cancel_seconds)

    @property
    def dispatch_poll_allowance_seconds(self):
        return self.rpc_seconds

    def bind(self, *args, **kwargs):
        self._binding = (args, kwargs)
        if self.engine is not None:
            return self.engine.bind(*args, **kwargs)

    async def _ensure(self):
        if self._closed:
            raise ServiceError('ENGINE_UNAVAILABLE', 'Development engine is closed.')
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if self.engine is None:
            from preprocessing.engine import OpenCodeServerEngine
            descriptor = await self.server.start(recovery={'managed': False, 'extractionExcluded': True,
                                                          'fencedGenerations': [], 'deadEngineInstances': self._dead_instances[:]})
            self.engine = OpenCodeServerEngine(descriptor, input_roots=self.input_roots,
                                             rpc_seconds=self.rpc_seconds, cancel_seconds=self.cancel_seconds)
            self._fresh = True
            if self._binding:
                self.engine.bind(*self._binding[0], **self._binding[1])
        return self.engine

    async def _call(self, name, *args, **kwargs):
        async with self._lock:
            engine = await self._ensure()
            if name == 'start':
                self._active_job = args[1] if len(args) > 1 else kwargs['job_id']
            try:
                if name in ('ready', 'ready_catalog'):
                    return await self._catalog_ready(engine, *args, wait_models=name == 'ready_catalog', **kwargs)
                if name == 'start' and self._fresh:
                    await self._catalog_ready(engine, args[3] if len(args) > 3 else kwargs.get('profile'))
                if self._closed:
                    raise ServiceError('ENGINE_UNAVAILABLE', 'Development engine is closed.')
                return await getattr(engine, name)(*args, **kwargs)
            finally:
                if name == 'start':
                    self._active_job = None
                if not self.keep_alive and not self._closed:
                    self._idle = asyncio.create_task(self._stop_idle())

    async def _catalog_ready(self, engine, profile=None, *, wait_models=False):
        # HTTP listener readiness precedes the location's asynchronous catalog.
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            if self._closed:
                return {'ready': False, 'reason': 'Development engine is closed.'}
            result = await engine.ready(profile)
            # Global context can precede asynchronous model discovery. Preserve
            # the startup wait for the first explicit selection in that case.
            if result.get('ready') is True and not (wait_models and self._fresh and not result.get('models')):
                self._fresh = profile is None and not result.get('models')
                return result
            if not self._fresh or asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(.05)

    async def _stop_idle(self):
        await asyncio.sleep(self.idle_seconds)
        async with self._lock:
            await self._stop()

    async def _stop(self):
        try:
            if self.engine is not None:
                await self.engine.close()
        finally:
            self.engine = None
            if self.server.descriptor:
                identity = self.server.descriptor['engineInstanceId']
                await self.server.close()
                if identity not in self._dead_instances:
                    self._dead_instances.append(identity)

    async def ready(self, profile=None):
        return await self._call('ready', profile)

    async def ready_catalog(self):
        """Development-only catalog warmup; global owner readiness remains passive."""
        return await self._call('ready_catalog')

    async def recover(self, persisted_operations):
        return await self._call('recover', persisted_operations)

    async def start(self, *args, **kwargs):
        return await self._call('start', *args, **kwargs)

    async def cancel(self, job_id):
        # Do not acquire the operation lock: cancellation must reach an active prompt.
        return await self.engine.cancel(job_id) if self.engine else {'settled': True}

    async def close(self):
        self._closed = True
        if self.engine is not None and self._active_job is not None:
            await self.engine.cancel(self._active_job)
        if self._idle:
            self._idle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle
        async with self._lock:
            await self._stop()
