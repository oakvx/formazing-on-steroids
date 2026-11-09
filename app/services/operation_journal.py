"""Durable, fail-closed operation journal for side effects across providers.

SQLite is used solely as a local coordination/audit store; Notion remains the
source of truth for training data. Never store message bodies or auth tokens.
A journal record is unique per (Notion training ID, operation type).
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class DuplicateOperation(RuntimeError):
    """A previous attempt exists: do not repeat potentially sent side effects."""


class InvalidTransition(RuntimeError):
    """An operation cannot be resumed from its current durable checkpoint."""


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class OperationJournal:
    ACTIONS = frozenset({'notification', 'feedback'})
    STATES = frozenset({'running', 'needs_review', 'ready_telegram', 'completed'})
    STEPS = frozenset({
        'created', 'validated', 'microsoft_attempted', 'notion_pending',
        'telegram_not_started', 'telegram_attempted', 'feedback_notion_pending',
        'notion_reconcile_attempted', 'completed',
    })

    def __init__(self, db_path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != 'nt':
            os.chmod(self.db_path.parent, self.db_path.parent.stat().st_mode & 0o700)
        # SQLite connection per operation: safe with multiple Flask workers.
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    training_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    checkpoint TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(training_id, action)
                );
                CREATE INDEX IF NOT EXISTS idx_operations_updated
                    ON operations(updated_at DESC);
                CREATE TABLE IF NOT EXISTS operation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(id),
                    event TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
            """)
        if os.name != 'nt':
            os.chmod(self.db_path, 0o600)

    @classmethod
    def configured(cls):
        from config import BASE_DIR, proteus
        supplied = proteus.get('APP.OPERATIONS_DB')
        return cls(supplied if supplied else Path(BASE_DIR) / 'var' / 'operations.sqlite3')

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=10000')
            connection.execute('PRAGMA foreign_keys=ON')
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _public(row):
        return {key: row[key] for key in (
            'id', 'training_id', 'action', 'actor', 'status',
            'checkpoint', 'created_at', 'updated_at'
        )}

    def begin(self, training_id, action, actor):
        if action not in self.ACTIONS:
            raise ValueError('Unsupported operation')
        if not isinstance(training_id, str) or not 1 <= len(training_id) <= 100:
            raise ValueError('Invalid training ID')
        if not actor or len(actor) > 254:
            raise ValueError('An authenticated actor is required')
        operation_id, now = uuid4().hex, _utc()
        with self._connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO operations
                       (id, training_id, action, actor, status, checkpoint, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'running', 'created', ?, ?)""",
                    (operation_id, training_id, action, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateOperation(
                    'Operazione già registrata. Controlla il Centro operazioni prima di ripetere invii.'
                ) from exc
            self._event(conn, operation_id, 'created', actor, now)
        return OperationAttempt(self, operation_id, actor)

    @staticmethod
    def _event(conn, operation_id, event, actor, now):
        conn.execute(
            """INSERT INTO operation_events (operation_id, event, actor, occurred_at)
               VALUES (?, ?, ?, ?)""",
            (operation_id, event, actor, now),
        )

    def checkpoint(self, operation_id, step, actor, *, status='running', metadata=None):
        if step not in self.STEPS or status not in self.STATES:
            raise ValueError('Unknown operation transition')
        now = _utc()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                'SELECT status, metadata FROM operations WHERE id=?', (operation_id,)
            ).fetchone()
            if row is None or row['status'] == 'completed':
                raise InvalidTransition('Operation is missing or already completed')
            stored = json.loads(row['metadata'])
            if metadata:
                # Only minimal resumption fields are persisted, never mail text.
                if set(metadata) - {'code', 'teams_link'}:
                    raise ValueError('Unexpected operation metadata')
                stored.update(metadata)
            conn.execute(
                """UPDATE operations SET status=?, checkpoint=?, metadata=?, updated_at=?
                   WHERE id=?""",
                (status, step, json.dumps(stored), now, operation_id),
            )
            self._event(conn, operation_id, step + ':' + status, actor, now)

    def review(self, operation_id, actor):
        """Leave the exact last checkpoint intact for manual reconciliation."""
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                'SELECT checkpoint, status FROM operations WHERE id=?', (operation_id,)
            ).fetchone()
            if row and row['status'] not in ('completed', 'needs_review'):
                now = _utc()
                conn.execute(
                    "UPDATE operations SET status='needs_review', updated_at=? WHERE id=?",
                    (now, operation_id),
                )
                self._event(conn, operation_id, 'manual_review_required', actor, now)

    def claim(self, operation_id, actor, *, status, checkpoint, next_checkpoint):
        """Atomic compare-and-swap: one worker at most may resume a step."""
        if next_checkpoint not in self.STEPS:
            raise ValueError('Unknown checkpoint')
        now = _utc()
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            updated = conn.execute(
                """UPDATE operations SET status='running', checkpoint=?, updated_at=?
                   WHERE id=? AND status=? AND checkpoint=?""",
                (next_checkpoint, now, operation_id, status, checkpoint),
            )
            if updated.rowcount != 1:
                raise InvalidTransition('Operazione cambiata o non recuperabile automaticamente.')
            self._event(conn, operation_id, 'claimed:' + next_checkpoint, actor, now)

    def get(self, operation_id, *, include_metadata=False):
        with self._connect() as conn:
            row = conn.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        if not row:
            return None
        result = self._public(row)
        if include_metadata:
            result['metadata'] = json.loads(row['metadata'])
        return result

    def recent(self, limit=75):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Invalid limit')
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM operations ORDER BY updated_at DESC, id DESC LIMIT ?',
                (limit,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def events(self, operation_id, limit=100):
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT event, actor, occurred_at FROM operation_events
                   WHERE operation_id=? ORDER BY id DESC LIMIT ?""",
                (operation_id, min(max(int(limit), 1), 100)),
            ).fetchall()
        return [dict(row) for row in rows]


class OperationAttempt:
    """Progress recorder deliberately injected into TrainingService on admin POSTs."""

    def __init__(self, journal, operation_id, actor):
        self.journal = journal
        self.id = operation_id
        self.actor = actor

    def step(self, step, *, metadata=None, status='running'):
        self.journal.checkpoint(
            self.id, step, self.actor, metadata=metadata, status=status
        )

    def complete(self):
        self.step('completed', status='completed')

    def review(self):
        self.journal.review(self.id, self.actor)
