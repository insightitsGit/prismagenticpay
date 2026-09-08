from __future__ import annotations

from conftest import fresh_metadata

from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from prismagenticpay.api.app import create_app
from prismagenticpay.config import Settings
from prismagenticpay.connectors.sap import SapConnector
from prismagenticpay.domain.models import PaymentProposal
from prismagenticpay.fx.provider import FxQuote, LockedRateBook
from prismagenticpay.lifecycle.orchestrator import RailOrchestrator
from prismagenticpay.rails.iso8583 import Iso8583Error, authorization_fields, pack, unpack
from prismagenticpay.rails.stripe_rail import StripeRail, StripeRailError
from prismagenticpay.receipts.ap2 import ReceiptIssuer
from prismagenticpay.state.ledger import HoldStatus


def test_partial_capture_and_refund(ledger, frozen_now):
    ok, res_id, _, _ = ledger.reserve("h-cap", 1000, now=frozen_now, session_id="s", principal_id="p", mandate_id="m")
    assert ok
    captured, msg = ledger.capture(res_id, 400, now=frozen_now)
    assert captured and msg == "SETTLED"
    hold = ledger.get(res_id)
    assert hold.captured_cents == 400
    refunded, _ = ledger.refund(res_id, 150, now=frozen_now)
    assert refunded
    assert ledger.get(res_id).refunded_cents == 150


def test_fx_locked_quote_converts_and_expires():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    quote = FxQuote(
        quote_id="fx1",
        base_currency="EUR",
        quote_currency="USD",
        rate="1.10",
        source="ecb",
        as_of=now,
        ttl_seconds=30,
    )
    book = LockedRateBook()
    book.put(quote)
    assert book.convert_to_home(1000, "EUR", "USD", quote, now=now) == 1100
    with pytest.raises(Exception):
        quote.convert_cents(1000, now=now.replace(year=2027))


def test_iso8583_roundtrip_rejects_pan():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    proposal = PaymentProposal(
        transaction_id="txiso",
        amount_cents=1299,
        merchant={"merchant_id": "m", "merchant_name": "M", "mcc": "5734", "domain": "m.example"},
        category="software",
        agent_id="a",
        principal_id="p",
        mandate_id="mid",
        mandate_type="closed_payment",
        mandate_hash="b" * 64,
        idempotency_nonce="n",
    )
    with pytest.raises(Iso8583Error, match="PAN"):
        authorization_fields(proposal, "1", now=now, payment_token="4111111111111111")
    fields = authorization_fields(proposal, "42", now=now, payment_token="tok_switch_1")
    packed = pack("0100", fields)
    mti, parsed = unpack(packed)
    assert mti == "0100"
    assert parsed[4] == f"{1299:012d}"
    response = pack("0110", {**fields, 39: "00"})
    rmti, rfields = unpack(response)
    assert rmti == "0110" and rfields[39] == "00"


def test_stripe_rail_uses_http_and_rejects_pan(proposal, authorizer, authority, frozen_now):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/payment_intents":
            return httpx.Response(200, json={"id": "pi_123", "status": "requires_capture"})
        if request.url.path.endswith("/capture"):
            return httpx.Response(200, json={"id": "pi_123", "status": "succeeded"})
        return httpx.Response(404, json={"error": "missing"})

    rail = StripeRail("sk_test_123", transport=httpx.MockTransport(handler))
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    result = rail.capture(proposal, decision, 2500, "pm_card_visa")
    assert result.ok and result.reference == "pi_123"
    with pytest.raises(StripeRailError):
        rail.capture(proposal, decision, 2500, "4111111111111111")


def test_sap_connector_maps_odata(proposal):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"d": {"VendorStatus": "approved", "BusinessPartnerGrouping": "standard", "DualControlRequired": False}},
        )

    connector = SapConnector("https://sap.example", "token", transport=httpx.MockTransport(handler))
    bundle = connector.fetch(proposal)
    assert bundle.fresh()["vendor_status"].value == "approved"


def test_receipt_roundtrip(proposal, authorizer, authority, frozen_now):
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = ReceiptIssuer(key)
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    receipt = issuer.issue(proposal, decision, rail_id="stripe", rail_reference="pi_1", outcome="captured")
    claims = issuer.verify(receipt.token, key.public_key())
    assert claims["payment_hash"] == decision.payment_hash


def test_api_key_required_in_production(authorizer, gateway, proposal, authority, tmp_path):
    from conftest import write_identities
    settings = Settings(environment="production", identity_registry_path=write_identities(tmp_path / "identities.json"))
    client = TestClient(create_app(authorizer, gateway, settings=settings))
    assert client.get("/v1/audit").status_code == 401
    assert client.get("/v1/audit", headers={"X-API-Key": "local-scenario-key"}).status_code == 200
    assert client.get("/console").status_code == 401
    assert client.post("/v1/authorize", headers={"X-API-Key": "local-scenario-key"},
        json={"proposal": proposal.model_dump(mode="json"), "authority": authority.model_dump(mode="json")}).status_code == 403


def test_console_and_health(authorizer, gateway):
    client = TestClient(create_app(authorizer, gateway))
    assert client.get("/healthz").json()["status"] == "ok"
    page = client.get("/console")
    assert page.status_code == 200
    assert "PrismAgenticPay Console" in page.text
