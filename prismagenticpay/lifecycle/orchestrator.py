"""Authorize locally, then capture/refund/void on a rail, then mutate the ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1

from prismagenticpay.core.clock import Clock, SystemClock
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.core.signing import DecisionSigner
from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal
from prismagenticpay.rails.base import RailResult, SettlementRail
from prismagenticpay.receipts.ap2 import PaymentReceipt, ReceiptIssuer
from prismagenticpay.state.audit import AuditEvent, AuditLog
from prismagenticpay.state.ledger import AtomicAuthorityLedger
import uuid


class RailOrchestrator:
    def __init__(
        self,
        ledger: AtomicAuthorityLedger,
        gateway: SettlementGatewayHarness,
        rails: Dict[str, SettlementRail],
        signer: DecisionSigner,
        *,
        receipt_key: EllipticCurvePrivateKey | None = None,
        audit: Optional[AuditLog] = None,
        clock: Optional[Clock] = None,
    ):
        self.ledger = ledger
        self.gateway = gateway
        self.rails = rails
        self.signer = signer
        self.receipts = ReceiptIssuer(receipt_key or generate_private_key(SECP256R1()))
        self.audit = audit or AuditLog()
        self.clock = clock or SystemClock()
        self._references: Dict[str, str] = {}

    def capture(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        *,
        rail_id: str,
        payment_token: str,
        amount_cents: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> tuple[RailResult, Optional[PaymentReceipt]]:
        current = now or self.clock.now()
        if not self.signer.verify(decision):
            return RailResult(ok=False, rail_id=rail_id, reference="", detail="invalid_signature"), None
        rail = self.rails.get(rail_id)
        if rail is None:
            return RailResult(ok=False, rail_id=rail_id, reference="", detail="unknown_rail"), None
        amount = amount_cents or proposal.amount_cents
        result = rail.capture(proposal, decision, amount, payment_token)
        if not result.ok:
            self.ledger.release(decision.reservation_id or "")
            return result, None
        ok, msg = self.ledger.capture(decision.reservation_id or "", amount, now=current)
        if not ok:
            rail.void(decision, result.reference)
            return RailResult(ok=False, rail_id=rail_id, reference=result.reference, detail=msg), None
        self._references[decision.authorization_id] = result.reference
        receipt = self.receipts.issue(
            proposal, decision, rail_id=rail_id, rail_reference=result.reference, outcome="captured", now=current
        )
        self._audit("rail.capture", decision, result.reference)
        return result, receipt

    def refund(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        *,
        rail_id: str,
        amount_cents: int,
        now: Optional[datetime] = None,
    ) -> RailResult:
        rail = self.rails.get(rail_id)
        if rail is None:
            return RailResult(ok=False, rail_id=rail_id, reference="", detail="unknown_rail")
        reference = self._references.get(decision.authorization_id, "")
        result = rail.refund(proposal, decision, amount_cents, reference)
        if not result.ok:
            return result
        ok, msg = self.ledger.refund(decision.reservation_id or "", amount_cents, now=now or self.clock.now())
        if not ok:
            return RailResult(ok=False, rail_id=rail_id, reference=result.reference, detail=msg)
        self._audit("rail.refund", decision, result.reference)
        return result

    def void(self, decision: AuthorizationDecision, *, rail_id: str) -> RailResult:
        rail = self.rails.get(rail_id)
        if rail is None:
            return RailResult(ok=False, rail_id=rail_id, reference="", detail="unknown_rail")
        reference = self._references.get(decision.authorization_id)
        result = rail.void(decision, reference)
        if result.ok and decision.reservation_id:
            self.ledger.release(decision.reservation_id)
        return result

    def _audit(self, event_type: str, decision: AuthorizationDecision, detail: str) -> None:
        self.audit.append(
            AuditEvent(
                event_id=f"aud_{uuid.uuid4().hex[:12]}",
                event_type=event_type,
                authorization_id=decision.authorization_id,
                payment_hash=decision.payment_hash,
                actor_id="rail",
                detail=detail,
            )
        )
