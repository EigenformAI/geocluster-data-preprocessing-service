"""Seal validated products; all navigation targets must be listed products."""
import hashlib
import json
import os
import posixpath
import re
import uuid
from collections import deque
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


def _evidence_json(handle, path):
    def nonfinite(value):
        invalid(f'Non-finite JSON value in evidence product {path}.')

    try:
        return json.loads(handle.read(), parse_constant=nonfinite)
    except (ValueError, UnicodeError):
        invalid(f'Invalid JSON in evidence product {path}.')


def _candidate_records(root, path):
    with open_regular(root, path) as handle:
        for line in handle:
            try:
                record = json.loads(line, parse_constant=lambda value: invalid(f'Non-finite candidate value in {path}.'))
            except (ValueError, UnicodeError):
                invalid(f'Invalid candidate JSONL in {path}.')
            if not isinstance(record, dict):
                invalid('Candidate records must be JSON objects.')
            yield record


def validate_table_evidence(root, manifest):
    try:
        _validate_table_evidence(root, manifest)
    except (TypeError, KeyError, AttributeError, RecursionError):
        invalid('Malformed table evidence or reference container.')


def _validate_table_evidence(root, manifest):
    """Check the evidence graph, without imposing a geometry/decision schema.

    Candidate/word identities are page-local. Optional references use tableId,
    *CandidateId(s), wordId(s), *Path(s), or evidencePaths. Paths are generation-
    relative. selectedRows may be raw row arrays or records with one-based index,
    candidateId and sourceRows; wordAssignments uses wordId(s) references.
    """
    products = {item['path']: item for item in manifest['products']}
    tables = {}
    for table in manifest.get('tables', []):
        identity = table.get('id')
        if not isinstance(identity, str) or not identity or identity in tables:
            invalid('Duplicate or invalid table identity.')
        tables[identity] = table
    page_total = manifest.get('coverage', {}).get('total') if manifest.get('coverage', {}).get('unit') == 'page' else None

    def page_number(value):
        if type(value) is not int or value < 1 or (type(page_total) is int and value > page_total):
            invalid('Evidence source page is invalid.')
        return value

    def product_ref(path, kind=None):
        if not isinstance(path, str) or path not in products:
            invalid('Table evidence links to an unlisted product.')
        relative_parts(path)
        if kind and products[path]['kind'] != kind:
            invalid('Table evidence does not reference the correct product kind.')
        return path

    def paths(value):
        pending = [value]
        while pending:
            entry = pending.pop()
            if isinstance(entry, dict):
                pending.extend(entry.values())
            elif isinstance(entry, list):
                pending.extend(entry)
            elif entry is not None:
                product_ref(entry)

    candidates, words, documents = {}, {}, {}
    for path, item in products.items():
        kind = item['kind']
        if kind == 'table-candidates':
            for record in _candidate_records(root, path):
                identity, page = record.get('id'), page_number(record.get('page'))
                product_page = item.get('source', {}).get('page')
                if product_page is not None and page_number(product_page) != page:
                    invalid('Candidate and product source pages disagree.')
                if not isinstance(identity, str) or not identity or (page, identity) in candidates:
                    invalid('Duplicate or invalid page-local candidate identity.')
                if 'bounds' not in record or not isinstance(record.get('rows'), list) or any(not isinstance(row, list) for row in record['rows']):
                    invalid('Candidate bounds or original rows are missing or invalid.')
                if not isinstance(record.get('method'), str) or not record['method']:
                    invalid('Candidate extraction method is missing.')
                candidates[page, identity] = (path, len(record['rows']))
        elif kind in {'table-structure', 'table-recovery-audit', 'work-plan', 'page-evidence'}:
            with open_regular(root, path) as handle:
                document = _evidence_json(handle, path)
            if not isinstance(document, dict):
                invalid('Evidence JSON must be an object.')
            if kind == 'page-evidence':
                # Legacy page evidence has no word IDs. IDs become mandatory only
                # for occurrences that other evidence actually references.
                page = document.get('source', item.get('source', {})).get('page')
                if page is not None:
                    page = page_number(page)
                    product_page = item.get('source', {}).get('page')
                    if product_page is not None and page_number(product_page) != page:
                        invalid('Word evidence and product source pages disagree.')
                    for word in document.get('words', []):
                        if 'id' in word:
                            identity = word['id']
                            if not isinstance(identity, str) or not identity or identity in words.setdefault(page, set()):
                                invalid('Duplicate or invalid page-local word identity.')
                            words[page].add(identity)
            else:
                documents[path] = document

    def candidate_ref(identity, page):
        if not isinstance(identity, str) or (page, identity) not in candidates:
            invalid('Evidence references an unknown candidate on its source page.')
        return candidates[page, identity]

    def references(value, page=None, candidate=None):
        pending = [(value, page, candidate)]
        while pending:
            entry, current_page, current_candidate = pending.pop()
            if isinstance(entry, list):
                pending.extend((child, current_page, current_candidate) for child in entry)
                continue
            if not isinstance(entry, dict):
                continue
            table_id = entry.get('tableId')
            if table_id is not None:
                if not isinstance(table_id, str) or table_id not in tables:
                    invalid('Evidence references an unknown table.')
                table_page = tables[table_id].get('source', {}).get('page')
                if current_page is not None and table_page != current_page:
                    invalid('Evidence table belongs to a different source page.')
                current_page = table_page
            explicit_page = entry.get('page', entry.get('source', {}).get('page'))
            if explicit_page is not None:
                explicit_page = page_number(explicit_page)
                if current_page is not None and current_page != explicit_page:
                    invalid('Evidence source pages disagree.')
                current_page = explicit_page
            current_candidate = entry.get('candidateId', current_candidate)
            for key, child in entry.items():
                if key.endswith('CandidateId') or key == 'candidateId':
                    if child is not None:
                        candidate_ref(child, current_page)
                elif key.endswith('CandidateIds') or key == 'candidateIds':
                    if not isinstance(child, list):
                        invalid('Candidate references must be a list.')
                    for identity in child:
                        candidate_ref(identity, current_page)
                elif key in {'wordId', 'wordIds'}:
                    identities = child if key == 'wordIds' else [child]
                    if not isinstance(identities, list) or any(not isinstance(identity, str) or identity not in words.get(current_page, set()) for identity in identities):
                        invalid('Evidence references an unknown word on its source page.')
                elif key.endswith('Path'):
                    if child is not None:
                        product_ref(child)
                elif key.endswith('Paths'):
                    paths(child)
                elif key == 'sourceRows':
                    if not isinstance(child, list):
                        invalid('Source row references must be a list.')
                    _, count = candidate_ref(current_candidate, current_page)
                    if any(type(row) is not int or not 1 <= row <= count for row in child):
                        invalid('Evidence source row reference is invalid.')
                # Original rows and raw cell values are data, not reference fields.
                elif key not in {'rows', 'text', 'value', 'bounds'}:
                    pending.append((child, current_page, current_candidate))

    for path, item in products.items():
        if item['kind'].startswith('table-') or item['kind'] == 'work-plan':
            references(item)
        if item['kind'] == 'table-candidates':
            for record in _candidate_records(root, path):
                references(record, record['page'], record['id'])

    selected_occurrences = set()
    for table in tables.values():
        fields = {'selectedCandidateId', 'recovery', 'confirmation', 'evidencePaths'}
        if not fields.intersection(table):
            continue
        if not fields <= table.keys():
            invalid('Table resolution summary is incomplete.')
        page = page_number(table.get('source', {}).get('page'))
        selected_path, selected_count = candidate_ref(table['selectedCandidateId'], page)
        if selected_count != table['rows']:
            invalid('Canonical row count differs from the selected candidate.')
        occurrence = (page, table['selectedCandidateId'])
        if occurrence in selected_occurrences:
            invalid('Multiple canonical tables select the same candidate occurrence.')
        selected_occurrences.add(occurrence)
        recovery, confirmation = table['recovery'], table['confirmation']
        if not isinstance(recovery, dict) or recovery.get('state') not in {'unchanged', 'recovered', 'unresolved'} or not isinstance(recovery.get('reason'), str):
            invalid('Table recovery summary is invalid.')
        if not isinstance(confirmation, dict) or confirmation.get('state') not in {'agreed', 'unresolved', 'failed', 'pending', 'not_checked'} or 'basis' not in confirmation:
            invalid('Table confirmation summary is invalid.')
        basis = confirmation['basis']
        if basis is not None and not isinstance(basis, str):
            invalid('Table confirmation basis is invalid.')
        if confirmation['state'] == 'agreed' and basis not in {'native-vision', 'ocr-vision', 'repeat-vision'}:
            invalid('Agreed table lacks a supported confirmation basis.')
        if 'reason' in confirmation and not isinstance(confirmation['reason'], str):
            invalid('Table confirmation reason is invalid.')
        evidence = table['evidencePaths']
        if not isinstance(evidence, dict) or not {'candidates', 'structure', 'decisions'} <= evidence.keys():
            invalid('Table evidence paths are incomplete.')
        for key, kind in [('candidates', 'table-candidates'), ('structure', 'table-structure'), ('decisions', 'table-recovery-audit')]:
            product_ref(evidence[key], kind)
        if selected_path != evidence['candidates']:
            invalid('Selected candidate is absent from the table candidate product.')
        references(table, page, table['selectedCandidateId'])
        with open_regular(root, product_ref(table['typedPath'], 'typed-table')) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    invalid('Typed table JSONL is invalid.')
                references(row, page, table['selectedCandidateId'])

    for path, document in documents.items():
        kind = products[path]['kind']
        if kind in {'table-structure', 'table-recovery-audit'}:
            table_id = document.get('tableId')
            if not isinstance(table_id, str) or table_id not in tables:
                invalid('Evidence references an unknown table.')
            table = tables[table_id]
            if document.get('selectedCandidateId') != table.get('selectedCandidateId') or not document.get('selectedCandidateId'):
                invalid('Evidence selection differs from the canonical table.')
            key = 'structure' if kind == 'table-structure' else 'decisions'
            if table.get('evidencePaths', {}).get(key) != path:
                invalid('Evidence product does not belong to its canonical table.')
            page = page_number(table.get('source', {}).get('page'))
            if kind == 'table-structure':
                candidate_list = document.get('candidates', [])
                if not isinstance(candidate_list, list):
                    invalid('Structure candidate references must be a list.')
                for entry in candidate_list:
                    candidate_ref(entry.get('id') if isinstance(entry, dict) else entry, page)
            if 'selectedRows' in document:
                rows = document['selectedRows']
                if not isinstance(rows, list) or len(rows) != table['rows']:
                    invalid('Selected row map differs from the canonical table.')
                for index, row in enumerate(rows, 1):
                    if not isinstance(row, (list, dict)):
                        invalid('Selected row map entries must be rows or mapping records.')
                    if isinstance(row, dict) and 'index' in row and (type(row['index']) is not int or row['index'] != index):
                        invalid('Selected row index is invalid.')
            references(document, page, table['selectedCandidateId'])
        else:
            if type(document.get('version')) is not int or document['version'] < 1 or not isinstance(document.get('fingerprint'), (str, dict)) or not document['fingerprint'] or not isinstance(document.get('units'), list):
                invalid('Work plan version, fingerprint or units are invalid.')
            units = {}
            for unit in document['units']:
                if not isinstance(unit, dict) or not isinstance(unit.get('id'), str) or not unit['id'] or unit['id'] in units:
                    invalid('Work plan unit identity is invalid or duplicated.')
                if any(not isinstance(unit.get(key), str) or not unit[key] for key in ('kind', 'state')) or not isinstance(unit.get('dependencies'), list) or 'evidencePaths' not in unit:
                    invalid('Work plan unit is incomplete.')
                page_number(unit.get('source', {}).get('page'))
                units[unit['id']] = unit
            dependents = {identity: [] for identity in units}
            remaining = {}
            for identity, unit in units.items():
                dependencies = unit['dependencies']
                if any(not isinstance(dep, str) or dep not in units for dep in dependencies) or len(set(dependencies)) != len(dependencies):
                    invalid('Work plan dependency is unknown or duplicated.')
                if unit['state'] == 'completed' and any(units[dep]['state'] != 'completed' for dep in dependencies):
                    invalid('Completed work plan unit has an incomplete dependency.')
                remaining[identity] = len(dependencies)
                for dependency in dependencies:
                    dependents[dependency].append(identity)
            ready = deque(identity for identity, count in remaining.items() if count == 0)
            completed = 0
            while ready:
                completed += 1
                for identity in dependents[ready.popleft()]:
                    remaining[identity] -= 1
                    if remaining[identity] == 0:
                        ready.append(identity)
            if completed != len(units):
                invalid('Work plan dependencies contain a cycle.')
            references(document)


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
    usable = any(item['kind'] in {'typed-table', 'page-text', 'page-ocr', 'figure-image', 'table-image'} and item['bytes'] for item in products)
    if not usable:
        invalid('No independently usable extraction products were produced.')
    manifest = {'schemaVersion': 1, 'jobId': job['id'], 'resultId': 'result_' + uuid.uuid4().hex,
                'outcome': 'succeeded' if success else 'partial', 'source': job['source'],
                'pipeline': job['profile'], **extraction}
    validate_generation(output, manifest)
    atomic_json(output / 'manifest.json', manifest)
    for directory in sorted((path for path in output.rglob('*') if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)
    fsync_directory(output)
    return manifest


def validate_generation(root, manifest):
    paths = [item['path'] for item in manifest['products']]
    if len(set(paths)) != len(paths) or 'manifest.json' in paths:
        invalid('Duplicate or reserved product path.')
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
    validate_table_evidence(root, manifest)
