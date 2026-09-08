"""Coinbase Commerce rail. Charges use API keys; no wallet seed is stored here."""

from __future__ import annotations

from typing import Optional

import httpx

from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal
from prismagenticpay.rails.base import RailResult


class CoinbaseRailError(ValueError):
    pass


class CoinbaseRail:
    rail_id = "coinbase"

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.commerce.coinbase.com",
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise CoinbaseRailError("COINBASE_COMMERCE_API_KEY is required")
        self._client = httpx.Client(
            base_url=api_base.rstrip("/"),
            timeout=20.0,
            transport=transport,
            headers={
                "X-CC-Api-Key": api_key,
                "X-CC-Version": "2018-03-22",
                "Content-Type": "application/json",
            },
        )

    def capture(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        payment_token: str,
    ) -> RailResult:
        del payment_token
        response = self._client.post(
            "/charges",
            json={
                "name": proposal.merchant.merchant_name,
                "description": decision.authorization_id,
                "pricing_type": "fixed_price",
                "local_price": {
                    "amount": f"{amount_cents / 100:.2f}",
                    "currency": proposal.currency,
                },
                "metadata": {"payment_hash": decision.payment_hash},
            },
        )
        body = response.json()
        if response.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference="", detail=str(body))
        charge_id = body.get("data", {}).get("id", "")
        return RailResult(ok=True, rail_id=self.rail_id, reference=charge_id, detail="charge_created")

    def refund(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        capture_reference: str,
    ) -> RailResult:
        del proposal, amount_cents
        return RailResult(
            ok=False,
            rail_id=self.rail_id,
            reference=capture_reference,
            detail="Coinbase Commerce has no charge-refund API; process refunds in the merchant dashboard",
        )

    def void(self, decision: AuthorizationDecision, capture_reference: Optional[str] = None) -> RailResult:
        del decision
        if not capture_reference:
            return RailResult(ok=True, rail_id=self.rail_id, reference="", detail="no_remote_charge")
        return RailResult(
            ok=False,
            rail_id=self.rail_id,
            reference=capture_reference,
            detail="Coinbase Commerce charges cannot be voided over the API",
        )
