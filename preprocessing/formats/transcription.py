"""Complete stage envelopes; invalid content is evidence, never transcription."""

import json
import math

from preprocessing.errors import ServiceError


def _invalid():
    return ServiceError('INVALID_EXTRACTION_RESPONSE', 'The transcription response does not satisfy its complete stage envelope.', retry='new-job')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON member')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Non-finite JSON constant')


def envelope(raw, *, page=False, limits=None, remaining_rows=None):
    """Figure: {text}; page: {text,tables:[{rows,bounds?}]}.

    Row matrices have implicit one-based indices and unit spans. Unsupported
    fields, including explicit spans, are rejected rather than silently ignored.
    Missing bounds remain missing evidence; supplied bounds must be valid.
    Validate every table before a caller publishes any candidate or text.
    """
    try:
        data = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, TypeError, RecursionError):
        raise _invalid() from None
    if not isinstance(data, dict) or set(data) != ({'text', 'tables'} if page else {'text'}) or not isinstance(data['text'], str):
        raise _invalid()
    if not page:
        return data
    if not isinstance(data['tables'], list):
        raise _invalid()
    rows_used = grid_used = 0
    for table in data['tables']:
        if not isinstance(table, dict) or 'rows' not in table or set(table) - {'rows', 'bounds'}:
            raise _invalid()
        rows = table['rows']
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise _invalid()
        if any(cell is not None and not isinstance(cell, str) for row in rows for cell in row):
            raise _invalid()
        nr, nc = len(rows), max(map(len, rows), default=0)
        if nr == 0 or nc == 0:
            raise _invalid()
        if 'bounds' in table:
            box = table['bounds']
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in box)
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise _invalid()
        rows_used += nr
        grid_used += nr * nc
        if (rows_used > (limits['rows'] if remaining_rows is None else remaining_rows)
                or nc > limits['columns'] or grid_used > limits['workerMemoryBytes'] // (4 * 1024)):
            raise ServiceError('RESOURCE_LIMIT', 'Page OCR tables exceed the configured row, column or grid allocation limit.')
    return data
