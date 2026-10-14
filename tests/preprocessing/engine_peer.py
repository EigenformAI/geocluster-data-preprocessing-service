"""Pinned v2 HTTP peer with deterministic transport and persistence faults."""
import asyncio
import copy
import json

import httpx

from preprocessing.contracts import default_profile as _default_profile
from preprocessing.engine import SYSTEM

DENY = [{'action': '*', 'resource': '*', 'effect': 'deny'}]
MODEL = {'providerID': 'opencode', 'id': 'space-bunny-free'}


def default_profile():
    profile = _default_profile()
    profile['models'] = {'vision': 'opencode/space-bunny-free'}
    return profile


class Peer:
    def __init__(self, directory, fault=''):
        self.directory, self.fault = str(directory), fault
        self.version = '2.0.19'
        self.calls, self.patches, self.sessions = [], [], {}
        self.prompted = asyncio.Event()
        self.records = {}

    async def persist(self, oid, jid, patch):
        self.patches.append(copy.deepcopy(patch))
        self.records.setdefault(oid, {'id': oid, 'jobId': jid}).update(copy.deepcopy(patch))
        if self.fault == 'evidence-persistence' and 'evidence' in patch:
            raise OSError('projection unavailable')

    def transcript(self, sid):
        session = self.sessions[sid]
        if 'input' not in session:
            return []
        body = session['input']
        user = {'id': body['id'], 'type': 'user', 'text': body['text'],
                'metadata': body['metadata']}
        if self.fault in ('hang', 'idle-only', 'unsettled') and not session.get('interrupted'):
            return [user] + ([{'type': 'idle', 'outcome': 'succeeded'}] if self.fault == 'idle-only' else [])
        return [user, {'id': 'msg_answer', 'type': 'assistant', 'agent': 'preprocessing',
                       'model': MODEL, 'content': [{'type': 'text', 'text': 'transcribed 42'}],
                       'finish': 'stop', 'tokens': {'input': 17, 'output': 4},
                       'time': {'completed': 123}},
                {'id': 'msg_idle', 'type': 'idle',
                 'outcome': 'interrupted' if session.get('interrupted') else 'succeeded'}]

    async def handle(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body, dict(request.url.params)))
        assert request.headers['authorization'].startswith('Basic ')
        if path == '/api/info':
            return httpx.Response(200, json={'version': self.version})
        if path == '/api/agent':
            permissions = [{'action': '*', 'resource': '*', 'effect': 'allow'}, *DENY]
            if self.fault == 'permission-override':
                permissions.append({'action': 'read', 'resource': '*', 'effect': 'allow'})
            return httpx.Response(200, json={'data': [{'id': 'preprocessing', 'mode': 'primary',
                'system': SYSTEM, 'steps': 1, 'permissions': permissions}]})
        if path == '/api/model':
            return httpx.Response(200, json={'data': [{**MODEL, 'enabled': True, 'variants': [],
                'capabilities': {'input': ['text'] if self.fault == 'no-image' else ['text', 'image'],
                                 'output': ['text']}}]})
        if path == '/api/session' and method == 'POST':
            assert self.patches[-1]['state'] == 'prepared'
            self.sessions[body['id']] = copy.deepcopy(body)
            self.sessions[body['id']]['model'].setdefault('variant', 'default')
            if self.fault == 'selection':
                self.sessions[body['id']]['model'] = {'providerID': 'wrong', 'id': 'wrong'}
            if self.fault in ('create-lost', 'create-absent'):
                if self.fault == 'create-absent':
                    self.sessions.pop(body['id'])
                raise httpx.ReadTimeout('lost create acknowledgement')
            return httpx.Response(200, json={'data': self.sessions[body['id']]})
        if path == '/api/session':
            assert request.url.params.get('parentID')
            children = [{'id': 'ses_unowned'}] if self.fault == 'child' else []
            return httpx.Response(200, json={'data': children, 'cursor': {}})
        sid = path.split('/')[4 if path.startswith('/api/experimental/') else 3]
        if sid not in self.sessions:
            return httpx.Response(404)
        session = self.sessions[sid]
        if path.endswith('/prompt'):
            assert self.patches[-1]['state'] == 'dispatch_intent'
            assert set(body) == {'id', 'text', 'files', 'metadata'}
            session['input'] = body
            self.prompted.set()
            if self.fault == 'prompt-lost':
                raise httpx.ReadTimeout('lost prompt acknowledgement')
            return httpx.Response(200, json={'data': {'id': body['id'], 'sessionID': sid, 'type': 'user'}})
        if path.endswith('/message'):
            return httpx.Response(200, json={'data': self.transcript(sid), 'cursor': {}})
        if '/inbox/' in path:
            return httpx.Response(204)
        if path.endswith('/inbox'):
            return httpx.Response(200, json={'data': []})
        if path.endswith('/interrupt'):
            assert 'resume' not in request.url.params
            if self.fault == 'unsettled':
                raise httpx.ReadTimeout('lost interrupt acknowledgement')
            interrupted = self.fault in ('hang', 'idle-only')
            session['interrupted'] = interrupted
            return httpx.Response(200, json={'interrupted': interrupted})
        if path.endswith('/wait'):
            return httpx.Response(204)
        if method == 'DELETE':
            if 'input' in session and self.fault not in ('hang', 'idle-only'):
                assert any('evidence' in patch for patch in self.patches)
            self.sessions.pop(sid)
            return httpx.Response(204)
        return httpx.Response(200, json={'data': session})
