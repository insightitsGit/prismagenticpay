"""Opt-in real Stripe sandbox contract test. Never accepts a live secret key."""
import os
import uuid
from datetime import datetime, timezone
import pytest
from conftest import fresh_metadata
from prismagenticpay.rails.stripe_rail import StripeRail


@pytest.mark.skipif(os.getenv("PAP_RUN_STRIPE_SANDBOX") != "1", reason="Stripe sandbox opt-in and test key required")
def test_real_stripe_sandbox_capture_and_refund(authorizer, proposal, authority):
    key = os.environ.get("STRIPE_API_KEY", "")
    assert key.startswith("sk_test_"), "Only an sk_test_ sandbox key is permitted"
    now = datetime.now(timezone.utc)
    proposal = proposal.model_copy(update={"idempotency_nonce": uuid.uuid4().hex, "amount_cents": 100})
    decision = authorizer.process_authorization(proposal, authority, now=now, fact_metadata=fresh_metadata(now))
    assert decision.status.value == "AUTHORIZED"
    rail = StripeRail(key, return_url=os.getenv("STRIPE_RETURN_URL", ""))
    operation = "sandbox_" + uuid.uuid4().hex
    try:
        captured = rail.capture(proposal, decision, 100, "pm_card_visa", operation_id=operation)
        assert captured.ok, captured.detail
        evidence = rail.reconcile(proposal, decision, 100, operation_id=operation, kind="capture", reference=captured.reference)
        assert evidence.ok
        refund_id = "sandbox_refund_" + uuid.uuid4().hex
        refunded = rail.refund(proposal, decision, 100, captured.reference, operation_id=refund_id)
        assert refunded.ok, refunded.detail
        assert rail.reconcile(proposal, decision, 100, operation_id=refund_id, kind="refund",
            reference=refunded.reference, capture_reference=captured.reference).ok
    finally:
        rail._client.close()
