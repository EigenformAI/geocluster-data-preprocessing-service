import hashlib
import json

from preprocessing.indexes import build_indexes
from preprocessing.results import make_manifest
from preprocessing.table_recovery import effective_profile, semantic_fingerprint


def build_parent_evidence(parent, sealed_root, snapshot_root):
    source = b'%PDF retained source fixture'
    snapshot_root.mkdir(parents=True)
    (snapshot_root / 'source').write_bytes(source)
    parent['profile'] = effective_profile(parent['profile'], {'forcePages': [1]})
    parent.update(state='partial', source={'snapshotId': 'source_fixture', 'sha256': hashlib.sha256(source).hexdigest(),
                                         'bytes': len(source), 'filename': 'a.pdf'})
    sealed_root.mkdir(parents=True)
    (sealed_root / 'page_1.txt').write_text('retained page one')
    (sealed_root / 'table_1.txt').write_text('retained table response')
    plan = {'version': 1, 'fingerprint': semantic_fingerprint(parent['source'], parent['profile']),
            'units': [
                {'id': 'page_1', 'kind': 'page-native', 'source': {'page': 1}, 'state': 'completed',
                 'dependencies': [], 'evidencePaths': ['page_1.txt']},
                {'id': 'tile_1', 'kind': 'table-read', 'source': {'page': 1}, 'state': 'completed',
                 'dependencies': ['page_1'], 'evidencePaths': ['page_1.txt', 'table_1.txt'], 'passesSpent': 1},
                {'id': 'page_2', 'kind': 'page-native', 'source': {'page': 2}, 'state': 'pending',
                 'dependencies': [], 'evidencePaths': []},
            ]}
    (sealed_root / 'work-plan.json').write_text(json.dumps(plan))
    extraction = {'products': [{'path': 'page_1.txt', 'kind': 'page-text', 'mediaType': 'text/plain'},
                               {'path': 'table_1.txt', 'kind': 'table-model-response', 'mediaType': 'text/plain',
                                'source': {'page': 1}, 'render': {'pixelsPerPoint': 2},
                                'model': {'requestedModel': 'fixture/vision', 'operationId': 'old-operation'}},
                               {'path': 'work-plan.json', 'kind': 'work-plan', 'mediaType': 'application/json'}],
                  'tables': [], 'figures': [], 'findings': [], 'provenance': {},
                  'coverage': {'enumeration': 'complete', 'total': 2, 'completed': 1, 'failed': 0,
                               'unsupported': 0, 'notProcessed': 1},
                  'stages': [{'id': 'native', 'state': 'partial', 'required': True}]}
    build_indexes(extraction, sealed_root, parent['profile']['limits'])
    manifest = make_manifest(parent, extraction, sealed_root)
    parent.update(resultId=manifest['resultId'], documents={'state': 'pending', 'path': None},
                  sealedManifestSha256=hashlib.sha256((sealed_root / 'manifest.json').read_bytes()).hexdigest())
    return manifest
