"""Trusted discovery evidence and deterministic new-job selection; never inference failover.

Input admission bounds combine qualified image cost with explicit request-byte
headroom. They are advisory bounds, not measured tokenizer counts. Output uses
the installed declaration; optional operator floors can only narrow selection.
"""
import contextlib
import copy
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path

MAX_BYTES = 65536
MAX_MODELS = 128
MAX_INT = 2**53 - 1
REFERENCE = re.compile(r'proxy/([A-Za-z0-9_./:-]{1,256})(?:#([A-Za-z0-9_.-]{1,64}))?')


def positive_int(value, maximum=MAX_INT):
    return type(value) is int and 0 < value <= maximum


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def read_trusted_json(path):
    """Read a root-owned regular file through root-owned non-writable directories.

    dirfd traversal pins each ancestor, rejects symlinks and processor-writable
    handoffs, and bounds reads even if metadata lies or the file changes in place.
    Runtime uses atomic replacement (root:996 0640 in a root-owned directory).
    """
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Trusted paths must be absolute')
    try:
        with contextlib.ExitStack() as stack:
            # O_PATH pins directories without requiring read permission; the
            # processor only needs execute permission to traverse root:996 0710.
            fd = os.open('/', os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            for part in path.parts[1:-1]:
                fd = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise ValueError('Untrusted directory')
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            if (before.st_uid != 0 or before.st_mode & 0o022 or not stat.S_ISREG(before.st_mode)
                    or before.st_size > MAX_BYTES):
                raise ValueError('Untrusted file')
            with os.fdopen(os.dup(fd), 'rb') as handle:
                raw = handle.read(MAX_BYTES + 1)
            after = os.fstat(fd)
            if (len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError('Changed or oversized file')
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=lambda _: _reject())
        if not isinstance(value, dict):
            raise TypeError('Expected object')
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError('Trusted model evidence is unavailable or invalid') from None


def _reject():
    raise ValueError('Nonfinite JSON')


def references(values):
    if (not isinstance(values, list) or len(values) > MAX_MODELS
            or any(not isinstance(v, str) or REFERENCE.fullmatch(v) is None for v in values)):
        raise ValueError('Use a bounded JSON array of exact proxy/model references')
    return list(dict.fromkeys(values))


class ModelPolicy:
    def __init__(self, path, declarations, *, allow_fallback=True, fallback_models=(), max_age_ms=120000,
                 min_context_tokens=None, min_output_tokens=None, reader=read_trusted_json,
                 clock_ms=None, boot_id=None):
        if (type(allow_fallback) is not bool or not positive_int(max_age_ms)
                or any(v is not None and not positive_int(v, 2**31 - 1)
                       for v in (min_context_tokens, min_output_tokens))):
            raise ValueError('Invalid model selection limits')
        self.path, self.declarations_path = path, declarations
        self.allow_fallback = allow_fallback
        self.fallback_models = references(list(fallback_models))
        self.max_age_ms = max_age_ms
        self.min_context_tokens, self.min_output_tokens = min_context_tokens, min_output_tokens
        self.reader = reader
        self.clock_ms = clock_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self.boot_id = boot_id or (lambda: Path('/proc/sys/kernel/random/boot_id').read_text().strip())

    def snapshot(self):
        try:
            value = self.reader(self.path)
            allowed = value.get('allowed_model_ids')
            fetched = value.get('fetched_at_monotonic_ms')
            if (type(value.get('discovery_schema_version')) is not int or value['discovery_schema_version'] != 1
                    or type(value.get('managed_access_enabled')) is not bool
                    or not isinstance(allowed, list) or len(allowed) > MAX_MODELS
                    or any(not isinstance(v, str) or REFERENCE.fullmatch('proxy/' + v) is None or '#' in v for v in allowed)
                    or len(set(allowed)) != len(allowed)
                    or not isinstance(value.get('model_limits'), dict)
                    or not isinstance(value.get('model_capabilities'), dict)
                    or type(fetched) is not int or not 0 <= fetched <= MAX_INT
                    or not isinstance(value.get('boot_id'), str) or not value['boot_id']
                    or not isinstance(value.get('as_of'), str) or len(value['as_of']) > 128
                    or datetime.fromisoformat(value['as_of']).tzinfo is None):
                raise ValueError('Invalid discovery projection')
            age = self.clock_ms() - fetched
            if value['boot_id'] != self.boot_id() or age < 0:
                return value, None, 'policy_wrong_boot_or_clock'
            return value, age, 'policy_stale' if age > self.max_age_ms else None
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return None, None, 'policy_invalid'

    def _declarations(self):
        try:
            models = self.reader(self.declarations_path)['providers']['proxy']['models']
            if not isinstance(models, dict) or len(models) > MAX_MODELS:
                raise ValueError('Invalid declarations')
            return models
        except (OSError, ValueError, TypeError, KeyError):
            return {}

    def _candidate(self, reference, profile, catalog, policy, declarations, *, pinned=False):
        """Return (reason, requirements); derive bounds only for new selections."""
        match = REFERENCE.fullmatch(reference) if isinstance(reference, str) else None
        if match is None:
            return 'invalid_model_reference', None
        model_id, variant = match.groups()
        if policy['managed_access_enabled'] is not True:
            return 'managed_access_disabled', None
        if model_id not in policy['allowed_model_ids']:
            return 'model_not_accepted', None
        declared = declarations.get(model_id)
        if not isinstance(declared, dict):
            return 'model_not_declared', None
        models = [m for m in catalog if isinstance(m, dict) and m.get('providerID') == 'proxy' and m.get('id') == model_id]
        if len(models) != 1 or models[0].get('enabled') is not True:
            return 'model_unavailable_or_ambiguous', None
        backend = policy['model_capabilities'].get(model_id)
        limits = policy['model_limits'].get(model_id)
        try:
            for caps in (declared['capabilities'], models[0]['capabilities'], backend):
                if (not isinstance(caps, dict) or not isinstance(caps.get('input'), list)
                        or not {'text', 'image'} <= set(caps['input']) or caps.get('output') != ['text']):
                    return 'model_modality_unqualified', None
            if declared['capabilities'].get('tools') is not False:
                return 'model_tools_enabled', None
            if models[0]['capabilities'].get('tools') is True:
                return 'model_tools_enabled', None
            efforts = backend.get('allowed_reasoning_efforts')
            if (not isinstance(efforts, list) or len(efforts) > 32
                    or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', v) for v in efforts)):
                return 'reasoning_evidence_missing', None
            if variant:
                declared_variants = declared.get('variants')
                catalog_variants = models[0].get('variants')
                if (not isinstance(declared_variants, list) or not isinstance(catalog_variants, list)
                        or any(not isinstance(v, dict) for v in declared_variants + catalog_variants)):
                    return 'model_variant_unqualified', None
                selected = [v for v in declared_variants if v.get('id') == variant]
                observed = [v for v in catalog_variants if v.get('id') == variant]
                if len(selected) != 1 or len(observed) != 1:
                    return 'model_variant_unqualified', None
                settings = selected[0].get('settings')
                reasoning = settings.get('reasoning') if isinstance(settings, dict) else None
                if (set(selected[0]) != {'id', 'settings'}
                        or not isinstance(settings, dict) or set(settings) != {'reasoning'}
                        or not isinstance(reasoning, dict) or set(reasoning) != {'effort'}
                        or not isinstance(reasoning['effort'], str)
                        or reasoning['effort'] not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'}
                        or reasoning['effort'] not in efforts or observed[0].get('settings') != settings):
                    return 'model_variant_unqualified', None
            declared_limits = declared['limit']
            if (not isinstance(declared_limits, dict)
                    or set(declared_limits) not in ({'context', 'output'}, {'context', 'input', 'output'})
                    or any(not positive_int(declared_limits.get(k), 2**31 - 1) for k in ('context', 'output'))
                    or ('input' in declared_limits and not positive_int(declared_limits['input'], 2**31 - 1))
                    or ('input' in declared_limits and declared_limits['input'] > declared_limits['context'] - declared_limits['output'])
                    or any(not positive_int(limits.get(k), 2**31 - 1)
                           for k in ('default_output_tokens', 'max_output_tokens', 'max_input_tokens'))):
                return 'model_limit_evidence_missing', None
            image = backend['image_input']
            if (not isinstance(image, dict) or not isinstance(image.get('mime_types'), list)
                    or not image['mime_types'] or len(image['mime_types']) > 8
                    or any(v not in ('image/png', 'image/jpeg', 'image/webp') for v in image['mime_types'])
                    or 'image/png' not in image['mime_types'] or image.get('detail') not in ('low', 'high', 'original')
                    or any(not positive_int(image.get(k)) for k in ('max_images', 'max_bytes', 'max_pixels', 'tokens_per_image'))):
                return 'model_image_evidence_missing', None
            needs = profile['limits']
            if any(not positive_int(needs.get(key)) for key in ('modelImages', 'requestBytes', 'pixels')):
                return 'request_bound_evidence_missing_or_invalid', None
            if (image['max_images'] < needs['modelImages']
                    or image['max_bytes'] < needs.get('modelImageBytes', 8 * 1024**2)
                    or image['max_pixels'] < needs['pixels']):
                return 'model_image_limits_insufficient', None
            if pinned:
                requirements = profile.get('modelRequirements')
            else:
                # The byte allowance is conservative admission headroom, not a
                # tokenizer conversion. Never derive a new output request budget.
                derived = image['tokens_per_image'] * needs['modelImages'] + needs['requestBytes']
                requirements = {'derivedInputBound': derived,
                    'minContextTokens': max(derived, self.min_context_tokens or 0),
                    'minOutputTokens': declared_limits['output'],
                    'operatorMinContextTokens': self.min_context_tokens,
                    'operatorMinOutputTokens': self.min_output_tokens}
            if (not isinstance(requirements, dict)
                    or set(requirements) != {'derivedInputBound', 'minContextTokens', 'minOutputTokens',
                                             'operatorMinContextTokens', 'operatorMinOutputTokens'}
                    or any(not positive_int(requirements.get(key), 2**31 - 1)
                           for key in ('derivedInputBound', 'minContextTokens', 'minOutputTokens'))
                    or any(value is not None and not positive_int(value, 2**31 - 1)
                           for value in (requirements['operatorMinContextTokens'], requirements['operatorMinOutputTokens']))):
                return 'request_token_requirements_invalid', None
            min_context, min_output = requirements['minContextTokens'], requirements['minOutputTokens']
            output_floor = requirements['operatorMinOutputTokens'] or 0
            if (requirements['derivedInputBound'] < needs['requestBytes']
                    or min_context != max(requirements['derivedInputBound'], requirements['operatorMinContextTokens'] or 0)):
                return 'request_token_requirements_invalid', None
            if pinned and declared_limits['output'] != min_output:
                return 'model_output_default_changed', None
            if (declared_limits['context'] < min_context
                    or declared_limits['context'] < min_context + min_output
                    or ('input' in declared_limits and declared_limits['input'] < min_context)
                    or limits['max_input_tokens'] < min_context
                    or min_output < output_floor
                    or min_output > limits['max_output_tokens']
                    or limits['default_output_tokens'] > limits['max_output_tokens']):
                return 'model_token_limits_insufficient', None
            return None, requirements
        except (KeyError, TypeError, AttributeError):
            return 'model_capability_or_limit_evidence_missing', None

    def _select(self, profile, catalog):
        preferred = profile['models'].get('vision')
        policy, age, problem = self.snapshot()
        result = {'preferredModel': preferred, 'selectedModel': None, 'eligibleCandidateCount': 0,
                  'selectionReason': problem, 'policyAgeMs': age, 'candidateReasons': {}}
        if problem:
            return result, None
        selected_requirements = None
        declarations = self._declarations()
        order = [preferred]
        if self.allow_fallback:
            order.extend(self.fallback_models)
            # An explicit variant requires explicitly configured alternative references.
            if isinstance(preferred, str) and '#' not in preferred:
                order.extend('proxy/' + v for v in sorted(policy['allowed_model_ids']))
        for reference in dict.fromkeys(order):
            reason, requirements = self._candidate(reference, profile, catalog, policy, declarations)
            if reason:
                result['candidateReasons'][str(reference)] = reason
                continue
            result['eligibleCandidateCount'] += 1
            if result['selectedModel'] is None:
                selected_requirements = requirements
                result.update(selectedModel=reference, selectionReason='preferred' if reference == preferred else
                              'configured_fallback' if reference in self.fallback_models else 'canonical_fallback')
        if result['selectedModel'] is None:
            result['selectionReason'] = 'managed_access_disabled' if not policy['managed_access_enabled'] else 'no_eligible_models'
        return result, selected_requirements

    def select(self, profile, catalog):
        return self._select(profile, catalog)[0]

    def resolve(self, profile, catalog):
        selection, requirements = self._select(profile, catalog)
        if selection['selectedModel'] is None:
            return None, selection
        resolved = copy.deepcopy(profile)
        resolved['models'] = dict.fromkeys(profile['models'], selection['selectedModel'])
        # Use the exact evidence from ranking, not a second snapshot or later cost.
        resolved['modelRequirements'] = copy.deepcopy(requirements)
        return resolved, selection

    def pinned_hold(self, profile, catalog):
        policy, _, problem = self.snapshot()
        # Stale/missing discovery is not fresh revocation of a durable selection.
        if problem:
            return None
        declarations = self._declarations()
        for reference in profile['models'].values():
            reason, _requirements = self._candidate(reference, profile, catalog, policy, declarations, pinned=True)
            if reason:
                return reason
        return None
