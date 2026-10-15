import copy
import hashlib
import json
import math
from importlib.metadata import version

from preprocessing.errors import ServiceError

DEFAULT_TABLE_RECOVERY = {
    'geometry': 'auto', 'vision': 'confirm-all', 'forcePages': [],
    'renderDpi': 300, 'maxAdditionalPasses': 1, 'maxModelCalls': None,
    'workSeconds': None,
}
RECOVERY_VERSIONS = {
    'discovery': '1', 'recovery': '1', 'comparison': '1',
    'confirmationPrompt': '1', 'scoutPrompt': '1', 'workPlan': '1',
}
RECOVERY_EVENT_FIELDS = {
    'page', 'tableId', 'candidateId', 'operationId', 'elapsedSeconds', 'count',
    'state', 'reason', 'modelCalls', 'additionalPasses', 'reusedUnits',
    'pendingUnits', 'completedUnits',
}
TABLE_RECOVERY_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'geometry': {'enum': ['auto', 'force', 'off']},
        'vision': {'enum': ['confirm-all', 'on-demand', 'off']},
        'forcePages': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
        'renderDpi': {'type': 'number', 'exclusiveMinimum': 0},
        'maxAdditionalPasses': {'type': 'integer', 'minimum': 0},
        'maxModelCalls': {'type': ['integer', 'null'], 'minimum': 1},
        'workSeconds': {'type': ['number', 'null'], 'exclusiveMinimum': 0},
    },
}


def _finite(value):
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_override(value, *, code='INVALID_REQUEST'):
    def invalid():
        raise ServiceError(code, 'Invalid tableRecovery fields or value ranges.')

    if not isinstance(value, dict) or value.keys() - DEFAULT_TABLE_RECOVERY.keys():
        invalid()
    result = copy.deepcopy(value)
    for key, item in result.items():
        if key in {'geometry', 'vision'}:
            choices = ('auto', 'force', 'off') if key == 'geometry' else ('confirm-all', 'on-demand', 'off')
            if not isinstance(item, str) or item not in choices:
                invalid()
        elif key == 'forcePages':
            if not isinstance(item, list) or any(type(page) is not int or page <= 0 for page in item):
                invalid()
            result[key] = sorted(set(item))
        elif key in {'maxAdditionalPasses', 'maxModelCalls'}:
            if key == 'maxModelCalls' and item is None:
                continue
            if type(item) is not int or item < (0 if key == 'maxAdditionalPasses' else 1):
                invalid()
        else:
            if key == 'workSeconds' and item is None:
                continue
            if type(item) not in {int, float} or not _finite(item) or item <= 0:
                invalid()
            if isinstance(item, float) and item.is_integer():
                result[key] = int(item)
    return result


def resolve_policy(value=None, *, job_seconds=3600, override=None, code='INVALID_PROFILE'):
    policy = copy.deepcopy(DEFAULT_TABLE_RECOVERY)
    policy.update(validate_override({} if value is None else value, code=code))
    if override is not None:
        policy.update(validate_override(override, code=code))
    if type(job_seconds) not in {int, float} or not _finite(job_seconds) or job_seconds <= 0:
        raise ServiceError(code, 'jobSeconds must be finite and positive.')
    if policy['workSeconds'] is None:
        policy['workSeconds'] = .75 * job_seconds
    if policy['workSeconds'] >= job_seconds:
        raise ServiceError(code, 'tableRecovery.workSeconds must be less than jobSeconds.')
    return policy


def effective_profile(profile, override=None, *, code='INVALID_PROFILE'):
    result = copy.deepcopy(profile)
    result.pop('profileDigest', None)
    result.pop('_tableRecoveryOverride', None)
    options = result.setdefault('options', {})
    if 'tableRecovery' in options:
        validate_override(options['tableRecovery'], code=code)
    options['tableRecovery'] = resolve_policy(options.get('tableRecovery'),
        job_seconds=result['limits']['jobSeconds'], override=override, code=code)
    if override is not None:
        result['_tableRecoveryOverride'] = validate_override(override, code=code)
    result['profileDigest'] = hashlib.sha256(_encode(result).encode()).hexdigest()
    return result


def validate_page_range(policy, page_count):
    if any(page > page_count for page in policy['forcePages']):
        raise ServiceError('INVALID_REQUEST', 'tableRecovery.forcePages exceeds the PDF page count.')


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def semantic_fingerprint(source, profile, *, adapter_version=None):
    if adapter_version is None:
        from preprocessing.formats import get_adapter
        adapter_version = get_adapter('.pdf').version
    options = copy.deepcopy(profile.get('options', {}))
    policy = resolve_policy(options.get('tableRecovery'), job_seconds=profile['limits']['jobSeconds'])
    options['tableRecovery'] = {key: value for key, value in policy.items()
                               if key not in {'forcePages', 'workSeconds', 'maxModelCalls', 'maxAdditionalPasses'}}
    identity = {'sourceSha256': source['sha256'], 'adapterVersion': adapter_version,
                'build': profile.get('build'), 'parsers': {name: version(name) for name in ('pdfplumber', 'pypdfium2')},
                'recoveryVersions': RECOVERY_VERSIONS, 'options': options,
                'models': profile.get('models'), 'promptVersion': profile.get('promptVersion'),
                'schemaVersion': profile.get('schemaVersion')}
    return hashlib.sha256(_encode(identity).encode()).hexdigest()
