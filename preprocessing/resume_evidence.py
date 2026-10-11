"""Validate retained generations, copy reusable files, and check successor evidence."""
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from preprocessing.errors import ServiceError
from preprocessing.files import digest_file, open_regular, relative_parts
from preprocessing.results import validate_generation
from preprocessing.store import encode
from preprocessing.table_recovery import semantic_fingerprint


@dataclass(frozen=True)
class ValidatedResume:
    """Detached parent identity and evidence validated at concrete artifact roots."""

    parent: dict
    sealed_root: Path
    snapshot_root: Path
    manifest: dict
    work_plan: dict


def validate_parent(parent: dict, profile: dict, *, sealed_root: Path, snapshot_root: Path) -> ValidatedResume:
    root, snapshot_root = Path(sealed_root), Path(snapshot_root)
    try:
        parent = json.loads(encode(parent))
        with open_regular(root, 'manifest.json') as handle:
            raw = handle.read()
        if hashlib.sha256(raw).hexdigest() != parent.get('sealedManifestSha256'):
            raise ValueError('manifest digest')
        manifest = json.loads(raw)
        if (manifest['jobId'] != parent['id'] or manifest['resultId'] != parent['resultId']
                or manifest['source'] != parent['source'] or manifest['pipeline'] != parent['profile']):
            raise ValueError('manifest identity')
        validate_generation(root, manifest)
        with open_regular(snapshot_root, 'source') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != parent['source']['sha256']:
                raise ValueError('source digest')
        plans = [item for item in manifest['products'] if item['kind'] == 'work-plan']
        if len(plans) != 1:
            raise ValueError('missing or ambiguous work plan')
        with open_regular(root, plans[0]['path']) as handle:
            raw_plan = handle.read()
        if len(raw_plan) != plans[0]['bytes'] or hashlib.sha256(raw_plan).hexdigest() != plans[0]['sha256']:
            raise ValueError('work plan changed during validation')
        plan = json.loads(raw_plan)
        fingerprint = semantic_fingerprint(parent['source'], parent['profile'])
        if plan.get('fingerprint') != fingerprint:
            raise ValueError('work plan semantic identity')
        if fingerprint != semantic_fingerprint(parent['source'], profile):
            raise ServiceError('RESUME_INCOMPATIBLE', 'Resume source, build, parser, policy, prompt or requested model settings changed.')
        units = {unit['id']: unit for unit in plan['units']}
        paths = {product['path'] for product in manifest['products']}
        for unit in units.values():
            if (unit['state'] == 'completed'
                    and any(units[dep]['state'] != 'completed' for dep in unit['dependencies'])):
                raise ValueError('completed unit has unfinished dependencies')
            if unit.get('fingerprint', fingerprint) != fingerprint:
                raise ValueError('unit semantic identity')
            evidence = unit['evidencePaths']
            if isinstance(evidence, dict):
                evidence = list(evidence.values())
            if not isinstance(evidence, list) or any(path not in paths for path in evidence):
                raise ValueError('unit evidence closure')
        return ValidatedResume(parent, root, snapshot_root, manifest, plan)
    except ServiceError as exc:
        if exc.code == 'RESUME_INCOMPATIBLE':
            raise
        raise ServiceError('RESUME_INVALID', 'Parent sealed evidence or work-plan validation failed.') from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ServiceError('RESUME_INVALID', 'Parent sealed evidence or work-plan validation failed.') from None


