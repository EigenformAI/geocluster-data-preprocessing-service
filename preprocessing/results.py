"""Seal validated products; all navigation targets must be listed products."""
import hashlib
import json
import os
import posixpath
import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import unquote

from preprocessing.errors import ServiceError
from preprocessing.files import (
    atomic_json,
    fsync_directory,
    open_regular,
    relative_parts,
)


def invalid(message):
    raise ServiceError('OUTPUT_INVALID', message, 500, 'operator-action')


def make_manifest(job, extraction, output):
    required = {'coverage', 'stages', 'products', 'tables', 'figures', 'findings', 'provenance'}
    if not required <= extraction.keys():
        invalid('Worker result lacks required fields.')
    products = extraction['products']
    paths = set()
    total = 0
    for item in products:
        path = item['path']
        if path in paths or path == 'manifest.json':
            invalid('Duplicate or reserved product path.')
        paths.add(path)
        with open_regular(output, path) as handle:
            item['bytes'] = os.fstat(handle.fileno()).st_size
            item['sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
            os.fsync(handle.fileno())
        total += item['bytes']
        item['id'] = 'product_' + uuid.uuid4().hex
        item['schemaVersion'] = 1
        item['contentUrl'] = f"/preprocessing/jobs/{job['id']}/products/{item['id']}"
    if total > job['profile']['limits']['outputBytes']:
        raise ServiceError('RESOURCE_LIMIT', 'Products exceed the output-byte limit.')
    if not {'AGENTS.md', 'index.md'} <= paths:
        invalid('Navigation products are missing.')
    by_path = {item['path']: item for item in products}
    for table in extraction['tables']:
        table['rowsUrl'] = f"/preprocessing/jobs/{job['id']}/tables/{table['id']}/rows"
        table['downloads'] = []
        for fmt, key, kind in [('typed-jsonl', 'typedPath', 'typed-table'), ('csv', 'csvPath', 'table-export')]:
            item = by_path.get(table[key])
            if not item or item['kind'] != kind:
                invalid('Table download does not reference the correct product kind.')
            table['downloads'].append({'format': fmt, 'productId': item['id']})
        count = 0
        with open_regular(output, table['typedPath']) as handle:
            for line in handle:
                row = json.loads(line)
                count += 1
                if row.get('index') != count or not isinstance(row.get('cells'), list):
                    invalid('Typed table rows are invalid.')
                if any(cell.get('type') not in {'absent', 'empty', 'text', 'null', 'number', 'boolean', 'date', 'formula', 'error'} for cell in row['cells']):
                    invalid('Typed cell kind is invalid.')
        if count != table['rows']:
            invalid('Table row count differs from inventory.')
    coverage = extraction['coverage']
    if coverage.get('enumeration') == 'complete':
        counts = [coverage.get(key) for key in ('completed', 'failed', 'unsupported', 'notProcessed')]
        if any(type(count) is not int or count < 0 for count in counts) or sum(counts) != coverage.get('total'):
            invalid('Coverage counts are inconsistent.')
    else:
        invalid('Initial adapters must enumerate all source units.')
    if not extraction['stages'] or any(stage.get('state') not in {'completed', 'partial', 'failed', 'not_applicable', 'not_run'} for stage in extraction['stages']):
        invalid('Stage outcomes are missing or invalid.')
    success = all(not stage.get('required', True) or stage['state'] == 'completed' or
                  (stage['state'] == 'not_applicable' and stage.get('reason')) for stage in extraction['stages'])
    usable = any(item['kind'] in {'typed-table', 'page-text', 'page-ocr', 'figure-image'} and item['bytes'] for item in products)
    if not usable:
        invalid('No independently usable extraction products were produced.')
    manifest = {'schemaVersion': 1, 'jobId': job['id'], 'resultId': 'result_' + uuid.uuid4().hex,
                'outcome': 'succeeded' if success else 'partial', 'source': job['source'],
                'pipeline': job['profile'], **extraction}
    atomic_json(output / 'manifest.json', manifest)
    validate_generation(output, manifest)
    for directory in sorted((path for path in output.rglob('*') if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(output)
    return manifest


def validate_generation(root, manifest):
    listed = {item['path'] for item in manifest['products']} | {'manifest.json'}
    ids = set()
    for item in manifest['products']:
        relative_parts(item['path'])
        if item['id'] in ids:
            invalid('Duplicate product identity.')
        ids.add(item['id'])
        with open_regular(root, item['path']) as handle:
            if os.fstat(handle.fileno()).st_size != item['bytes'] or hashlib.file_digest(handle, 'sha256').hexdigest() != item['sha256']:
                invalid('Product bytes do not match the manifest.')
        if item['kind'] in {'navigation-index', 'table-preview', 'figure-index', 'agent-guide'}:
            with open_regular(root, item['path']) as handle:
                text = handle.read().decode('utf-8')
            for target in re.findall(r'\]\(([^)]+)\)', text):
                target = unquote(target)
                resolved = posixpath.normpath(str(PurePosixPath(item['path']).parent / target))
                if resolved not in listed:
                    invalid('Navigation links to an unlisted product.')
    for figure in manifest['figures']:
        for key in ('imagePath', 'ocrPath', 'interpretationPath'):
            if figure.get(key) and figure[key] not in listed:
                invalid('Figure links to an unlisted product.')
