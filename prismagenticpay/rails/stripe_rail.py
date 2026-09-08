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
    supports_recovery = True

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.stripe.com",
        transport: httpx.BaseTransport | None = None,
        *,
        return_url: str = "",
    ):
        if not api_key:
            raise StripeRailError("STRIPE_API_KEY is required")
        if api_key.startswith("pk_"):
            raise StripeRailError("publishable keys cannot capture funds; use a secret key")
        self.api_key = api_key
        if return_url:
            from urllib.parse import urlsplit
            parsed = urlsplit(return_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise StripeRailError("return_url must be an absolute HTTP(S) URL without credentials")
        self.return_url = return_url
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
        *, operation_id: str | None = None, capture_reference: str = "",
    ) -> RailResult:
        _reject_pan(payment_token)
        operation_id = operation_id or decision.authorization_id
        if capture_reference:
            existing = self._client.get(f"/v1/payment_intents/{capture_reference}")
            existing.raise_for_status()
            body = existing.json()
            if body.get("status") == "succeeded":
                if body.get("amount_received") != amount_cents or body.get("currency") != proposal.currency.lower():
                    raise StripeRailError("captured amount or currency mismatch")
                return RailResult(ok=True, rail_id=self.rail_id, reference=capture_reference, detail="succeeded")
        created = self._client.post(
            "/v1/payment_intents",
            data={
                "amount": str(amount_cents),
                "currency": proposal.currency.lower(),
                "payment_method": payment_token,
                "confirm": "true",
                "capture_method": "manual",
                "confirmation_method": "automatic",
                "payment_method_types[0]": "card",
                **({"return_url": self.return_url} if self.return_url else {}),
                "metadata[payment_hash]": decision.payment_hash,
                "metadata[authorization_id]": decision.authorization_id,
                "metadata[operation_id]": operation_id,
            },
            headers={"Idempotency-Key": f"pi_{operation_id}"},
        )
        body = created.json()
        if created.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference="", detail="provider_request_failed")
        intent_id = body["id"]
        captured = self._client.post(
            f"/v1/payment_intents/{intent_id}/capture",
            data={"amount_to_capture": str(amount_cents)},
            headers={"Idempotency-Key": f"cap_{operation_id}"},
        )
        cap_body = captured.json()
        if captured.status_code >= 400 or cap_body.get("status") != "succeeded":
            return RailResult(ok=False, rail_id=self.rail_id, reference=intent_id, detail="capture_not_confirmed")
        return RailResult(ok=True, rail_id=self.rail_id, reference=intent_id, detail="succeeded")

    def refund(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        amount_cents: int,
        capture_reference: str,
        *, operation_id: str, refund_reference: str = "",
    ) -> RailResult:
        if refund_reference:
            existing = self._client.get(f"/v1/refunds/{refund_reference}")
            existing.raise_for_status()
            body = existing.json()
            if body.get("amount") != amount_cents or body.get("payment_intent") != capture_reference:
                raise StripeRailError("refund binding mismatch")
            return RailResult(ok=body.get("status") == "succeeded", rail_id=self.rail_id,
                              reference=refund_reference, detail=body.get("status", "unknown"))
        response = self._client.post(
            "/v1/refunds",
            data={
                "payment_intent": capture_reference,
                "metadata[operation_id]": operation_id,
                "amount": str(amount_cents),
                "metadata[payment_hash]": decision.payment_hash,
            },
            headers={"Idempotency-Key": f"re_{operation_id}"},
        )
        body = response.json()
        if response.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference="", detail="provider_request_failed")
        return RailResult(ok=body.get("status") == "succeeded", rail_id=self.rail_id, reference=body.get("id", ""), detail=body.get("status", "unknown"))

    def reconcile(self, proposal, decision, amount_cents, *, operation_id, kind, reference, capture_reference=""):
        import re
        prefix = "pi" if kind == "capture" else "re"
        if not re.fullmatch(prefix + r"_[A-Za-z0-9]+", reference):
            raise StripeRailError("invalid provider reference")
        resource = "payment_intents" if kind == "capture" else "refunds"
        response = self._client.get(f"/v1/{resource}/{reference}")
        response.raise_for_status()
        body = response.json()
        metadata = body.get("metadata", {})
        if (metadata.get("operation_id") != operation_id or metadata.get("payment_hash") != decision.payment_hash
                or body.get("amount") != amount_cents or body.get("currency") != proposal.currency.lower()):
            raise StripeRailError("provider evidence does not match operation")
        if kind == "refund" and body.get("payment_intent") != capture_reference:
            raise StripeRailError("provider refund does not match capture")
        status = body.get("status", "unknown")
        if status == "succeeded":
            if kind == "capture" and body.get("amount_received") != amount_cents:
                raise StripeRailError("provider captured amount mismatch")
            return RailResult(ok=True, rail_id=self.rail_id, reference=reference, detail="succeeded")
        terminal = status == "canceled" or (kind == "refund" and status == "failed")
        return RailResult(ok=False, rail_id=self.rail_id, reference=reference,
                          detail="confirmed_not_paid" if terminal else "provider_still_pending")

    def void(self, decision: AuthorizationDecision, capture_reference: Optional[str] = None) -> RailResult:
        if not capture_reference:
            return RailResult(ok=True, rail_id=self.rail_id, reference="", detail="no_remote_intent")
        response = self._client.post(f"/v1/payment_intents/{capture_reference}/cancel")
        body = response.json()
        if response.status_code >= 400:
            return RailResult(ok=False, rail_id=self.rail_id, reference="", detail="provider_request_failed")
        return RailResult(ok=True, rail_id=self.rail_id, reference=capture_reference, detail=body.get("status", ""))


def _reject_pan(token: str) -> None:
    import re
    if not re.fullmatch(r"pm_[A-Za-z0-9_]+", token):
        raise StripeRailError("only Stripe payment_method tokens are permitted")
