"""Crash-safe SQLite snapshot of AtomicAuthorityLedger."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Optional, Tuple

from prismagenticpay.state.ledger import (
    AtomicAuthorityLedger,
    BucketAvailability,
    MultiBucketReservation,
)


class SqliteAuthorityLedger:
    def __init__(
        self,
        path: str,
        session_budget_cents: int,
        daily_budget_cents: int,
        mandate_budget_cents: int,
        default_hold_ttl_seconds: int = 120,
    ):
        self._mem = AtomicAuthorityLedger(
            session_budget_cents=session_budget_cents,
            daily_budget_cents=daily_budget_cents,
            mandate_budget_cents=mandate_budget_cents,
            default_hold_ttl_seconds=default_hold_ttl_seconds,
        )
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS ledger_snapshot (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)"
        )
        self._conn.commit()
        self._load()
        self._mem._on_mutate = self._persist

    def _load(self) -> None:
        row = self._conn.execute("SELECT payload FROM ledger_snapshot WHERE id = 1").fetchone()
        if row:
            self._mem.load_snapshot(json.loads(row[0]))

    def _persist(self, snapshot: dict) -> None:
        payload = json.dumps(snapshot)
        self._conn.execute(
            "INSERT INTO ledger_snapshot (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (payload,),
        )
        self._conn.commit()

    def inspect_available(self, **kwargs) -> BucketAvailability:
        return self._mem.inspect_available(**kwargs)

    def reserve(self, *args, **kwargs) -> Tuple[bool, Optional[str], Optional[MultiBucketReservation], str]:
        return self._mem.reserve(*args, **kwargs)

    def commit(self, reservation_id: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
        return self._mem.commit(reservation_id, now=now)

    def release(self, reservation_id: str, *, gathered: bool = False) -> bool:
        return self._mem.release(reservation_id, gathered=gathered)

    def expire(self, reservation_id: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
        return self._mem.expire(reservation_id, now=now)

    def get(self, reservation_id: str) -> Optional[MultiBucketReservation]:
        return self._mem.get(reservation_id)
