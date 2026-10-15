import hashlib
import json

import pytest
from resume_fixtures import build_parent_evidence

from preprocessing.contracts import default_profile
from preprocessing.errors import ServiceError
from preprocessing.indexes import build_indexes
from preprocessing.resume_evidence import (
    prepare_resume,
    validate_parent,
    validate_resume_output,
)
from preprocessing.service import Service
from preprocessing.table_recovery import effective_profile


@pytest.fixture
def parent_evidence(tmp_path):
    parent = {'id': 'job_fixture', 'revision': 1, 'profile': effective_profile(default_profile())}
    private = tmp_path / 'state' / '.preprocessing'
    sealed_root = private / 'jobs' / parent['id'] / 'sealed'
    snapshot_root = private / 'sources' / 'source_fixture'
    manifest = build_parent_evidence(parent, sealed_root, snapshot_root)
    return parent, sealed_root, snapshot_root, manifest


def test_resume_finalization_rejects_staged_immutable_bytes_before_rehash(tmp_path, parent_evidence):
    parent, sealed_root, snapshot_root, _ = parent_evidence
    service = Service(tmp_path / 'state', tmp_path)
    profile = effective_profile(default_profile())
    job = {'id': 'job_child', 'resumeOf': parent['id'], 'source': parent['source'], 'profile': profile,
           'requestedSource': {'kind': 'snapshot', 'snapshotId': parent['source']['snapshotId']}}
    staging = tmp_path / 'resumed'
    staging.mkdir()
    validated = validate_parent(parent, profile, sealed_root=sealed_root, snapshot_root=snapshot_root)
    resume = prepare_resume(validated, staging, profile=profile)
    extraction = resume['previousExtraction']
    extraction['products'].append({'path': 'work-plan.json', 'kind': 'work-plan', 'mediaType': 'application/json'})
    (staging / 'work-plan.json').write_text(json.dumps(resume['workPlan']))
    build_indexes(extraction, staging, profile['limits'])
    validate_resume_output(extraction, staging, resume)
    retained = (staging / 'table_1.txt').read_bytes()
    (staging / 'table_1.txt').write_bytes(retained.replace(b'retained', b'modified'))
    with pytest.raises(ServiceError) as error:
        service._finalize(job, extraction, staging, resume)
    assert error.value.code == 'OUTPUT_INVALID'
    assert not (staging / 'manifest.json').exists()


def test_resume_copies_sealed_evidence_and_invalidates_explicit_force_only(tmp_path, parent_evidence):
    parent, sealed, snapshot_root, manifest = parent_evidence
    before = {p.relative_to(sealed).as_posix(): p.read_bytes() for p in sealed.rglob('*') if p.is_file()}
    profile = effective_profile(default_profile(), {'workSeconds': 3000, 'maxAdditionalPasses': 2})
    validated = validate_parent(parent, profile, sealed_root=sealed, snapshot_root=snapshot_root)
    staging = tmp_path / 'new-staging'
    staging.mkdir()
    descriptor = prepare_resume(validated, staging, profile=profile)
    assert [unit['id'] for unit in descriptor['completedUnits']] == ['page_1', 'tile_1']
    assert descriptor['workPlan']['units'][1]['passesSpent'] == 1
    assert descriptor['previousExtraction']['coverage']['total'] == 2
    assert (staging / 'page_1.txt').read_bytes() == before['page_1.txt']
    assert (staging / 'page_1.txt').stat().st_ino != (sealed / 'page_1.txt').stat().st_ino
    retained = next(p for p in manifest['products'] if p['path'] == 'table_1.txt')
    copied = next(p for p in descriptor['products'] if p['path'] == retained['path'])
    for field in ('source', 'render', 'model'):
        assert copied[field] == retained[field]
    (staging / 'page_1.txt').write_text('new generation changes')
    assert {p.relative_to(sealed).as_posix(): p.read_bytes() for p in sealed.rglob('*') if p.is_file()} == before
    forced = effective_profile(profile, {'forcePages': [1]})
    other = tmp_path / 'forced-staging'
    other.mkdir()
    assert prepare_resume(validated, other, profile=forced)['completedUnits'] == []


@pytest.mark.parametrize('fault, expected', [
    ('model', 'RESUME_INCOMPATIBLE'), ('tamper', 'RESUME_INVALID'),
    ('manifest', 'RESUME_INVALID'), ('closure', 'RESUME_INVALID'),
])
def test_resume_rejects_invalid_or_incompatible_evidence(parent_evidence, fault, expected):
    parent, sealed, snapshot_root, manifest = parent_evidence
    profile = effective_profile(default_profile())
    if fault == 'model':
        profile['models']['vision'] = 'changed/model'
    elif fault == 'tamper':
        (sealed / 'page_1.txt').write_text('changed')
    elif fault == 'manifest':
        (sealed / 'manifest.json').write_text('{}')
    elif fault == 'closure':
        plan = json.loads((sealed / 'work-plan.json').read_text())
        plan['units'][1]['dependencies'] = ['page_2']
        (sealed / 'work-plan.json').write_text(json.dumps(plan))
        product = next(p for p in manifest['products'] if p['kind'] == 'work-plan')
        raw = (sealed / 'work-plan.json').read_bytes()
        product.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        (sealed / 'manifest.json').write_text(json.dumps(manifest))
        parent['sealedManifestSha256'] = hashlib.sha256((sealed / 'manifest.json').read_bytes()).hexdigest()
    with pytest.raises(ServiceError) as error:
        validate_parent(parent, profile, sealed_root=sealed, snapshot_root=snapshot_root)
    assert error.value.code == expected


def test_resume_output_rejects_discarded_completed_work(tmp_path, parent_evidence):
    parent, sealed, snapshot_root, _ = parent_evidence
    profile = effective_profile(default_profile())
    staging = tmp_path / 'resumed'
    staging.mkdir()
    validated = validate_parent(parent, profile, sealed_root=sealed, snapshot_root=snapshot_root)
    resume = prepare_resume(validated, staging, profile=profile)
    extraction = resume['previousExtraction']
    with pytest.raises(ServiceError) as error:
        validate_resume_output(extraction, staging, resume)
    assert error.value.code == 'OUTPUT_INVALID'
    extraction['products'].append({'path': 'work-plan.json', 'kind': 'work-plan', 'mediaType': 'application/json'})
    plan = json.loads(json.dumps(resume['workPlan']))
    (staging / 'work-plan.json').write_text(json.dumps(plan))
    validate_resume_output(extraction, staging, resume)
    plan['units'][1]['state'] = 'pending'
    (staging / 'work-plan.json').write_text(json.dumps(plan))
    with pytest.raises(ServiceError) as error:
        validate_resume_output(extraction, staging, resume)
    assert error.value.code == 'OUTPUT_INVALID'