def prepare_resume(validated: ValidatedResume, staging: Path, *, profile: dict) -> dict:
    parent, manifest, plan = validated.parent, validated.manifest, validated.work_plan
    # Copies are independent mutable files; sealed generations are never linked.
    excluded = {'navigation-index', 'table-preview', 'figure-index', 'agent-guide', 'work-plan'}
    products = [item for item in manifest['products'] if item['kind'] not in excluded]
    limits = profile['limits']
    if (sum(table['rows'] for table in manifest['tables']) > limits['rows']
            or any(len(table['columns']) > limits['columns'] for table in manifest['tables'])):
        raise ServiceError('RESOURCE_LIMIT', 'Retained tables exceed the new generation row or column limit.')
    if sum(item['bytes'] for item in products) > profile['limits']['outputBytes']:
        raise ServiceError('RESOURCE_LIMIT', 'Reusable evidence exceeds the new generation output-byte limit.')
    root = validated.sealed_root
    copied = []
    for item in products:
        target = staging.joinpath(*relative_parts(item['path']))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open_regular(root, item['path']) as source, target.open('xb') as output:
            shutil.copyfileobj(source, output)
        if target.stat().st_size != item['bytes'] or digest_file(target) != item['sha256']:
            raise ServiceError('RESUME_INVALID', 'Parent evidence changed while copying.')
        # Source locators, render transforms and model/attempt metadata are
        # evidence. Only generation-specific publication identities change.
        copied.append(json.loads(encode({key: value for key, value in item.items()
                                         if key not in {'id', 'contentUrl'}})))
    # Explicit new force pages invalidate their dependent units, not whole jobs.
    force_pages = profile.get('_tableRecoveryOverride', {}).get('forcePages', [])
    invalidated = {unit['id'] for unit in plan['units'] if unit.get('source', {}).get('page') in force_pages}
    changed = True
    while changed:
        more = {unit['id'] for unit in plan['units'] if invalidated.intersection(unit['dependencies'])}
        changed = not more <= invalidated
        invalidated.update(more)
    plan = json.loads(encode(plan))
    completed = []
    for unit in plan['units']:
        if unit['id'] in invalidated:
            unit.update(state='pending', reason='explicit-force-page')
            unit.pop('reusedFromJob', None)
        elif unit['state'] == 'completed':
            unit['reusedFromJob'] = parent['id']
            completed.append(unit)
    extraction = {key: manifest[key] for key in ('coverage', 'stages', 'tables', 'figures', 'findings', 'provenance')}
    extraction = json.loads(encode(extraction))
    extraction['products'] = copied
    for table in extraction['tables']:
        for key in ('rowsUrl', 'downloads'):
            table.pop(key, None)
    for figure in extraction['figures']:
        for key in ('imageProductId', 'ocrProductId', 'interpretationProductId'):
            figure.pop(key, None)
    histories = {}
    completed_tables = {unit.get('regionId') for unit in completed if unit['kind'] == 'table-confirmation'}
    copied_paths = {item['path'] for item in copied}
    for table in extraction['tables']:
        if table.get('source', {}).get('page') in force_pages:
            continue
        path = table.get('evidencePaths', {}).get('decisions')
        if path:
            with open_regular(staging, path) as handle:
                history = json.load(handle)
            comparisons = history.get('comparisons', [])
            if not isinstance(comparisons, list):
                raise ServiceError('RESUME_INVALID', 'Parent comparison history is malformed.')
            histories[table['id']] = {
                'comparisonCount': len(comparisons),
                'comparisonsDigest': hashlib.sha256(encode(comparisons).encode()).hexdigest(),
                'evidencePaths': history.get('evidencePaths', []),
            }
        if table['id'] in completed_tables:
            table_paths = [table['typedPath'], table['csvPath'], *table.get('evidencePaths', {}).values()]
            if any(path not in copied_paths for path in table_paths):
                raise ServiceError('RESUME_INVALID', 'Completed table dependency evidence was not copied.')
    return {'reusedFromJob': parent['id'], 'previousExtraction': extraction,
            'products': copied, 'workPlan': plan, 'completedUnits': completed,
            'forcePages': force_pages, 'invalidatedUnitIds': sorted(invalidated),
            'tableHistories': histories,
            'maxAdditionalPasses': profile['options']['tableRecovery']['maxAdditionalPasses']}


