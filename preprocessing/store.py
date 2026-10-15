"""Single-owner SQLite persistence. The coordinator is the only writer."""
import json
import sqlite3
import uuid
from collections.abc import Iterator

from preprocessing.errors import ServiceError

TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled', 'interrupted'}


def unsettled(operation):
    settlement = operation.get('settlement')
    if isinstance(settlement, dict):
        return settlement.get('settled') is not True
    if isinstance(settlement, bool):
        return not settlement
    return operation.get('state') in {'preparing', 'prepared', 'dispatch_intent', 'admitted',
                                      'outcome_unknown', 'cleanup_pending'}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                submission_key TEXT UNIQUE NOT NULL, request TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scheduling_receipts(
                request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                action TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL);
        ''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('scope', 'scope_' + uuid.uuid4().hex))
        self.scope = self.db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()[0]
        # Unclassified accepted work cannot become runnable merely on upgrade.
        for _, job in list(self.all()):
            if 'scheduling' not in job:
                self.save(job)

    @property
    def activity_revision(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='activityRevision'").fetchone()
        return int(row[0]) if row else 0

    def bump_activity(self):
        with self.db:
            self._bump_activity()

    def _bump_activity(self):
        self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        ('activityRevision', str(self.activity_revision + 1)))

    def _scheduling(self, job):
        operations = self.operations(job['id'])
        pending = any(unsettled(item) for item in operations)
        publication = job.get('resultId') and (job.get('documents') or {}).get('state') != 'ready'
        scheduling = job.get('scheduling')
        if scheduling is None:
            job['scheduling'] = {'class': 'terminal' if job['state'] in TERMINAL and not pending and not publication else 'settling',
                                 'revision': 1, 'activationRevision': 1, 'block': None}
            return
        if (job['state'] == 'cancelling' or (job['state'] == 'queued' and operations)
                or (pending and job['state'] != 'running')
                or any(unsettled(item) and item.get('state') in {'outcome_unknown', 'cleanup_pending'} for item in operations)):
            kind = 'settling'
        elif job['state'] in TERMINAL:
            kind = 'runnable' if publication else 'terminal'
        elif job['state'] == 'running':
            kind = 'runnable'
        else:
            kind = scheduling['class']
            if kind == 'settling' and job.get('checkpointContract') == 'queued-input-v1' and not job.get('startedAt'):
                kind = 'runnable'
        if kind != scheduling['class']:
            scheduling.update({'class': kind, 'revision': scheduling['revision'] + 1})

    def get(self, job_id):
        row = self.db.execute('SELECT body FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'Job not found.', 404)
        return json.loads(row[0])

    def lookup(self, key):
        row = self.db.execute('SELECT body,request FROM jobs WHERE submission_key=?', (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def insert(self, job, request):
        self._scheduling(job)
        with self.db:
            self.db.execute('INSERT INTO jobs(id,submission_key,request,body) VALUES(?,?,?,?)',
                            (job['id'], job['submissionKey'], request, encode(job)))
            self._bump_activity()

    def save(self, job):
        self._scheduling(job)
        job['revision'] += 1
        with self.db:
            self.db.execute('UPDATE jobs SET body=? WHERE id=?', (encode(job), job['id']))
            self._bump_activity()
        return job

    def control_replay(self, job_id, action, body):
        row = self.db.execute('SELECT * FROM scheduling_receipts WHERE request_id=?', (body['requestId'],)).fetchone()
        if row is None:
            return None
        if row['job_id'] != job_id or row['action'] != action or row['request'] != encode(body):
            raise ServiceError('IDEMPOTENCY_CONFLICT', 'Scheduling request UUID already has a different binding.', 409)
        return json.loads(row['response'])

    def save_control(self, job, action, body, receipt, *, changed=True):
        """Commit the transition and its exact replay response in one FULL-sync transaction."""
        with self.db:
            if changed:
                self._scheduling(job)
                job['revision'] += 1
                self.db.execute('UPDATE jobs SET body=? WHERE id=?', (encode(job), job['id']))
                self._bump_activity()
            response = {**job, 'receipt': receipt}
            self.db.execute('INSERT INTO scheduling_receipts VALUES(?,?,?,?,?)',
                            (body['requestId'], job['id'], action, encode(body), encode(response)))
        return response

    def active_count(self) -> int:
        placeholders = ','.join('?' for _ in TERMINAL)
        return self.db.execute(
            f"""SELECT COUNT(*) FROM jobs
                WHERE COALESCE(json_extract(body, '$.scheduling.class'), 'settling') = 'settling'
                OR (json_extract(body, '$.scheduling.class') = 'runnable'
                    AND json_extract(body, '$.state') NOT IN ({placeholders}))""",
            tuple(sorted(TERMINAL)),
        ).fetchone()[0]

    def state_count(self, state):
        return self.db.execute("SELECT COUNT(*) FROM jobs WHERE json_extract(body, '$.state') = ?", (state,)).fetchone()[0]

    def next_queued(self) -> dict | None:
        row = self.db.execute('''
            SELECT body FROM jobs WHERE json_extract(body, '$.state') = 'queued'
            AND json_extract(body, '$.scheduling.class') = 'runnable'
            ORDER BY seq ASC LIMIT 1
        ''').fetchone()
        return json.loads(row[0]) if row else None

    def max_sequence(self) -> int:
        return self.db.execute('SELECT COALESCE(MAX(seq), 0) FROM jobs').fetchone()[0]

    def page(self, ceiling: int, before: int, limit: int, state=None,
             snapshot_id=None, upload_batch_id=None) -> list[tuple[int, dict]]:
        """Return newest-first matches within an inclusive ceiling and exclusive cursor."""
        if type(limit) is not int or limit < 0:
            raise ValueError('Page limit must be a nonnegative integer.')
        clauses = ['seq <= ?', 'seq < ?']
        values = [ceiling, before]
        for path, value in [('$.state', state), ('$.source.snapshotId', snapshot_id),
                            ('$.correlation.uploadBatchId', upload_batch_id)]:
            if value is not None:
                clauses.append(f"json_extract(body, '{path}') = ?")
                values.append(value)
        values.append(limit)
        rows = self.db.execute(
            'SELECT seq,body FROM jobs WHERE ' + ' AND '.join(clauses) + ' ORDER BY seq DESC LIMIT ?',
            values,
        )
        return [(row['seq'], json.loads(row['body'])) for row in rows]

    def snapshot(self, snapshot_id: str) -> dict | None:
        row = self.db.execute('''
            SELECT json_extract(body, '$.source') FROM jobs
            WHERE json_extract(body, '$.source.snapshotId') = ?
            ORDER BY seq DESC LIMIT 1
        ''', (snapshot_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> Iterator[tuple[int, dict]]:
        for row in self.db.execute('SELECT seq,body FROM jobs ORDER BY seq DESC'):
            yield row['seq'], json.loads(row['body'])

    def ready_summaries(self) -> Iterator[dict]:
        """Stream published catalog metadata without loading stage or product history."""
        rows = self.db.execute('''
            SELECT json_object(
                'resultId', json_extract(body, '$.resultId'),
                'jobId', id,
                'source', json_extract(body, '$.source'),
                'profile', json_extract(body, '$.profile.profileDigest'),
                'build', json_extract(body, '$.profile.build'),
                'finishedAt', json_extract(body, '$.finishedAt'),
                'outcome', json_extract(body, '$.state'),
                'rerunOf', json_extract(body, '$.rerunOf'),
                'resumeOf', json_extract(body, '$.resumeOf'),
                'coverage', json_extract(body, '$.coverage'),
                'path', json_extract(body, '$.resultId') || '/index.md'
            ) FROM jobs
            WHERE json_extract(body, '$.documents.state') = 'ready'
            ORDER BY json_extract(body, '$.finishedAt') DESC,
                     json_extract(body, '$.resultId') DESC
        ''')
        for row in rows:
            yield json.loads(row[0])

    def operation(self, operation):
        with self.db:
            self.db.execute('INSERT INTO operations VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                             (operation['id'], operation['jobId'], encode(operation)))
            job = self.get(operation['jobId'])
            self._scheduling(job)
            job['revision'] += 1
            self.db.execute('UPDATE jobs SET body=? WHERE id=?', (encode(job), job['id']))
            self._bump_activity()

    def get_operation(self, operation_id):
        row = self.db.execute('SELECT body FROM operations WHERE id=?', (operation_id,)).fetchone()
        if row is None:
            raise ServiceError('NOT_FOUND', 'Operation not found.', 404)
        return json.loads(row[0])

    def iter_operations(self, *, only_unsettled=False):
        """Stream the operation ledger, including records belonging to terminal jobs."""
        for row in self.db.execute('SELECT body FROM operations ORDER BY rowid'):
            operation = json.loads(row[0])
            if not only_unsettled or unsettled(operation):
                yield operation

    def metadata(self, key, value):
        with self.db:
            self.db.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                            (key, encode(value)))

    def operations(self, job_id):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM operations WHERE job_id=?', (job_id,))]

    def close(self):
        self.db.close()
