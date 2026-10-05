"""Local commands and an HTTP client for the existing service owner."""
import argparse
import asyncio
import io
import ipaddress
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.service import Service
from preprocessing.store import TERMINAL
from preprocessing.table_recovery import effective_profile, validate_override


def _invalid_profile():
    # Profiles are persisted in manifests: never include unvalidated values in errors.
    return ServiceError('INVALID_PROFILE', 'Invalid profile. Use the supported profile fields and value ranges.')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_profile()
        result[key] = value
    return result


def load_profile(path=None, model=None):
    """Merge a small, credential-free admin override into the service defaults."""
    profile = default_profile()
    allowed = {
        'limits': set(profile['limits']) | {'modelImages', 'modelImageBytes'},
        'options': {'encoding', 'delimiter', 'figureDescriptions', 'figureDetector', 'tableRecovery'},
        'models': set(profile['models']),
        'retry': set(profile['retry']) | {'backoffSeconds'},
    }
    explicit_models = False
    overrides = {}
    if path is not None:
        try:
            with Path(path).open('rb') as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise _invalid_profile()
            overrides = json.loads(raw, object_pairs_hook=_unique_object)
        except (OSError, ValueError, RecursionError):
            raise _invalid_profile() from None
        if not isinstance(overrides, dict) or overrides.keys() - allowed.keys() - {'id'}:
            raise _invalid_profile()
        explicit_models = isinstance(overrides.get('models'), dict) and set(profile['models']) <= set(overrides['models'])
        for key, value in overrides.items():
            if key == 'id':
                profile[key] = value
            elif not isinstance(value, dict) or value.keys() - allowed[key]:
                raise _invalid_profile()
            else:
                profile[key].update(value)

    if model is not None:
        profile['models'] = dict.fromkeys(profile['models'], model)
    if not isinstance(profile['id'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', profile['id']):
        raise _invalid_profile()
    for value in profile['limits'].values():
        if type(value) is not int or not 0 < value <= sys.maxsize:
            raise _invalid_profile()
    for role, value in profile['models'].items():
        if model is None and role not in overrides.get('models', {}):
            continue  # Unselected defaults receive the actionable MODEL_REQUIRED below.
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_./:-]+(?:#[A-Za-z0-9_.-]+)?', value):
            raise _invalid_profile()
    options = profile['options']
    detector = options.get('figureDetector', {})
    if (not isinstance(detector, dict) or set(detector) - {'minDimensionPoints', 'groupOverlaps', 'captionPage'}
            or any(type(detector[key]) is not bool for key in ('groupOverlaps', 'captionPage') if key in detector)
            or ('minDimensionPoints' in detector and
                (type(detector['minDimensionPoints']) not in {int, float} or not math.isfinite(detector['minDimensionPoints']) or detector['minDimensionPoints'] < 0))):
        raise _invalid_profile()
    if type(options['figureDescriptions']) is not bool or not isinstance(options['encoding'], str):
        raise _invalid_profile()
    try:
        # Validate a text codec without decoding a sample that may use another encoding.
        with io.TextIOWrapper(io.BytesIO(), encoding=options['encoding']):
            pass
    except (LookupError, ValueError):
        raise _invalid_profile() from None
    if 'delimiter' in options and (not isinstance(options['delimiter'], str)
                                   or len(options['delimiter']) != 1
                                   or options['delimiter'] in '\r\n\0'):
        raise _invalid_profile()
    retry = profile['retry']
    if (type(retry['attempts']) is not int or retry['attempts'] < 1
            or type(retry['noProgress']) is not int or retry['noProgress'] < 0
            or retry['retryAmbiguous'] is not False):
        raise _invalid_profile()
    backoff = retry.get('backoffSeconds', 1)
    if type(backoff) not in {int, float} or not 0 <= backoff <= sys.maxsize or not math.isfinite(backoff):
        raise _invalid_profile()
    if model is None and not explicit_models:
        raise ServiceError('MODEL_REQUIRED', 'Select --model provider/model for all roles, or supply a profile with every model role explicitly selected.')
    return effective_profile(profile)


def _table_override(args):
    override = {}
    for attr, field in [('table_geometry', 'geometry'), ('table_vision', 'vision'),
                        ('table_force_pages', 'forcePages')]:
        value = getattr(args, attr, None)
        if value is not None:
            if field == 'forcePages':
                try:
                    value = [int(page) for page in value.split(',')] if value else []
                except ValueError:
                    raise ServiceError('INVALID_REQUEST', 'Use comma-separated physical page numbers for --table-force-pages.') from None
            override[field] = value
    return validate_override(override) if override else None


def _table_flags(command):
    command.add_argument('--table-geometry', choices=['auto', 'force', 'off'])
    command.add_argument('--table-vision', choices=['confirm-all', 'on-demand', 'off'])
    command.add_argument('--table-force-pages', help='Comma-separated one-based PDF pages; empty replaces the profile list.')


def _resume_body(parent, args):
    if not parent.get('source'):
        raise ServiceError('INVALID_REQUEST', 'The parent job has no retained snapshot.')
    body = {'source': {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']},
            'resumeOf': parent['id'], 'profileId': parent['profile']['id']}
    override = _table_override(args)
    if override is not None:
        body['tableRecovery'] = override
    return body


def _token(variable):
    if variable is None:
        return None
    value = os.environ.get(variable)
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ServiceError('AUTH_REQUIRED', 'The token environment variable must contain a nonempty printable ASCII token without spaces.')
    return value


def _loopback(host):
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_descriptor(path):
    if path is None:
        path = os.environ.get('PREPROCESSING_ENGINE_DESCRIPTOR')
    if not path:
        raise ServiceError('ENGINE_DESCRIPTOR_REQUIRED', 'Production process/serve require --engine-descriptor PATH or PREPROCESSING_ENGINE_DESCRIPTOR.')
    try:
        with Path(path).open('rb') as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        descriptor = json.loads(raw) if len(raw) <= 4 * 1024 * 1024 else None
        if not isinstance(descriptor, dict):
            raise TypeError
    except (OSError, ValueError, TypeError, RecursionError):
        raise ServiceError('INVALID_ENGINE_DESCRIPTOR', 'Could not read a bounded engine descriptor JSON object.') from None
    return descriptor


def _service(root, workspace, profile, args):
    root = Path(root).resolve()
    if getattr(args, 'development', False):
        from preprocessing.dev import DevelopmentEngine, load_config
        config = load_config(args.engine_config, args.credential_env)
        for selected in profile['models'].values():
            provider, model = selected.split('/', 1)
            model = model.split('#', 1)[0]
            if model not in config['providers'].get(provider, {}).get('models', {}):
                raise ServiceError('INVALID_ENGINE_CONFIG', 'Every profile model must be explicitly declared in the private engine configuration.')
        engine = DevelopmentEngine(root / '.preprocessing/dev-engine', executable=args.opencode_executable,
                                   config=config, credential_env=args.credential_env,
                                   input_roots=[root / '.preprocessing/jobs'],
                                   idle_seconds=args.idle_seconds, keep_alive=args.keep_alive)
    else:
        from preprocessing.engine import OpenCodeServerEngine
        descriptor_path = args.engine_descriptor or os.environ.get('PREPROCESSING_ENGINE_DESCRIPTOR')
        engine = OpenCodeServerEngine(load_descriptor(descriptor_path),
                                      descriptor_loader=lambda: load_descriptor(descriptor_path),
                                      input_roots=[root / '.preprocessing/jobs'])
    return Service(root, workspace, profile=profile, engine=engine)


async def _process(args, profile):
    source = args.source.resolve()
    service = _service(args.output, source.parent, profile, args)
    try:
        await service.start()
        body = {
            'source': {'kind': 'workspace-file', 'path': source.name},
            'profileId': profile['id'],
        }
        override = _table_override(args)
        if override is not None:
            body['tableRecovery'] = override
        job, _ = await service.submit(str(uuid.uuid4()), body)
        while True:
            job = service.get(job['id'])
            # Terminal extraction precedes the scheduler's shared-publication attempt.
            if ((job['state'] in TERMINAL and service.active != job['id'])
                    or (job['state'] == 'cancelling' and service.settlement_unknown)):
                break
            await asyncio.sleep(.05)
        manifest_path = None
        if job.get('resultId'):
            service.get_result(job['id'])
            manifest_path = str(service.private / 'jobs' / job['id'] / 'sealed/manifest.json')
        documents = job.get('documents') or {}
        summary = {
            'jobId': job['id'], 'state': job['state'], 'resultId': job.get('resultId'),
            'manifestPath': manifest_path,
            'documentsPath': str(service.root / documents['path']) if documents.get('path') else None,
            'documents': job.get('documents'), 'error': job.get('error'),
        }
        return summary, {'succeeded': 0, 'partial': 2}.get(job['state'], 1)
    finally:
        await service.close()


async def _local_admin(args):
    root = args.data_root.resolve()
    service = Service(root, root, admin_only=True)
    try:
        await service.start()
        if args.command == 'reconcile':
            documents = await service.reconcile_documents(args.job)
            return documents, 0 if documents['state'] == 'ready' else 1
        return service.get(args.job) if args.job else service.list_jobs(), 0
    finally:
        await service.close()


async def _local_resume(args):
    root = args.data_root.resolve()
    reader = Service(root, root, admin_only=True)
    try:
        await reader.start()
        parent = reader.get(args.job)
    finally:
        await reader.close()
    profile = load_profile(args.profile, args.model) if args.profile or args.model else parent['profile']
    service = _service(root, root, profile, args)
    try:
        await service.start()
        job, _ = await service.submit(str(uuid.uuid4()), _resume_body(parent, args))
        while service.get(job['id'])['state'] not in TERMINAL or service.active == job['id']:
            if service.settlement_unknown:
                break
            await asyncio.sleep(.05)
        job = service.get(job['id'])
        return job, {'succeeded': 0, 'partial': 2}.get(job['state'], 1)
    finally:
        await service.close()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _owner_request(args, token, path, *, body=None, reconcile=False):
    try:
        url = urlsplit(args.url)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment):
            raise ValueError
        _ = url.port  # Validate the optional port without echoing a malformed URL.
    except ValueError:
        raise ServiceError('INVALID_URL', 'Use an HTTP or HTTPS owner URL without credentials, query, or fragment.') from None
    headers = {'Accept': 'application/json'}
    data = b'' if reconcile else None
    if body is not None:
        data = json.dumps(body, allow_nan=False).encode()
        headers.update({'Content-Type': 'application/json', 'Idempotency-Key': str(uuid.uuid4())})
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = Request(args.url.rstrip('/') + path, data=data,
                      headers=headers, method='POST' if reconcile or body is not None else 'GET')
    try:
        # Never forward credentials through redirects or ambient proxy settings.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError
        body = json.loads(raw)
        if not isinstance(body, dict) or (reconcile and body.get('state') not in {'ready', 'failed', 'pending'}):
            raise ValueError
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ServiceError('OWNER_HTTP_ERROR', f'The owner returned HTTP {status}. Check its API and authentication.', status, 'check-status') from None
    except (URLError, OSError, ValueError):
        raise ServiceError('OWNER_UNAVAILABLE', 'No valid response from the owner. Check its status before retrying.', 503, 'check-status') from None
    return body


def _remote_admin(args, token):
    path = '/preprocessing/jobs'
    if args.job:
        path += '/' + quote(args.job, safe='')
    if args.command == 'resume':
        parent = _owner_request(args, token, path)
        return _owner_request(args, token, '/preprocessing/jobs', body=_resume_body(parent, args)), 0
    reconcile = args.command == 'reconcile'
    if reconcile:
        path += '/reconcile'
    body = _owner_request(args, token, path, reconcile=reconcile)
    return body, 0 if not reconcile or body['state'] == 'ready' else 1


def _parser():
    parser = argparse.ArgumentParser(prog='preprocess', description='Extract documents and manage the local preprocessing service.')
    commands = parser.add_subparsers(dest='command', required=True)
    dev = commands.add_parser('dev', help='Launch an isolated development-only HTTP engine (refused on Fly).')
    dev_commands = dev.add_subparsers(dest='command', required=True)
    for parent, development in ((commands, False), (dev_commands, True)):
        for name in ('process', 'serve'):
            command = parent.add_parser(name, help='Process one source.' if name == 'process' else 'Serve HTTP and MCP.')
            command.set_defaults(development=development)
            if name == 'process':
                command.add_argument('source', type=Path)
                command.add_argument('--output', type=Path, required=True, help='Service data root.')
                _table_flags(command)
            else:
                command.add_argument('--data-root', type=Path, required=True)
                command.add_argument('--workspace', type=Path, required=True)
                command.add_argument('--host', default='127.0.0.1')
                command.add_argument('--port', type=int, default=8765)
                command.add_argument('--token-env', help='Environment variable containing the service bearer token.')
            command.add_argument('--profile', type=Path, help='Trusted credential-free JSON profile overrides.')
            command.add_argument('--model', help='Explicit provider/model for every model role.')
            if development:
                command.add_argument('--opencode-executable', type=Path, required=True, help='Absolute path to the pinned 2.0.19 CLI.')
                command.add_argument('--engine-config', type=Path, required=True, help='Private extraction-only OpenCode JSON configuration.')
                command.add_argument('--credential-env', action='append', default=[], help='Forward only this named credential variable (repeatable).')
                command.add_argument('--idle-seconds', type=float, default=60, help='Stop owned engine after idle seconds; 0 stops immediately.')
                command.add_argument('--keep-alive', action='store_true', help='Keep the engine until this launcher exits.')
            else:
                command.add_argument('--engine-descriptor', type=Path, help='Private descriptor JSON; defaults to PREPROCESSING_ENGINE_DESCRIPTOR.')
    for name, help_text in [('reconcile', 'Repair shared publication without extracting again.'),
                            ('resume', 'Resume completed PDF work with a fresh submission UUID.'),
                            ('status', 'Read a job or list recent jobs.')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--data-root', type=Path, help='Required without --url; opens an exclusive owner.')
        command.add_argument('--job', required=name in {'reconcile', 'resume'})
        command.add_argument('--url', help='Existing owner base URL; never opens the local data root.')
        command.add_argument('--token-env', help='Environment variable containing the owner bearer token.')
        if name == 'resume':
            _table_flags(command)
            command.add_argument('--profile', type=Path)
            command.add_argument('--model')
            command.add_argument('--engine-descriptor', type=Path)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {'process', 'serve'}:
            if args.command == 'serve':
                token = _token(args.token_env)
                if not _loopback(args.host) and token is None:
                    raise ServiceError('AUTH_REQUIRED', 'Non-loopback listeners require --token-env with a nonempty token.')
                if not 0 <= args.port <= 65535:
                    raise ServiceError('INVALID_PORT', 'Port must be between 0 and 65535.')
            profile = load_profile(args.profile, args.model)
            if args.command == 'process':
                result, code = asyncio.run(_process(args, profile))
            else:
                import uvicorn

                from preprocessing.api import create_app
                service = _service(args.data_root, args.workspace, profile, args)
                uvicorn.run(create_app(service, token=token), host=args.host, port=args.port)
                return 0
        else:
            if not args.url and args.data_root is None:
                parser.error('--data-root is required without --url')
            token = _token(args.token_env)
            if not args.url and token is not None:
                parser.error('--token-env requires --url for this command')
            if args.command == 'resume' and args.url and (args.profile or args.model):
                raise ServiceError('UNSUPPORTED_OPTION', 'Remote resume selects the configured server profile; use submission overrides for recovery controls.')
            result, code = (_remote_admin(args, token) if args.url else
                            asyncio.run(_local_resume(args) if args.command == 'resume' else _local_admin(args)))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return code
    except ServiceError as exc:
        error = exc
    except KeyboardInterrupt:
        error = ServiceError('CANCELLED', 'Command interrupted. Check job status before retrying.', retry='check-status')
    except OSError:
        error = ServiceError('IO_ERROR', 'Could not access the source, configuration, or data root.', retry='operator-action')
    print(json.dumps({'error': error.as_dict()}), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
