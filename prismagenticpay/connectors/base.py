"""Fact freshness withholding. Stale or untracked facts are omitted, never degraded."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from pydantic import BaseModel, Field
from prismthinker import FactValue


class FactMeta(BaseModel):
    source: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_seconds: int = 300

    def is_fresh(self, now: datetime) -> bool:
        if self.ttl_seconds < 0:
            return False
        return 0 <= (now - self.fetched_at).total_seconds() < self.ttl_seconds


class FactTtlValidator:
    @staticmethod
    def filter_fresh_facts(
        raw_facts: Dict[str, FactValue],
        metadata_map: Dict[str, FactMeta],
        now: Optional[datetime] = None,
    ) -> Dict[str, FactValue]:
        check_time = now or datetime.now(timezone.utc)
        fresh: Dict[str, FactValue] = {}
        for key, fact in raw_facts.items():
            meta = metadata_map.get(key)
            # Fail-closed: no metadata means the fact is not authoritative.
            if meta is None:
                continue
            if meta.is_fresh(check_time):
                fresh[key] = fact
        return fresh
