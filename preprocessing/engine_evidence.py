from __future__ import annotations

from dataclasses import dataclass

from .errors import ServiceError


class EvidenceError(ServiceError):
    """Invalid evidence retains any correlated input already observed in its page."""

    def __init__(self, code, message, *, observed=False, retry='operator-action'):
        super().__init__(code, message, 503, retry)
        self.observed = observed


@dataclass(frozen=True)
class TranscriptInspection:
    outcome: str | None
    complete: bool
    evidence: dict
    observed: bool


def operation_marker(record: dict) -> dict:
    return {key: record[key] for key in ('jobId', 'generation', 'engineInstanceId', 'inputId', 'requestHash')} | {
        'operationId': record['id']}


def deny_all(rules):
    """Pinned permissions are ordered; configured rules follow built-in defaults."""
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        return False
    broad = [index for index, rule in enumerate(rules)
             if rule.get('action') == '*' and rule.get('resource') == '*']
    return bool(broad and all(rule.get('effect') == 'deny' for rule in rules[broad[-1]:]))


def _same_model(actual, expected):
    # The pinned projector materializes an omitted variant as "default".
    return (isinstance(actual, dict) and actual.get('providerID') == expected['providerID']
            and actual.get('id') == expected['id']
            and actual.get('variant', 'default') == expected.get('variant', 'default'))


def validate_session_ownership(session: dict, record: dict) -> None:
    if (not isinstance(session, dict) or session.get('id') != record['sessionId']
            or session.get('parentID') is not None or session.get('location') != record['location']
            or not isinstance(session.get('metadata'), dict)
            or session['metadata'].get('preprocessing') != operation_marker(record)
            or not deny_all(session.get('permissions'))):
        raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'The session ownership or extraction policy does not match.')


def validate_session_selection(session: dict, record: dict) -> None:
    if (not isinstance(session, dict) or session.get('agent') != record['agent']
            or not _same_model(session.get('model'), record['model'])):
        raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'The actual extraction agent or model does not match.')


def inspect_transcript(page: dict, record: dict, *, response_bytes: int) -> TranscriptInspection:
    messages = page.get('data')
    cursor = page.get('cursor', {})
    # 2.0.19 emits cursor.next for every nonempty page; it is an anchor,
    # not a has-more flag. A short page is complete at this read watermark.
    if not isinstance(messages, list) or not isinstance(cursor, dict) or len(messages) >= 200:
        raise EvidenceError('OUTPUT_INVALID', 'The one-step extraction transcript exceeds its bounded page.', retry='new-job')
    user_index = None
    observed = False
    text, assistants = [], []
    terminal = None
    terminal_index, assistant_index = -1, -1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'Malformed extraction transcript.', observed=observed)
        kind = message.get('type')
        if kind == 'user':
            if (user_index is not None or message.get('id') != record['inputId']
                    or not isinstance(message.get('metadata'), dict)
                    or message['metadata'].get('requestHash') != record['requestHash']):
                raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'The transcript contains an unrelated input.', observed=observed)
            user_index = index
            observed = True
        elif kind == 'assistant':
            content = message.get('content')
            if (not isinstance(content, list) or not isinstance(message.get('time'), dict)
                    or any(not isinstance(c, dict) or (c.get('type') == 'text' and not isinstance(c.get('text'), str))
                           for c in content)):
                raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'Malformed extraction assistant evidence.', observed=observed)
            if user_index is None or message.get('agent') != record['agent'] or not _same_model(message.get('model'), record['model']):
                raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'An assistant response is not bound to the extraction input.', observed=observed)
            if any(c.get('type') == 'tool' for c in message.get('content', [])):
                raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'Tool execution is forbidden for extraction.', observed=observed)
            text.extend(c['text'] for c in message.get('content', []) if c.get('type') == 'text')
            assistants.append(message)
            assistant_index = index
        elif kind == 'idle' and user_index is not None:
            terminal = message.get('outcome')
            terminal_index = index
        elif kind not in ('agent-switched', 'model-switched', 'location-switched', 'idle'):
            raise EvidenceError('PROVIDER_ISOLATION_FAILED', 'Unexpected activity in the extraction transcript.', observed=observed)
    combined = ''.join(text)
    if len(combined.encode()) > response_bytes:
        raise EvidenceError('OUTPUT_INVALID', 'The extraction output exceeds its byte limit.', observed=observed, retry='new-job')
    evidence = {'text': combined, 'usage': {'tokens': assistants[-1].get('tokens'),
                'cost': assistants[-1].get('cost')} if assistants else None,
                'outcome': terminal, 'inputId': record['inputId']}
    complete = bool(terminal == 'succeeded' and assistants and combined.strip() and terminal_index > assistant_index
                    and all(a.get('time', {}).get('completed') is not None and a.get('finish') == 'stop'
                            and not a.get('error') for a in assistants))
    return TranscriptInspection(terminal, complete, evidence, observed)
