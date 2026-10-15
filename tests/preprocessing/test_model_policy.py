import copy
import json
import os
import stat
from types import SimpleNamespace

import pytest

from preprocessing.contracts import default_profile
from preprocessing.model_policy import ModelPolicy, read_trusted_json


def evidence():
    model = {'capabilities': {'tools': False, 'input': ['text', 'image'], 'output': ['text']},
             'limit': {'context': 131072, 'output': 1024}}
    declarations = {'providers': {'proxy': {'models': {name: copy.deepcopy(model) for name in ('a', 'b', 'c')}}}}
    catalog = [{'providerID': 'proxy', 'id': name, 'enabled': True,
                'capabilities': model['capabilities'], 'variants': [{'id': 'high'}]} for name in ('a', 'b', 'c')]
    policy = {'discovery_schema_version': 1, 'managed_access_enabled': True,
              'allowed_model_ids': ['a', 'b', 'c'], 'as_of': '2026-10-14T00:00:00Z',
              'fetched_at_monotonic_ms': 1000, 'boot_id': 'boot',
              'model_limits': {name: {'max_input_tokens': 128000, 'default_output_tokens': 1024,
                                     'max_output_tokens': 2048} for name in ('a', 'b', 'c')},
              'model_capabilities': {name: {'input': ['text', 'image'], 'output': ['text'],
                  'allowed_reasoning_efforts': ['high'], 'image_input': {'mime_types': ['image/png'],
                      'max_images': 4, 'max_bytes': 8 * 1024**2, 'max_pixels': 16_000_000,
                      'tokens_per_image': 4096, 'detail': 'original'}} for name in ('a', 'b', 'c')}}
    return policy, declarations, catalog


def make_policy(policy, declarations, **kwargs):
    return ModelPolicy('policy', 'declarations', reader=lambda path: copy.deepcopy(policy if str(path) == 'policy' else declarations),
                       clock_ms=lambda: 2000, boot_id=lambda: 'boot', min_context_tokens=32768,
                       min_output_tokens=512, **kwargs)


def profile(model='proxy/a'):
    value = default_profile()
    value['models']['vision'] = model
    return value


def test_ranking_and_freshness_never_refresh_age_on_read():
    policy, declarations, catalog = evidence()
    policy['model_capabilities']['a']['image_input'] = None
    selection = make_policy(policy, declarations, fallback_models=['proxy/c', 'proxy/b', 'proxy/c'])
    result = selection.select(profile(), catalog)
    assert result['selectedModel'] == 'proxy/c'
    assert result['eligibleCandidateCount'] == 2
    assert result['selectionReason'] == 'configured_fallback'
    assert result['policyAgeMs'] == 1000
    policy['fetched_at_monotonic_ms'] = 0
    selection.clock_ms = lambda: 120001
    assert selection.select(profile(), catalog)['selectionReason'] == 'policy_stale'
    assert selection.select(profile(), catalog)['selectedModel'] is None
    assert selection.pinned_hold(profile('proxy/c'), catalog) is None
    policy['fetched_at_monotonic_ms'] = 120000
    policy['allowed_model_ids'] = ['b']
    assert selection.pinned_hold(profile('proxy/c'), catalog) == 'model_not_accepted'
    policy['managed_access_enabled'] = False
    assert selection.pinned_hold(profile('proxy/b'), catalog) == 'managed_access_disabled'


@pytest.mark.parametrize('fault', ['text_only', 'disabled', 'duplicate', 'wrong_provider', 'missing_limit',
                                  'small_image', 'small_context', 'oversized_output', 'tools', 'wrong_boot'])
