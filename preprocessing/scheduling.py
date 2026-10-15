"""Strict private control contract for the queued-input-v1 checkpoint."""
from datetime import datetime
from uuid import UUID

from preprocessing.errors import ServiceError

CHECKPOINT_CONTRACT = 'queued-input-v1'


def control(body, action):
    fields = {'requestId', 'expectedRevision', 'activationRevision', 'checkpointId'}
    if action == 'park':
        fields.add('block')
    if not isinstance(body, dict) or set(body) != fields:
        raise ServiceError('INVALID_REQUEST', 'Scheduling fields are missing or unknown.')
    try:
        if str(UUID(body['requestId'])) != body['requestId'].lower():
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ServiceError('INVALID_REQUEST', 'requestId must be a UUID.') from None
    body = {**body, 'requestId': body['requestId'].lower()}
    for field in ('expectedRevision', 'activationRevision'):
        if type(body[field]) is not int or body[field] < 0:
            raise ServiceError('INVALID_REQUEST', 'Scheduling revisions must be nonnegative integers.')
    _text(body['checkpointId'])
    if action == 'park':
        block = body['block']
        fields = {'reason', 'episodeId', 'phase', 'failedAttempts', 'automaticRetriesRemaining',
                  'nextEligibleAt', 'unblock', 'checkpointId'}
        if not isinstance(block, dict) or set(block) != fields:
            raise ServiceError('INVALID_REQUEST', 'Block fields are missing or unknown.')
        for field in ('reason', 'episodeId', 'phase', 'checkpointId'):
            _text(block[field])
        if block['checkpointId'] != body['checkpointId'] or block['unblock'] not in ('due', 'dependency', 'user'):
            raise ServiceError('INVALID_REQUEST', 'Block checkpoint or unblock event does not match.')
        for field in ('failedAttempts', 'automaticRetriesRemaining'):
            if type(block[field]) is not int or block[field] < 0:
                raise ServiceError('INVALID_REQUEST', 'Block counters must be nonnegative integers.')
        if block['nextEligibleAt'] is not None:
            try:
                due = datetime.fromisoformat(block['nextEligibleAt'])
                if due.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError):
                raise ServiceError('INVALID_REQUEST', 'nextEligibleAt must be an offset timestamp or null.') from None
        body['block'] = dict(block)
    return body


def _text(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256 or any(ord(char) < 32 for char in value):
        raise ServiceError('INVALID_REQUEST', 'Scheduling identifiers and reasons must be bounded text.')
