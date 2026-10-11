"""External standalone supervisor and projection-only cleanup/history helpers.

The service ledger remains authoritative. Owner state records process deaths and
generation fences, plus copies of unresolved cleanup projections, never jobs.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import select
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from preprocessing.dev import PrivateServer, atomic_json, http_request, load_config
from preprocessing.engine import OpenCodeServerEngine
from preprocessing.engine_evidence import (
    inspect_transcript,
    operation_marker,
    validate_session_ownership,
    validate_session_selection,
)
from preprocessing.engine_guardian import _pidfd_open, _pidfd_signal
from preprocessing.errors import ServiceError

MAX_DESCRIPTOR_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024


def read_projection(path, max_bytes=MAX_DESCRIPTOR_BYTES):
    with Path(path).open('rb') as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('Ownership projection exceeds inspection bound')
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get('entries', []), list):
        raise TypeError('Invalid ownership projection')
    return value


def filter_history(history, projection_path):
    """Read at request time. Malformed projection fails closed to the caller."""
    projection = read_projection(projection_path)
    owned = {sid for item in projection.get('entries', [])
             for sid in [item.get('sessionId'), *item.get('sessionIds', [])] if sid}
    return [item for item in history if item.get('id') not in owned]


def process_identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except FileNotFoundError:
        return None


def boot_identity():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


async def fence_writer(writer):
    """Kill only the exact writer identity; pidfd readiness proves its death."""
    pid, start = writer.get('pid'), writer.get('starttime')
    if writer.get('bootId') and writer['bootId'] != boot_identity():
        return True
    if type(pid) is not int or pid <= 1 or pid == os.getpid() or not isinstance(start, str):
        return False
    if process_identity(pid) != start:
        return True  # That exact process is already dead, including PID reuse.
    try:
        fd = _pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        if process_identity(pid) != start:
            return True
        _pidfd_signal(fd, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0)[0]:
                return True
            await asyncio.sleep(.02)
        return False
    finally:
        os.close(fd)


class StandaloneEngineOwner:
    def __init__(self, root, projection_path, *, request=None, fence_writer=fence_writer,
                 evidence_bytes=256 * 1024):
        if type(evidence_bytes) is not int or evidence_bytes <= 0:
            raise ValueError('Evidence byte limit must be a positive integer')
        self.evidence_bytes = evidence_bytes
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise ValueError('Owner state directory must have mode 0700')
        self.projection_path = Path(projection_path).resolve()
        self.state_path = self.root / 'owner.json'
        self.state = (read_projection(self.state_path, MAX_STATE_BYTES) if self.state_path.exists() else
                      {'engineInstanceId': None, 'guardian': None, 'fencedGenerations': [],
                        'deadEngineInstances': [], 'pendingGenerations': {}})
        if (not {'engineInstanceId', 'guardian', 'fencedGenerations', 'deadEngineInstances', 'pendingGenerations'} <= self.state.keys()
                or any(not isinstance(self.state[key], list)
                       or any(not isinstance(value, str) or not value for value in self.state[key])
                       for key in ('fencedGenerations', 'deadEngineInstances'))
                or not isinstance(self.state['pendingGenerations'], dict)):
            # Missing fencing/death records cannot be safely replaced with defaults.
            raise ValueError('Owner state is incomplete; repair its ownership evidence explicitly')
        self.request = request
        self.fence_writer = fence_writer
        self.descriptor_path = None
        self.descriptor = None
        self.state.setdefault('cleanupReceipts', [])
        self.state.setdefault('lastProjection', None)
        self._state_digest = None
        self._descriptor_digest = None

    def save(self):
        state_bytes = json.dumps(self.state, allow_nan=False, sort_keys=True).encode()
        if len(state_bytes) > MAX_STATE_BYTES:
            raise ValueError('Owner cleanup state exceeds its bounded storage')
        descriptor = None
        if self.descriptor is not None:
            descriptor = {**self.descriptor, 'recovery': self.recovery()}
            if len(json.dumps(descriptor, allow_nan=False).encode()) > MAX_DESCRIPTOR_BYTES:
                raise ValueError('Owner descriptor exceeds its bounded handoff')
        state_digest = hashlib.sha256(state_bytes).digest()
        if state_digest != self._state_digest:
            atomic_json(self.state_path, self.state)
            self._state_digest = state_digest
        if descriptor is not None and self.descriptor_path is not None:
            self.descriptor.update(descriptor)
            descriptor_digest = hashlib.sha256(json.dumps(descriptor, allow_nan=False, sort_keys=True).encode()).digest()
            if descriptor_digest != self._descriptor_digest:
                atomic_json(self.descriptor_path, descriptor)
                self._descriptor_digest = descriptor_digest

    def recovery(self):
        # This wrapper launches ordinary serve only: managed recovery is not claimed.
        return {'managed': False, 'extractionExcluded': True,
                'fencedGenerations': self.state['fencedGenerations'][:],
                'deadEngineInstances': self.state['deadEngineInstances'][:],
                'cleanupReceipts': copy.deepcopy(self.state['cleanupReceipts'])}

    @staticmethod
    def _entry_key(entry):
        return tuple(entry.get(key) for key in ('id', 'generation', 'engineInstanceId', 'sessionId', 'inputId', 'requestHash'))

    def _enqueue(self, projection):
        generation = projection['generation']
        previous = self.state['pendingGenerations'].get(generation)
        if previous:
            # Keep the exact predecessor writer. A replacement projection must
            # never authorize termination of a different process identity.
            if previous.get('writer') != projection.get('writer'):
                return
            entries = {self._entry_key(entry): entry for entry in previous.get('entries', [])}
            for entry in projection.get('entries', []):
                key = self._entry_key(entry)
                entries[key] = {**entry, **entries.get(key, {})}
            projection = {**projection, 'entries': list(entries.values())}
        self.state['pendingGenerations'][generation] = copy.deepcopy(projection)

    def _merge_projected_entries(self, projection):
        for generation, pending in self.state['pendingGenerations'].items():
            entries = {self._entry_key(entry): entry for entry in pending.get('entries', [])}
            for entry in projection.get('entries', []):
                if entry.get('generation') == generation:
                    key = self._entry_key(entry)
                    entries[key] = {**entry, **entries.get(key, {})}
            pending['entries'] = list(entries.values())

    @staticmethod
    def _writer_dead(writer):
        pid, start = writer.get('pid'), writer.get('starttime')
        if type(pid) is not int or pid <= 1 or not isinstance(start, str):
            return False
        return ((writer.get('bootId') and writer['bootId'] != boot_identity())
                or process_identity(pid) != start)

    def _prune_receipts(self, projection):
        if not projection or projection.get('generation') in self.state['fencedGenerations']:
            return
        outstanding = {(entry.get('sessionId'), entry.get('inputId')) for entry in projection.get('entries', [])}
        outstanding.update((entry.get('sessionId'), entry.get('inputId'))
                           for pending in self.state['pendingGenerations'].values() for entry in pending.get('entries', []))
        self.state['cleanupReceipts'] = [receipt for receipt in self.state['cleanupReceipts']
            if ((receipt['sessionId'], receipt['inputId']) in outstanding
                or receipt['generation'] not in self.state['fencedGenerations']
                or receipt['generation'] == projection.get('generation'))]

    async def reconcile(self, *, force=False):
        try:
            projection = read_projection(self.projection_path)
        except FileNotFoundError:
            projection = None
        if projection:
            generation = projection.get('generation')
            if not isinstance(generation, str) or not generation:
                raise ValueError('Projection requires a service generation')
            previous = self.state['lastProjection']
            if previous and previous.get('generation') != generation:
                self._enqueue(previous)
            self.state['lastProjection'] = copy.deepcopy(projection)
            expired = (force or generation in self.state['fencedGenerations']
                       or self._writer_dead(projection.get('writer', {}))
                       or projection.get('expiresAt', 0) <= time.time())
            if expired:
                # Save responsibility before signalling or making HTTP mutations.
                self._enqueue(projection)
            self._merge_projected_entries(projection)
            self.save()
        for generation, candidate in list(self.state['pendingGenerations'].items()):
            if generation not in self.state['fencedGenerations']:
                if not await self.fence_writer(candidate.get('writer', {})):
                    continue
                self.state['fencedGenerations'].append(generation)
                self.save()
                # Re-read the quiet writer's final projection after death. Prepared IDs
                # may have been published between our expiry observation and fencing.
                try:
                    latest = read_projection(self.projection_path)
                    self._merge_projected_entries(latest)
                    if latest.get('generation') == generation and latest.get('writer') == candidate.get('writer'):
                        self._enqueue(latest)
                        candidate = self.state['pendingGenerations'][generation]
                        self.save()
                    else:
                        self.save()
                except FileNotFoundError:
                    pass
            if self.request is None:
                continue
            remaining = []
            for entry in candidate.get('entries', []):
                try:
                    cleaned = await self._cleanup(entry, candidate)
                except (OSError, TimeoutError, ValueError, TypeError, KeyError, ServiceError):
                    # HTTP outages and malformed/oversized scoped evidence retain
                    # ownership. Reads and cleanup can retry; prompts never replay.
                    cleaned = False
                if not cleaned:
                    remaining.append(entry)
            candidate['entries'] = remaining
            if not remaining:
                del self.state['pendingGenerations'][generation]
            self.save()
        self._prune_receipts(projection)
        self.save()
        return not self.state['pendingGenerations']

    @staticmethod
    def _receipt(entry, evidence):
        return {'operationId': entry['id'], **{key: entry[key] for key in
                ('jobId', 'generation', 'engineInstanceId', 'sessionId', 'inputId', 'requestHash')},
                'evidence': evidence, 'settlement': {'settled': True, 'sessionClosed': True,
                                                     'sessionId': entry['sessionId'], 'remoteOutcome': 'unknown'}}

    def _receipt_fits(self, receipt):
        recovery = self.recovery()
        recovery['cleanupReceipts'].append(receipt)
        # Reserve descriptor identity/auth/path space when it isn't published yet.
        limit = MAX_DESCRIPTOR_BYTES if self.descriptor is not None else MAX_DESCRIPTOR_BYTES - 65536
        return len(json.dumps({**(self.descriptor or {}), 'recovery': recovery}, allow_nan=False).encode()) <= limit

    def _confirm_receipt(self, entry):
        receipt = self._receipt(entry, entry['_cleanupEvidence'])
        if not self._receipt_fits(receipt):
            return False
        self.state['cleanupReceipts'].append(receipt)
        del entry['_cleanupEvidence']
        self.save()
        return True

    async def _evidence(self, entry, *, old_dead=False):
        path = '/api/session/' + entry['sessionId'] + '/message?' + urlencode({'limit': 200, 'order': 'asc'})
        status, response = await self.request('GET', path)
        if status != 200 or not isinstance(response, dict):
            raise ValueError('Known transcript is unavailable')
        inspection = inspect_transcript(response, entry, response_bytes=self.evidence_bytes)
        evidence = inspection.evidence
        if inspection.outcome not in {'succeeded', 'failed', 'interrupted'} or (inspection.outcome == 'succeeded' and not inspection.complete):
            if not old_dead:
                return None
            evidence['outcome'] = None  # Boot death can settle execution, never manufacture terminal evidence.

        def numeric(value):
            return (value is None or (type(value) in {int, float} and math.isfinite(value) and value >= 0)
                    or (isinstance(value, dict) and all(isinstance(key, str) and len(key) <= 64 and numeric(item)
                                                       for key, item in value.items())))

        usage = evidence['usage']
        if usage is not None and (not numeric(usage.get('tokens')) or
                                  (usage.get('cost') is not None and
                                   (type(usage['cost']) not in {int, float} or not math.isfinite(usage['cost']) or usage['cost'] < 0))):
            return None
        if len(json.dumps(evidence, allow_nan=False).encode()) > self.evidence_bytes:
            return None
        return evidence

    async def _cleanup(self, entry, projection):
        sid, mid = entry.get('sessionId'), entry.get('inputId')
        if not isinstance(sid, str) or not re.fullmatch(r'ses_[A-Za-z0-9_-]+', sid):
            return False
        if not isinstance(mid, str) or not re.fullmatch(r'msg_[A-Za-z0-9_-]+', mid):
            return False
        try:
            marker = operation_marker(entry)
            if (any(not isinstance(entry[key], str) or not entry[key]
                    for key in ('id', 'jobId', 'generation', 'engineInstanceId'))
                    or not re.fullmatch('[0-9a-f]{64}', entry['requestHash'])
                    or entry.get('agent') != 'preprocessing' or not isinstance(entry.get('model'), dict)
                    or entry.get('location') != {'directory': projection.get('directory')}):
                return False
        except (KeyError, TypeError):
            return False
        if entry['generation'] not in self.state['fencedGenerations']:
            return False
        if any({key: receipt[key] for key in marker} == marker and receipt.get('sessionId') == sid
               for receipt in self.state['cleanupReceipts']):
            return True
        path = '/api/session/' + sid
        status, envelope = await self.request('GET', path)
        old_dead = entry.get('engineInstanceId', projection.get('engineInstanceId')) in self.state['deadEngineInstances']
        if status == 404:
            if '_cleanupEvidence' in entry:
                return self._confirm_receipt(entry)
            # Only confirmed old-engine death rules out old local execution.
            return old_dead
        if status != 200 or not isinstance(envelope, dict):
            return False
        session = envelope.get('data', {})
        try:
            validate_session_ownership(session, entry)
            validate_session_selection(session, entry)
        except ServiceError:
            return False
        # Do not recursively delete unproven child sessions. This is a parent-scoped
        # inspection, never global enumeration or cancellation.
        status, children = await self.request('GET', '/api/session?' + urlencode({'parentID': sid, 'limit': 1}))
        if status != 200 or children.get('data') != [] or children.get('cursor', {}).get('next'):
            return False
        status, inbox = await self.request('GET', path + '/inbox')
        if status != 200 or not isinstance(inbox.get('data'), list):
            return False
        if any(item.get('id') != mid for item in inbox['data']):
            return False
        if inbox['data']:
            cancel_status, _ = await self.request('DELETE', path + '/inbox/' + mid)
            if cancel_status not in {200, 204}:
                return False
        interrupt_status, _ = await self.request('POST', path + '/interrupt')  # Never resume=true.
        if interrupt_status != 200:
            return False
        wait_status, _ = await self.request('POST', '/api/experimental/session/' + sid + '/wait')
        if wait_status not in {200, 204}:
            return False
        inbox_status, inbox = await self.request('GET', path + '/inbox')
        if inbox_status != 200 or inbox.get('data') != []:
            return False
        evidence = await self._evidence(entry, old_dead=old_dead)
        if evidence is None:
            return False
        terminal_evidence = evidence['outcome'] in {'succeeded', 'failed', 'interrupted'}
        if terminal_evidence and not self._receipt_fits(self._receipt(entry, evidence)):
            return False
        status, children = await self.request('GET', '/api/session?' + urlencode({'parentID': sid, 'limit': 1}))
        if status != 200 or children.get('data') != [] or children.get('cursor', {}).get('next'):
            return False
        # Durable delete intent/evidence closes the crash window between HTTP
        # deletion and receipt publication. No receipt is exposed before readback.
        if terminal_evidence:
            entry['_cleanupEvidence'] = evidence
            self.save()
        status, _ = await self.request('DELETE', path)
        if status not in {200, 204}:
            return False
        status, _ = await self.request('GET', path)
        return status == 404 and (self._confirm_receipt(entry) if terminal_evidence else old_dead)


async def _run(args):
    owner = StandaloneEngineOwner(args.state_dir, args.projection, evidence_bytes=args.evidence_bytes)
    lock_fd = os.open(owner.root / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = PrivateServer(owner.root / 'server', args.opencode_executable,
                           load_config(args.engine_config, args.credential_env), args.credential_env)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    owner.descriptor_path = Path(args.descriptor).resolve()
    launched = False
    try:
        # Withdraw stale admission before checking any prior supervised process.
        with contextlib.suppress(FileNotFoundError):
            owner.descriptor_path.unlink()
        prior = owner.state.get('guardian')
        if prior and prior.get('bootId') and prior['bootId'] != boot_identity():
            prior = {**prior, 'pid': -1}  # A prior boot's processes cannot survive.
        deadline = time.monotonic() + 30
        while prior and process_identity(prior['pid']) == prior['starttime']:
            if time.monotonic() >= deadline:
                raise ServiceError('OWNER_BLOCKED', 'Prior engine guardian is still alive; replacement is blocked.')
            await asyncio.sleep(.05)
        previous = owner.state.get('engineInstanceId')
        if previous and not prior and previous not in owner.state['deadEngineInstances']:
            raise ServiceError('OWNER_BLOCKED', 'Prior engine process identity is missing; replacement is blocked.')
        await owner.reconcile(force=True)  # Startup rescan fences the old service.
        if any(generation not in owner.state['fencedGenerations'] for generation in owner.state['pendingGenerations']):
            raise ServiceError('OWNER_BLOCKED', 'Prior service writer could not be fenced.')
        descriptor = await server.start(mode='standalone', recovery=owner.recovery())
        launched = True
        # PrivateServer has now acquired the guardian-retained server lock. A
        # vanished guardian PID alone never authorizes old-engine death evidence.
        if previous and previous not in owner.state['deadEngineInstances']:
            owner.state['deadEngineInstances'].append(previous)
        for identity in descriptor['recovery']['deadEngineInstances']:
            if identity not in owner.state['deadEngineInstances']:
                owner.state['deadEngineInstances'].append(identity)
        descriptor['recovery'] = owner.recovery()
        owner.state.update(engineInstanceId=descriptor['engineInstanceId'],
                           guardian={'pid': server.process.pid, 'starttime': process_identity(server.process.pid),
                                     'bootId': boot_identity()})
        owner.save()

        probe = OpenCodeServerEngine(descriptor, input_roots=[])
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                readiness = await probe.ready({'models': {'vision': server.config['model']}})
                if readiness.get('ready') is True:
                    break
                await asyncio.sleep(.05)
            else:
                raise ServiceError('ENGINE_UNAVAILABLE', 'Controlled extraction agent/model catalog did not become ready.')
        finally:
            await probe.close()

        async def request(method, path, body=None):
            return await asyncio.to_thread(http_request, descriptor, method, path, body)

        owner.request = request
        while not await owner.reconcile():
            # Startup outages retain the live supervised engine and candidates;
            # admission stays unpublished until owned cleanup can be verified.
            if stopped.is_set():
                return
            if server.process.returncode is not None:
                raise ServiceError('ENGINE_UNAVAILABLE', 'Owned engine exited during startup cleanup.')
            try:
                await asyncio.wait_for(stopped.wait(), 1)
            except TimeoutError:
                pass
        owner.descriptor = descriptor
        owner.save()
        while not stopped.is_set():
            if server.process.returncode is not None:
                raise ServiceError('ENGINE_UNAVAILABLE', 'Owned engine exited; restart the external owner.')
            await owner.reconcile()
            try:
                await asyncio.wait_for(stopped.wait(), 1)
            except TimeoutError:
                pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            owner.descriptor_path.unlink()
        owner.descriptor = None
        await server.close()
        if launched:
            identity = owner.state.get('engineInstanceId')
            if identity and identity not in owner.state['deadEngineInstances']:
                owner.state['deadEngineInstances'].append(identity)
            owner.state['guardian'] = None
            owner.save()
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='preprocess-engine-owner', description='Externally supervised dedicated OpenCode HTTP owner (Linux).')
    for flag in ('state-dir', 'projection', 'descriptor', 'opencode-executable', 'engine-config'):
        parser.add_argument('--' + flag, type=Path, required=True)
    parser.add_argument('--credential-env', action='append', default=[])
    parser.add_argument('--evidence-bytes', type=int, default=256 * 1024,
                        help='Maximum serialized extraction evidence per cleanup receipt (default 262144).')
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
        return 0
    except (OSError, ValueError, TypeError, ServiceError):
        print('Standalone engine owner blocked; inspect private configuration, process identity, and ownership projection.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