def test_ineligible_evidence_cannot_win(fault):
    policy, declarations, catalog = evidence()
    if fault == 'text_only':
        declarations['providers']['proxy']['models']['a']['capabilities']['input'] = ['text']
    elif fault == 'disabled':
        catalog[0]['enabled'] = False
    elif fault == 'duplicate':
        catalog.append(copy.deepcopy(catalog[0]))
    elif fault == 'wrong_provider':
        catalog[0]['providerID'] = 'public'
    elif fault == 'missing_limit':
        del policy['model_limits']['a']
    elif fault == 'small_image':
        policy['model_capabilities']['a']['image_input']['max_pixels'] = 10
    elif fault == 'small_context':
        policy['model_limits']['a']['max_input_tokens'] = 100
    elif fault == 'oversized_output':
        policy['model_limits']['a']['max_output_tokens'] = 512
    elif fault == 'tools':
        declarations['providers']['proxy']['models']['a']['capabilities']['tools'] = True
    elif fault == 'wrong_boot':
        policy['boot_id'] = 'old-boot'
    result = make_policy(policy, declarations, allow_fallback=False).select(profile(), catalog)
    assert result['selectedModel'] is None


def test_variant_is_never_stripped_or_copied_to_another_candidate():
    policy, declarations, catalog = evidence()
    selection = make_policy(policy, declarations)
    assert selection.select(profile('proxy/a#high'), catalog)['selectedModel'] == 'proxy/a#high'
    policy['model_capabilities']['a']['allowed_reasoning_efforts'] = []
    assert selection.select(profile('proxy/a#high'), catalog)['selectedModel'] is None
    selection = make_policy(policy, declarations, fallback_models=['proxy/b'])
    assert selection.select(profile('proxy/a#high'), catalog)['selectedModel'] == 'proxy/b'
    catalog[1]['variants'] = []
    selection = make_policy(policy, declarations, fallback_models=['proxy/b#high'])
    assert selection.select(profile('proxy/a#high'), catalog)['selectedModel'] is None


@pytest.mark.parametrize('detail,eligible', [('low', True), ('high', True), ('original', True), ('auto', False)])
def test_image_detail_eligibility_matches_qualified_backend_metadata(detail, eligible):
    policy, declarations, catalog = evidence()
    policy['model_capabilities']['a']['image_input']['detail'] = detail
    result = make_policy(policy, declarations, allow_fallback=False).select(profile(), catalog)
    assert result['selectedModel'] == ('proxy/a' if eligible else None)
    assert result['eligibleCandidateCount'] == (1 if eligible else 0)
    if not eligible:
        assert result['candidateReasons']['proxy/a'] == 'model_image_evidence_missing'


def test_optional_floor_and_malformed_policy_are_actionable():
    policy, declarations, catalog = evidence()
    selection = make_policy(policy, declarations)
    selection.min_context_tokens = None
    assert selection.select(profile(), catalog)['selectedModel'] == 'proxy/a'
    policy['managed_access_enabled'] = 'false'
    assert selection.select(profile(), catalog)['selectionReason'] == 'policy_invalid'


def luna_evidence():
    policy, declarations, catalog = evidence()
    name = 'openai/gpt-6-luna'
    declaration = declarations['providers']['proxy']['models']['a']
    declaration['limit'] = {'context': 1050000, 'output': 32000}
    declarations['providers']['proxy']['models'] = {name: declaration}
    catalog = [{**catalog[0], 'id': name}]
    caps = policy['model_capabilities']['a']
    caps['image_input']['tokens_per_image'] = 131072
    policy.update(allowed_model_ids=[name], model_capabilities={name: caps}, model_limits={name: {
        'max_input_tokens': 922000, 'default_output_tokens': 6000, 'max_output_tokens': 32000}})
    return policy, declarations, catalog


def derived_policy(policy, declarations, **kwargs):
    return ModelPolicy('policy', 'declarations', reader=lambda path: copy.deepcopy(policy if str(path) == 'policy' else declarations),
                       clock_ms=lambda: 2000, boot_id=lambda: 'boot', **kwargs)


