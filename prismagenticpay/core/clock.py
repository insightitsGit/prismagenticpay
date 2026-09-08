"""Injectable clock. Settlement and TTL use the same source."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    def __init__(self, instant: datetime):
        self._instant = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._instant

    def advance(self, instant: datetime) -> None:
        self._instant = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
