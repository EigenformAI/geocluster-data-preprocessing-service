import json
import shutil

import pytest
from test_formats import Model, extract, make_pdf

from preprocessing.contracts import default_profile


@pytest.mark.parametrize('raw', [
    'Maximum steps for this agent have been reached. {"text":"copied","tables":[]}',
    '```json\n{"text":"copied","tables":[]}\n```',
    '{"text":"copied","tables":[],"commentary":"not source"}',
    '{"text":"first","text":"second","tables":[]}',
    '{"text":"copied","tables":[{"rows":[[123]]}]}',
    '{"text":"copied","tables":[{"bounds":[0,0,1001,1000],"rows":[["a"]]}]}',
    '{"text":"copied","tables":[{"rows":[["a"]],"rowSpan":0}]}',
    '{"text":"copied","tables":[{"rows":[["a"]]},{"rows":"invalid"}]}',
])
def test_invalid_complete_page_envelope_is_only_raw_evidence(tmp_path, raw):
    source = tmp_path / 'scan.pdf'
    make_pdf(source, scanned=True)

    class Response(Model):
        def model(self, operation, refs):
            response = super().model(operation, refs)
            if operation['kind'] == 'page-ocr':
                response['text'] = raw
            return response

    result, output = extract(source, tmp_path, Response())
    assert not any(p['kind'] == 'page-ocr' for p in result['products'])
    retained = next(p for p in result['products'] if p['kind'] == 'page-ocr-response')
    assert (output / retained['path']).read_text() == raw
    assert any(f['code'] == 'INVALID_EXTRACTION_RESPONSE' for f in result['findings'])
    assert not result['tables']
    page = json.loads((output / 'text/pages.jsonl').read_text())
    assert page['ocrStatus'] == 'failed' and page['ocrPath'] is None


@pytest.mark.parametrize('text', ['', 'Maximum steps for this agent have been reached.'])
def test_valid_page_and_figure_envelopes_accept_empty_or_literal_source_text(tmp_path, text):
    source = tmp_path / 'scan.pdf'
    make_pdf(source, scanned=True)

    class Response(Model):
        def model(self, operation, refs):
            response = super().model(operation, refs)
            if operation['kind'] == 'page-ocr':
                response['text'] = json.dumps({'text': text, 'tables': []})
            elif operation['kind'] == 'figure-ocr':
                response['text'] = json.dumps({'text': text})
            return response

    result, output = extract(source, tmp_path, Response())
    assert result['coverage']['completed'] == 1
    for kind in ('page-ocr', 'figure-ocr'):
        accepted = next(p for p in result['products'] if p['kind'] == kind)
        assert (output / accepted['path']).read_text() == text
        raw = next(p for p in result['products'] if p['kind'] == kind + '-response')
        assert json.loads((output / raw['path']).read_text())['text'] == text


@pytest.mark.parametrize('raw', ['operational commentary', '{"text":42}', '{"text":"a","extra":true}'])
def test_invalid_figure_transcription_never_becomes_a_transcription_product(tmp_path, raw):
    source = tmp_path / 'native.pdf'
    make_pdf(source)

    class Response(Model):
        def model(self, operation, refs):
            response = super().model(operation, refs)
            if operation['kind'] == 'figure-ocr':
                response['text'] = raw
            return response

    result, output = extract(source, tmp_path, Response())
    assert not any(p['kind'] == 'figure-ocr' for p in result['products'])
    retained = next(p for p in result['products'] if p['kind'] == 'figure-ocr-response')
    assert (output / retained['path']).read_text() == raw
    assert result['figures'][0]['ocrPath'] is None
    assert result['figures'][0]['ocrStatus'] == 'failed'
    assert result['figures'][0]['interpretationStatus'] == 'succeeded'
    assert any(f['code'] == 'INVALID_EXTRACTION_RESPONSE' for f in result['findings'])