def test_default_luna_derives_advisory_bounds_from_request_and_qualified_evidence():
    policy, declarations, catalog = luna_evidence()
    requested = profile('proxy/openai/gpt-6-luna')
    unchanged = copy.deepcopy(requested)
    resolved, selection = derived_policy(policy, declarations).resolve(requested, catalog)
    assert selection['selectedModel'] == 'proxy/openai/gpt-6-luna'
    assert selection['eligibleCandidateCount'] == 1
    assert requested == unchanged
    assert resolved['modelRequirements'] == {
        'derivedInputBound': 589824, 'minContextTokens': 589824, 'minOutputTokens': 32000,
        'operatorMinContextTokens': None, 'operatorMinOutputTokens': None}
    assert resolved['limits']['modelImages'] == 4
    assert resolved['limits']['requestBytes'] == 65536


@pytest.mark.parametrize('fault', ['missing_image_cost', 'missing_output', 'bad_backend_default',
                                  'backend_default_exceeds_max', 'small_backend_input', 'small_context_headroom',
                                  'missing_request_bytes', 'bad_image_count'])
def test_derived_selection_requires_valid_evidence_and_output_headroom(fault):
    policy, declarations, catalog = luna_evidence()
    name = 'openai/gpt-6-luna'
    requested = profile('proxy/' + name)
    if fault == 'missing_image_cost':
        del policy['model_capabilities'][name]['image_input']['tokens_per_image']
    elif fault == 'missing_output':
        del declarations['providers']['proxy']['models'][name]['limit']['output']
    elif fault == 'bad_backend_default':
        policy['model_limits'][name]['default_output_tokens'] = True
    elif fault == 'backend_default_exceeds_max':
        policy['model_limits'][name]['default_output_tokens'] = 32001
    elif fault == 'small_backend_input':
        policy['model_limits'][name]['max_input_tokens'] = 589823
    elif fault == 'small_context_headroom':
        declarations['providers']['proxy']['models'][name]['limit']['context'] = 589824 + 32000 - 1
    elif fault == 'missing_request_bytes':
        del requested['limits']['requestBytes']
    elif fault == 'bad_image_count':
        requested['limits']['modelImages'] = True
    assert derived_policy(policy, declarations).select(requested, catalog)['selectedModel'] is None


@pytest.mark.parametrize('input_floor,output_floor,eligible,input_requirement', [
    (None, None, True, 589824), (100, 100, True, 589824),
    (600000, 16000, True, 600000), (922001, None, False, None),
    (None, 32001, False, None),
])
def test_operator_floors_narrow_admission_without_fabricating_an_output_budget(input_floor, output_floor, eligible, input_requirement):
    policy, declarations, catalog = luna_evidence()
    selection = derived_policy(policy, declarations, min_context_tokens=input_floor, min_output_tokens=output_floor)
    resolved, _choice = selection.resolve(profile('proxy/openai/gpt-6-luna'), catalog)
    assert (resolved is not None) is eligible
    if eligible:
        requirements = resolved['modelRequirements']
        assert requirements['derivedInputBound'] == 589824
        assert requirements['minContextTokens'] == input_requirement
        assert requirements['minOutputTokens'] == 32000
        assert requirements['operatorMinContextTokens'] == input_floor
        assert requirements['operatorMinOutputTokens'] == output_floor


def test_pinned_derived_requirements_do_not_grow_when_fresh_image_cost_changes():
    policy, declarations, catalog = luna_evidence()
    selection = derived_policy(policy, declarations)
    pinned, _choice = selection.resolve(profile('proxy/openai/gpt-6-luna'), catalog)
    assert pinned is not None
    original = copy.deepcopy(pinned)
    name = 'openai/gpt-6-luna'
    policy['model_capabilities'][name]['image_input']['tokens_per_image'] = 524288
    assert selection.pinned_hold(pinned, catalog) is None
    assert selection.select(profile('proxy/' + name), catalog)['selectedModel'] is None
    selection.min_context_tokens = 1000000
    selection.min_output_tokens = 1000000
    assert selection.pinned_hold(pinned, catalog) is None
    assert selection.select(profile('proxy/' + name), catalog)['selectedModel'] is None
    policy['model_limits'][name]['max_input_tokens'] = 589823
    assert selection.pinned_hold(pinned, catalog) == 'model_token_limits_insufficient'
    assert pinned == original


