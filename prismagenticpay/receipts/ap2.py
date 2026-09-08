"""AP2-style settlement receipts. Signed evidence after commit, not a network receipt service."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal


class PaymentReceipt:
    def __init__(self, token: str, claims: dict):
        self.token = token
        self.claims = claims

    @property
    def receipt_hash(self) -> str:
        return hashlib.sha256(self.token.encode("utf-8")).hexdigest()


class ReceiptIssuer:
    def __init__(self, private_key: EllipticCurvePrivateKey, issuer_id: str = "prismagenticpay"):
        self._private_key = private_key
        self.issuer_id = issuer_id

    def issue(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        *,
        rail_id: str,
        rail_reference: str,
        outcome: str,
        now: Optional[datetime] = None,
    ) -> PaymentReceipt:
        stamp = now or datetime.now(timezone.utc)
        claims = {
            "vct": "receipt.payment.1",
            "iss": self.issuer_id,
            "exp": int(stamp.timestamp()) + 365 * 24 * 3600,
            "transaction_id": proposal.transaction_id,
            "authorization_id": decision.authorization_id,
            "payment_hash": decision.payment_hash,
            "mandate_hash": proposal.mandate_hash,
            "amount_cents": proposal.amount_cents,
            "currency": proposal.currency,
            "rail_id": rail_id,
            "rail_reference": rail_reference,
            "outcome": outcome,
        }
        token = jwt.encode(claims, self._private_key, algorithm="ES256")
        return PaymentReceipt(token=token, claims=claims)

    def verify(self, token: str, public_key) -> dict:
        return jwt.decode(
            token,
            key=public_key,
            algorithms=["ES256"],
            options={"require": ["exp", "vct"]},
        )
