"""ERP fact connectors. They fetch typed facts; they never score stale data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Protocol

from prismthinker import FactValue

from prismagenticpay.connectors.base import FactMeta, FactTtlValidator
from prismagenticpay.domain.models import PaymentProposal


class FactBundle:
    def __init__(self, facts: Dict[str, FactValue], metadata: Dict[str, FactMeta]):
        self.raw_facts = facts
        self.metadata = metadata

    def fresh(self, now: datetime | None = None) -> Dict[str, FactValue]:
        return FactTtlValidator.filter_fresh_facts(self.raw_facts, self.metadata, now=now)


class FactConnector(Protocol):
    source: str

    def fetch(self, proposal: PaymentProposal, now: datetime | None = None) -> FactBundle:
        ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
