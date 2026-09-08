"""Transactional SQLite workflow store; reload under the database writer lock.

All workflow records and ledger mutations commit together. No network work is
performed inside a transaction. Multiple local connections serialize via SQLite.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import sqlite3
import threading

from prismagenticpay.state.ledger import AtomicAuthorityLedger


class SqliteAuthorityLedger:
    def __init__(self, path, session_budget_cents, daily_budget_cents,
                 mandate_budget_cents, default_hold_ttl_seconds=120):
        self._mem = AtomicAuthorityLedger(session_budget_cents, daily_budget_cents,
                                         mandate_budget_cents, default_hold_ttl_seconds)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS ledger_snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")
        with self.transaction():
            pass

    @contextmanager
    def transaction(self):
        with self._lock:
            outer = self._depth == 0
            before = None
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    row = self._conn.execute("SELECT payload FROM ledger_snapshot WHERE id = 1").fetchone()
                    if row:
                        self._mem.load_snapshot(json.loads(row[0]))
                    before = self._mem.snapshot()
                except BaseException:
                    self._conn.rollback()
                    raise
            self._depth += 1
            try:
                yield self
                if outer:
                    self._persist(self._mem.snapshot())
                    self._conn.commit()
            except BaseException:
                if outer:
                    self._conn.rollback()
                    self._mem.load_snapshot(before)
                raise
            finally:
                self._depth -= 1

    def _persist(self, snapshot):
        self._conn.execute(
            "INSERT INTO ledger_snapshot (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (json.dumps(snapshot),),
        )

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(self._mem, name)
        if not callable(method):
            raise AttributeError(name)
        def call(*args, **kwargs):
            with self.transaction():
                return deepcopy(method(*args, **kwargs))
        return call

    def readiness(self):
        with self.transaction():
            return self._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def close(self):
        with self._lock:
            self._conn.close()