def validate_resume_output(extraction: dict, staging: Path, resume: dict) -> None:
    try:
        plans = [product for product in extraction['products'] if product['kind'] == 'work-plan']
        if len(plans) != 1:
            raise ValueError('resume requires one work plan')
        with open_regular(staging, plans[0]['path']) as handle:
            plan = json.load(handle)
        if plan['fingerprint'] != resume['workPlan']['fingerprint']:
            raise ValueError('resume semantic identity changed')
        units = {unit['id']: unit for unit in plan['units']}
        products = {item['path']: item for item in extraction['products']}
        old_products = {item['path']: item for item in resume['products']}
        immutable_paths = set()
        completed_tables = set()
        for reused in resume['completedUnits']:
            unit = units[reused['id']]
            if unit['state'] != 'completed' or unit.get('reusedFromJob') != resume['reusedFromJob']:
                raise ValueError('resume discarded completed work')
            for key in ('kind', 'source', 'fingerprint', 'candidateId', 'regionId', 'passId', 'tile', 'operationKind'):
                if unit.get(key) != reused.get(key):
                    raise ValueError('reused unit identity changed')
            if not set(reused['dependencies']) <= set(unit['dependencies']):
                raise ValueError('completed dependency closure was dropped')
            if not set(reused['evidencePaths']) <= set(unit['evidencePaths']):
                raise ValueError('completed evidence references were dropped')
            for path in reused['evidencePaths']:
                if path not in products:
                    raise ValueError('reused evidence product was dropped')
                if old_products[path]['kind'] in {'table-image', 'table-model-response', 'typed-table',
                                                'table-export', 'table-structure', 'table-recovery-audit'}:
                    immutable_paths.add(path)
            if reused['kind'] == 'table-confirmation' and reused.get('regionId'):
                completed_tables.add(reused['regionId'])
        invalidated = set(resume.get('invalidatedUnitIds', []))
        for old_unit in resume['workPlan']['units']:
            if old_unit['id'] in invalidated or 'passesSpent' not in old_unit:
                continue
            spent = units[old_unit['id']].get('passesSpent')
            old_spent = old_unit['passesSpent']
            allowance = max(old_spent, resume.get('maxAdditionalPasses', old_spent))
            if type(spent) is not int or not old_spent <= spent <= allowance:
                raise ValueError('cumulative dispute allowance changed')
        tables = {table['id']: table for table in extraction['tables']}
        confirmations = {unit['regionId']: unit for unit in units.values()
                         if unit['kind'] == 'table-confirmation' and unit.get('regionId')}
        for old_table in resume['previousExtraction']['tables']:
            if old_table['id'] in completed_tables:
                table = tables[old_table['id']]
                if any(table.get(key) != value for key, value in old_table.items()):
                    raise ValueError('completed table selection or references changed')
            history = resume.get('tableHistories', {}).get(old_table['id'])
            if history and old_table['id'] in tables:
                with open_regular(staging, tables[old_table['id']]['evidencePaths']['decisions']) as handle:
                    decisions = json.load(handle)
                prefix = decisions.get('comparisons', [])[:history['comparisonCount']]
                if hashlib.sha256(encode(prefix).encode()).hexdigest() != history['comparisonsDigest']:
                    raise ValueError('prior comparison history was dropped or changed')
                if not set(history['evidencePaths']) <= set(decisions.get('evidencePaths', [])):
                    raise ValueError('prior decision evidence was dropped')
                if old_table['id'] in confirmations:
                    spent = decisions.get('passesSpent')
                    if type(spent) is not int or spent != confirmations[old_table['id']]['passesSpent']:
                        raise ValueError('decision audit and work plan disagree on cumulative passes')
        for path in immutable_paths:
            old = old_products[path]
            if any(products[path].get(key) != old.get(key) for key in ('kind', 'source', 'render', 'model')):
                raise ValueError('reused evidence metadata changed')
            with open_regular(staging, path) as handle:
                if hashlib.file_digest(handle, 'sha256').hexdigest() != old['sha256']:
                    raise ValueError('reused evidence bytes changed')
    except (OSError, ValueError, TypeError, KeyError, ServiceError, RecursionError):
        raise ServiceError('OUTPUT_INVALID', 'Resumed extraction must retain completed evidence, selection, cumulative passes and comparison history.') from None
