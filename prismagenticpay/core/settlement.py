"""Phase-2 settlement handshake. Rails present a signed decision; this kernel never clears funds."""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from pydantic import BaseModel

from prismagenticpay.core.clock import Clock, SystemClock
from prismagenticpay.core.signing import DecisionSigner
from prismagenticpay.domain.models import AuthorizationDecision, PaymentAuthStatus, PaymentProposal
from prismagenticpay.state.ledger import AtomicAuthorityLedger


class SettlementRequest(BaseModel):
    authorization: AuthorizationDecision
    payment_hash: str
    mandate_hash: str
    authority_snapshot_hash: str
    rail_id: str


class SettlementGatewayHarness:
    def __init__(
        self,
        ledger: AtomicAuthorityLedger,
        signer: Optional[DecisionSigner] = None,
        clock: Optional[Clock] = None,
        allowed_rails: Optional[set[str]] = None,
    ):
        self.ledger = ledger
        self.signer = signer
        self.clock = clock or SystemClock()
        self.allowed_rails = allowed_rails if allowed_rails is not None else {"stripe", "iso8583", "test"}

    def settle_transaction(
        self,
        auth_decision: AuthorizationDecision,
        submitted_payment_hash: str,
        now: Optional[datetime] = None,
        *,
        proposal: Optional[PaymentProposal] = None,
        rail_id: str = "test",
    ) -> bool:
        current_time = now or self.clock.now()
        if rail_id not in self.allowed_rails:
            return False
        if self.signer is not None and not self.signer.verify(auth_decision):
            return False
        mandate_hash = proposal.mandate_hash if proposal is not None else None
        snapshot_hash = auth_decision.authority_snapshot_hash if proposal is None else None
        if proposal is not None:
            if proposal.compute_canonical_payment_hash() != submitted_payment_hash:
                if auth_decision.reservation_id:
                    self.ledger.release(auth_decision.reservation_id)
                return False
            mandate_hash = proposal.mandate_hash
            snapshot_hash = None
        if not auth_decision.reservation_id:
            return False
        if not auth_decision.is_valid_for_settlement(
            submitted_payment_hash,
            now=current_time,
            mandate_hash=mandate_hash,
            authority_snapshot_hash=snapshot_hash,
        ):
            if auth_decision.status is PaymentAuthStatus.AUTHORIZED:
                self.ledger.release(auth_decision.reservation_id)
            return False
        success, _msg = self.ledger.commit(auth_decision.reservation_id, now=current_time)
        return success

    def settle(self, request: SettlementRequest, now: Optional[datetime] = None) -> bool:
        if request.mandate_hash != request.authorization.mandate_hash or request.authority_snapshot_hash != request.authorization.authority_snapshot_hash:
            return False
        return self.settle_transaction(
            request.authorization,
            request.payment_hash,
            now=now,
            rail_id=request.rail_id,
        )

    def release_hold(self, auth_decision: AuthorizationDecision) -> bool:
        if not auth_decision.reservation_id:
            return False
        return self.ledger.release(auth_decision.reservation_id)

    def expire_hold(
        self,
        auth_decision: AuthorizationDecision,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        if not auth_decision.reservation_id:
            return False, "RESERVATION_NOT_FOUND"
        return self.ledger.expire(auth_decision.reservation_id, now=now)