def test_ocr_envelope_limits_precede_candidate_allocation(tmp_path):
    from preprocessing.errors import ServiceError

    source = tmp_path / 'scan.pdf'
    make_pdf(source, scanned=True)

    class Response(Model):
        def model(self, operation, refs):
            return {'text': json.dumps({'text': '', 'tables': [{'rows': [['a', 'b', 'c']]}]})}

    profile = default_profile()
    profile['limits']['columns'] = 2
    with pytest.raises(ServiceError) as error:
        extract(source, tmp_path, Response(), profile)
    assert error.value.code == 'RESOURCE_LIMIT'


@pytest.mark.parametrize('difference', [None, 'header-omitted', 'numeric-conflict', 'missing-cell'])
def test_four_source_rows_need_a_separate_complete_agreeing_visual_read(tmp_path, difference):
    source = tmp_path / 'scan.pdf'
    make_pdf(source, scanned=True)
    # Distinctive source lexemes avoid mistaking an image dimension such as
    # 1250 pixels for disclosure of the source value "125".
    rows = [['Sample_ID', 'Cu_ppm', 'Au_ppm'], ['CC-101', '125.0000', '0.12'],
            ['CC-102', '250.0000', '0.24'], ['CC-103', '375.0000', '0.36']]

    class Responses(Model):
        def model(self, operation, refs):
            response = super().model(operation, refs)
            if operation['kind'] == 'page-ocr':
                response['text'] = json.dumps({'text': 'source text', 'tables': [
                    {'bounds': [0, 0, 1000, 1000], 'rows': rows}]})
            elif operation['kind'] == 'table-confirmation':
                # The second read receives only the image and independent prompt,
                # never the first response's values.
                assert all(value not in operation['prompt'] for row in rows for value in row)
                visual_rows = [list(row) for row in (rows[1:] if difference == 'header-omitted' else rows)]
                if difference == 'numeric-conflict':
                    visual_rows[1][1] = '999'
                cells = [{'row': r, 'column': c, 'text': value}
                         for r, row in enumerate(visual_rows, 1) for c, value in enumerate(row, 1)]
                if difference == 'missing-cell':
                    cells.pop()
                response['text'] = json.dumps({'tablePresent': True, 'rowCount': len(visual_rows),
                                               'columnCount': 3, 'cells': cells})
            return response

    profile = default_profile()
    profile['options']['tableRecovery']['maxAdditionalPasses'] = 0
    model = Responses()
    result, output = extract(source, tmp_path, model, profile)
    assert [call[0]['kind'] for call in model.calls].count('page-ocr') == 1
    assert [call[0]['kind'] for call in model.calls].count('table-confirmation') == 1
    table = result['tables'][0]
    assert table['header'] == 'not-inferred' and table['rows'] == 4
    assert table['recovery']['baselineCheck'] == {'state': 'not-applicable', 'reason': 'no-native-word-evidence'}
    assert 'incompatible-baseline-row-count' not in table['recovery']['reason']
    assert table['confirmation']['state'] == ('agreed' if difference is None else 'unresolved')
    if difference is None:
        assert table['confirmation']['basis'] == 'ocr-vision'
    decisions = json.loads((output / table['evidencePaths']['decisions']).read_text())
    assert decisions['comparisons']
    assert all(c['leftCandidateId'] != c['rightCandidateId'] for c in decisions['comparisons'])
    assert all(c['agreed'] == (difference is None) for c in decisions['comparisons'])


