from preprocessing.formats.pdf_tables import (
    assess_geometry,
    candidate_from_rows,
    word_occurrences,
)


def test_empty_filtered_native_words_are_not_a_baseline_contradiction():
    rows = [['Sample', 'Cu'], ['001', '1.2300'], ['002', '2'], ['003', '3']]
    for words in ([], word_occurrences(1, [{'text': 'footer', 'bbox': [0, 110, 20, 120]}])):
        candidate = candidate_from_rows('ocr', 1, [0, 0, 100, 100], rows,
                                        'model-transcription', words=words)
        assessment = assess_geometry(candidate)
        assert assessment['baselineCheck'] == {'state': 'not-applicable', 'reason': 'no-native-word-evidence'}
        assert 'incompatible-baseline-row-count' not in assessment['triggers']
        assert candidate['rowCount'] == 4 and candidate['rows'] == rows


def test_nonempty_conflicting_native_words_still_run_the_baseline_check():
    words = word_occurrences(1, [{'text': 'conflict', 'bbox': [10, 10, 20, 20]}])
    candidate = candidate_from_rows('ocr', 1, [0, 0, 100, 100], [['header'], ['001']],
                                    'model-transcription', words=words)
    assessment = assess_geometry(candidate)
    assert assessment['baselineCheck']['state'] == 'applicable'
    assert 'incompatible-baseline-row-count' in assessment['triggers']
