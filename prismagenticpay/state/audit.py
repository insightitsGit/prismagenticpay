"""Append-only authorization audit log."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    event_id: str
    event_type: str
    authorization_id: Optional[str] = None
    payment_hash: Optional[str] = None
    actor_id: str
    detail: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuditLog:
    def __init__(self):
        self._lock = threading.Lock()
        self._events: List[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def list(self, payment_hash: Optional[str] = None) -> List[AuditEvent]:
        with self._lock:
            if payment_hash is None:
                return list(self._events)
            return [e for e in self._events if e.payment_hash == payment_hash]


class DurableAuditLog:
    def __init__(self, ledger):
        self.ledger = ledger

    def append(self, event):
        self.ledger.put_record("audit", event.event_id, event.model_dump(mode="json"))

    def list(self, payment_hash=None):
        events = [AuditEvent.model_validate(item) for item in self.ledger.list_records("audit")]
        return [event for event in events if payment_hash is None or event.payment_hash == payment_hash]