@pytest.mark.parametrize('passes,wrong_reread,agreed', [(0, False, False), (1, False, True), (1, True, False)])
def test_boundary_dispute_requires_a_fresh_geometrically_agreeing_read(tmp_path, passes, wrong_reread, agreed):
    source = tmp_path / 'scan.pdf'
    make_pdf(source, scanned=True)
    rows = [['Sample_ID', 'Cu_ppm', 'Au_ppm'], ['CC-101', '125.0000', '0.12'],
            ['CC-102', '250.0000', '0.24'], ['CC-103', '375.0000', '0.36']]

    class Responses(Model):
        def model(self, operation, refs):
            response = super().model(operation, refs)
            kind = operation['kind']
            if kind == 'page-ocr':
                response['text'] = json.dumps({'text': 'source text', 'tables': [
                    {'bounds': [50, 244, 951, 823], 'rows': rows}]})
            elif kind in {'table-confirmation', 'table-reread'}:
                assert all(value not in operation['prompt'] for row in rows for value in row)
                visual = {'tablePresent': True, 'rowCount': 4, 'columnCount': 3,
                    'cells': [{'row': r, 'column': c, 'text': value}
                              for r, row in enumerate(rows, 1) for c, value in enumerate(row, 1)]}
                if kind == 'table-confirmation':
                    # Captured production-shaped response: text is correct, but
                    # vertical edges resemble pixels rather than normalized y.
                    visual.update(rowEdges=[11, 250, 488, 727, 965], columnEdges=[5, 372, 683, 994])
                else:
                    x0, y0, x1, y1 = operation['sourceUnit']['bounds']
                    target = [15, 97.6, 285.3, 329.2 - (20 if wrong_reread else 0)]
                    visual['rowEdges'] = [1000 * (target[1] + i * (target[3] - target[1]) / 4 - y0) / (y1 - y0)
                                          for i in range(5)]
                    visual['columnEdges'] = [1000 * (target[0] + i * (target[2] - target[0]) / 3 - x0) / (x1 - x0)
                                             for i in range(4)]
                response['text'] = json.dumps(visual)
            return response

    profile = default_profile()
    profile['options']['figureDescriptions'] = False
    profile['options']['tableRecovery'].update(maxAdditionalPasses=passes, maxModelCalls=2)
    model = Responses()
    result, output = extract(source, tmp_path, model, profile)
    table = result['tables'][0]
    assert table['rows'] == 4 and table['header'] == 'not-inferred'
    assert table['confirmation']['state'] == ('agreed' if agreed else 'unresolved')
    if agreed:
        assert table['confirmation']['basis'] == 'ocr-vision'
    calls = [call[0]['kind'] for call in model.calls]
    assert calls.count('table-confirmation') == 1 and calls.count('table-reread') == passes
    decisions = json.loads((output / table['evidencePaths']['decisions']).read_text())
    initial = decisions['comparisons'][0]
    assert initial['agreed'] is False
    assert [d['kind'] for d in initial['differences']] == ['table-boundaries']
    assert any(c['agreed'] for c in decisions['comparisons']) == agreed


def test_forced_resume_rejection_cannot_republish_previous_transcription(tmp_path):
    from preprocessing.formats.pdf import PDFAdapter

    source = tmp_path / 'scan.pdf'
    make_pdf(source, scanned=True)
    first, first_output = extract(source, tmp_path)
    plan = json.loads((first_output / 'work-plan.json').read_text())
    output = tmp_path / 'retry'
    shutil.copytree(first_output, output)

    class Invalid(Model):
        def model(self, operation, refs):
            response = super().model(operation, refs)
            if operation['kind'] in {'page-ocr', 'figure-ocr'}:
                response['text'] = 'invalid completed response'
            return response

    context = Invalid()
    context.resume = {'previousExtraction': first, 'products': first['products'], 'workPlan': plan,
                      'completedUnits': [], 'reusedFromJob': 'prior', 'forcePages': [1],
                      'invalidatedUnitIds': [unit['id'] for unit in plan['units']]}
    result = PDFAdapter().extract({'path': str(source)}, default_profile(), output, context)
    assert not any(p['kind'] in {'page-ocr', 'figure-ocr'} for p in result['products'])
    assert json.loads((output / 'text/pages.jsonl').read_text())['ocrPath'] is None
    assert result['figures'][0]['ocrPath'] is None
    # The parent's retained evidence remains intact.
    assert (first_output / 'text/page_0001.ocr.txt').read_text() == 'visible 001'
