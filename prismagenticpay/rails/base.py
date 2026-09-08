"""Settlement rail contract. Rails move funds; this kernel only commits after they succeed."""

from __future__ import annotations

from typing import Optional, Protocol

from pydantic import BaseModel

from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal


class RailResult(BaseModel):
    ok: bool
    rail_id: str
    reference: str
    detail: str = ""


class SettlementRail(Protocol):
    rail_id: str

    def capture(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        payment_token: str,
    ) -> RailResult:
        ...

    def refund(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        capture_reference: str,
    ) -> RailResult:
        ...

    def void(self, decision: AuthorizationDecision, capture_reference: Optional[str] = None) -> RailResult:
        ...