def test_changed_installed_output_default_holds_pinned_budget_without_rewriting_it():
    policy, declarations, catalog = luna_evidence()
    selection = derived_policy(policy, declarations, min_output_tokens=16000)
    pinned, _choice = selection.resolve(profile('proxy/openai/gpt-6-luna'), catalog)
    assert pinned is not None
    original = copy.deepcopy(pinned)
    declarations['providers']['proxy']['models']['openai/gpt-6-luna']['limit']['output'] = 16000
    assert selection.pinned_hold(pinned, catalog) == 'model_output_default_changed'
    fresh, _choice = selection.resolve(profile('proxy/openai/gpt-6-luna'), catalog)
    assert fresh['modelRequirements']['minOutputTokens'] == 16000
    assert pinned == original


def test_fallback_derives_requirements_from_the_chosen_candidate():
    policy, declarations, catalog = evidence()
    for name in ('a', 'b', 'c'):
        declarations['providers']['proxy']['models'][name]['limit']['context'] = 131072
        policy['model_limits'][name]['max_input_tokens'] = 128000
    policy['model_capabilities']['a']['image_input']['tokens_per_image'] = 131072
    policy['model_capabilities']['b']['image_input']['tokens_per_image'] = 2048
    declarations['providers']['proxy']['models']['b']['limit']['output'] = 2048
    policy['model_limits']['b'].update(max_output_tokens=2048, default_output_tokens=128)
    resolved, choice = derived_policy(policy, declarations).resolve(profile(), catalog)
    assert choice['selectedModel'] == 'proxy/b'
    assert resolved['modelRequirements']['minContextTokens'] == 4 * 2048 + 65536
    assert resolved['modelRequirements']['minOutputTokens'] == 2048


def test_untrusted_file_and_symlinks_never_authorize(tmp_path):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(evidence()[0]))
    with pytest.raises(ValueError):
        read_trusted_json(path)
    link = tmp_path / 'link'
    link.symlink_to(path)
    with pytest.raises(ValueError):
        read_trusted_json(link)


@pytest.mark.parametrize('contents', ['{"ok":true}', '{"ok":true,"ok":false}', '{"ok":NaN}', '[]', ' ' * 65537])
def test_trusted_reader_bounds_and_strict_json(tmp_path, monkeypatch, contents):
    # Simulate root ownership of fixture inodes while exercising the real dirfd,
    # bounded read, duplicate-key parser, and symlink rejection paths as UID 1000.
    original = os.fstat
    def root_owned(fd):
        info = original(fd)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022,
            st_size=info.st_size, st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
    monkeypatch.setattr(os, 'fstat', root_owned)
    path = tmp_path / 'projection.json'
    path.write_text(contents)
    if contents == '{"ok":true}':
        assert read_trusted_json(path) == {'ok': True}
        path.unlink()
        os.mkfifo(path)
        with pytest.raises(ValueError):
            read_trusted_json(path)
        path.unlink()
        target = tmp_path / 'target.json'
        target.write_text('{"ok":true}')
        path.symlink_to(target)
        with pytest.raises(ValueError):
            read_trusted_json(path)
    else:
        with pytest.raises(ValueError):
            read_trusted_json(path)


def test_writable_or_nonroot_regular_file_rejected_even_with_trusted_directories(tmp_path, monkeypatch):
    original = os.fstat
    def trust_directories(fd):
        info = original(fd)
        if stat.S_ISDIR(info.st_mode):
            return SimpleNamespace(st_uid=0, st_mode=info.st_mode & ~0o022)
        return info
    monkeypatch.setattr(os, 'fstat', trust_directories)
    path = tmp_path / 'projection.json'
    path.write_text('{"allowed":true}')
    with pytest.raises(ValueError):
        read_trusted_json(path)
