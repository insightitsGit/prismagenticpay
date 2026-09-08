"""Stripe rail. Uses PaymentIntents with a caller-supplied payment_method token.

Raw card numbers are rejected. PCI card data stays in Stripe.
"""

from __future__ import annotations

from typing import Optional

import httpx

from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal
from prismagenticpay.rails.base import RailResult


class StripeRailError(ValueError):
    pass


class StripeRail:
    rail_id = "stripe"

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.stripe.com",
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise StripeRailError("STRIPE_API_KEY is required")
        if api_key.startswith("pk_"):
            raise StripeRailError("publishable keys cannot capture funds; use a secret key")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self._client = httpx.Client(
            base_url=self.api_base,
            timeout=20.0,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def capture(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        payment_token: str,
    ) -> RailResult:
        _reject_pan(payment_token)
        created = self._client.post(
            "/v1/payment_intents",
            data={
                "amount": str(amount_cents),
                "currency": proposal.currency.lower(),
                "payment_method": payment_token,
                "confirm": "true",
                "capture_method": "manual",
                "confirmation_method": "automatic",
                "metadata[payment_hash]": decision.payment_hash,
                "metadata[authorization_id]": decision.authorization_id,
                "idempotency_key": decision.authorization_id,
            },
            headers={"Idempotency-Key": f"pi_{decision.authorization_id}"},
        )
        body = created.json()
        if created.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference="", detail=str(body))
        intent_id = body["id"]
        captured = self._client.post(
            f"/v1/payment_intents/{intent_id}/capture",
            data={"amount_to_capture": str(amount_cents)},
        )
        cap_body = captured.json()
        if captured.status_code >= 400 or cap_body.get("status") != "succeeded":
            return RailResult(ok=False, rail_id=self.rail_id, reference=intent_id, detail=str(cap_body))
        return RailResult(ok=True, rail_id=self.rail_id, reference=intent_id, detail="succeeded")

    def refund(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        capture_reference: str,
    ) -> RailResult:
        response = self._client.post(
            "/v1/refunds",
            data={
                "payment_intent": capture_reference,
                "amount": str(amount_cents),
                "metadata[payment_hash]": decision.payment_hash,
            },
            headers={"Idempotency-Key": f"re_{decision.authorization_id}_{amount_cents}"},
        )
        body = response.json()
        if response.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference=capture_reference, detail=str(body))
        return RailResult(ok=True, rail_id=self.rail_id, reference=body.get("id", ""), detail=body.get("status", ""))

    def void(self, decision: AuthorizationDecision, capture_reference: Optional[str] = None) -> RailResult:
        if not capture_reference:
            return RailResult(ok=True, rail_id=self.rail_id, reference="", detail="no_remote_intent")
        response = self._client.post(f"/v1/payment_intents/{capture_reference}/cancel")
        body = response.json()
        if response.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference=capture_reference, detail=str(body))
        return RailResult(ok=True, rail_id=self.rail_id, reference=capture_reference, detail=body.get("status", ""))


def _reject_pan(token: str) -> None:
    digits = "".join(ch for ch in token if ch.isdigit())
    if token.isdigit() and 13 <= len(digits) <= 19:
        raise StripeRailError("raw PAN is not permitted; pass a Stripe payment_method token")
