from conftest import fresh_metadata, write_identities
"""Real loopback HTTP, cryptography, policy engine and SQLite; simulated provider."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from prismagenticpay.runtime import create_production_app
import socket
import threading
import time
from urllib.parse import parse_qs

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
import uvicorn

from prismagenticpay.adapters.ap2 import AP2IngressAdapter, CLOSED_PAYMENT_VCT
from prismagenticpay.api.app import create_app
from prismagenticpay.config import Settings
from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.lifecycle.orchestrator import RailOrchestrator
from prismagenticpay.policies.corporate import default_corporate_policies
from prismagenticpay.rails.stripe_rail import StripeRail
from prismagenticpay.state.sqlite_ledger import SqliteAuthorityLedger


@contextmanager
def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("local HTTP server did not start")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_local_purchase_review_capture_refund_restart(tmp_path, authority, signer):
    # A software agent buys a $25 seat; finance approves; merchant captures
    # $20 and returns $5. Every rail call travels over loopback HTTP.
    provider = FastAPI()
    calls = []

    @provider.post("/v1/payment_intents")
    async def create_intent(request: Request):
        body = parse_qs((await request.body()).decode())
        assert body["amount"] == ["2000"]
        assert body["payment_method"] == ["pm_local_scenario"]
        assert request.headers["idempotency-key"].startswith("pi_capture_")
        calls.append("create")
        return {"id": "pi_local", "status": "requires_capture"}

    @provider.post("/v1/payment_intents/pi_local/capture")
    async def capture_intent(request: Request):
        assert parse_qs((await request.body()).decode())["amount_to_capture"] == ["2000"]
        calls.append("capture")
        return {"id": "pi_local", "status": "succeeded"}

    @provider.post("/v1/refunds")
    async def refund_intent(request: Request):
        body = parse_qs((await request.body()).decode())
        assert body["payment_intent"] == ["pi_local"]
        assert body["amount"] == ["500"]
        calls.append("refund")
        return {"id": "re_local", "status": "succeeded"}

    now = datetime.now(timezone.utc)
    payment_key = ec.generate_private_key(ec.SECP256R1())
    checkout_key = ec.generate_private_key(ec.SECP256R1())
    checkout = jwt.encode({"exp": int(now.timestamp()) + 600, "cart": "one-software-seat"}, checkout_key, algorithm="ES256")
    token = jwt.encode({
        "vct": CLOSED_PAYMENT_VCT, "exp": int(now.timestamp()) + 600,
        "checkout_hash": hashlib.sha256(checkout.encode()).hexdigest(),
        "transaction_id": "local-purchase", "amount_cents": 2500, "currency": "USD",
        "merchant_id": "software-vendor", "merchant_name": "Local Software Vendor",
        "mcc": "5734", "domain": "vendor.example", "category": "software",
        "agent_id": "purchasing-agent", "principal_id": "employee", "mandate_id": "seat-mandate",
        "nonce": "local-seat-1", "items": [{"sku": "seat", "name": "Software seat", "amount_cents": 2500}],
    }, payment_key, algorithm="ES256")
    # The client computes the projection too, solely to submit the exact capture cart.
    proposal = AP2IngressAdapter(payment_public_key=payment_key.public_key(), checkout_public_key=checkout_key.public_key()).verify_and_project(token, checkout)
    path = str(tmp_path / "scenario.sqlite")
    identity_path = write_identities(tmp_path / "identities.json")
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps({"issuers": [
        {"issuer_id": "payment", "jwk": json.loads(jwt.algorithms.ECAlgorithm.to_jwk(payment_key.public_key())), "purposes": ["payment"]},
        {"issuer_id": "checkout", "jwk": json.loads(jwt.algorithms.ECAlgorithm.to_jwk(checkout_key.public_key())), "purposes": ["checkout"]},
    ]}))
    grant_path = tmp_path / "authority.json"
    grant_path.write_text(json.dumps({"grants": [{"principal_id": "employee", "agent_id": "purchasing-agent", "mandate_id": "seat-mandate",
        "authority": authority.model_copy(update={"requires_dual_signature": True, "snapshot_taken_at": now}).model_dump(mode="json")}]}))
    with serve(provider) as provider_url:
        settings = Settings(environment="production", database_url="sqlite:///" + path,
            signing_seed_hex=signer.private_bytes().hex(), identity_registry_path=identity_path,
            trust_registry_path=str(trust_path), authority_registry_path=str(grant_path),
            stripe_api_key="sk_test_local_only", stripe_api_base=provider_url,
            session_budget_cents=10000, daily_budget_cents=20000, mandate_budget_cents=15000)
        app = create_production_app(settings)
        authorizer = app.state.authorizer
        ledger = authorizer.ledger
        orchestrator = app.state.orchestrator
        rail = orchestrator.rails["stripe"]
        with serve(app) as url, httpx.Client(base_url=url, headers={"X-API-Key": "local-scenario-key"}, trust_env=False) as client:
            assert client.get("/healthz").status_code == 200
            assert httpx.get(url + "/v1/audit", trust_env=False).status_code == 401
            payload = {"proposal": proposal.model_dump(mode="json"), "authority": authority.model_copy(update={"requires_dual_signature": True}).model_dump(mode="json")}
            assert client.post("/v1/authorize", json=payload).status_code == 403
            response = client.post("/v1/authorize/ap2", json={"payment_token": token, "checkout_jwt": checkout,
                "payment_issuer_id": "payment", "checkout_issuer_id": "checkout"})
            assert response.status_code == 200, response.text
            review = response.json()
            assert review["status"] == "REVIEW"
            capture = {"proposal": payload["proposal"], "authorization": review, "rail_id": "stripe", "payment_token": "pm_local_scenario", "amount_cents": 2000}
            assert client.post("/v1/capture", json=capture).status_code == 409
            assert calls == []
            route = f"/v1/reviews/{review['review_case_id']}/approve"
            assert client.post(route, json={"approver_id": "finance-controller"}).status_code == 403
            assert client.post(route, json={"approver_id": "finance-controller"}, headers={"X-API-Key": "employee-review-key"}).status_code == 409
            approved = client.post(route, json={}, headers={"X-API-Key": "controller-key"})
            assert approved.status_code == 200, approved.text
            capture["authorization"] = approved.json()
            captured = client.post("/v1/capture", json=capture)
            assert captured.status_code == 200, captured.text
            receipt = orchestrator.receipts.verify(captured.json()["receipt"], orchestrator.receipts._private_key.public_key())
            assert receipt["payment_hash"] == proposal.compute_canonical_payment_hash()
            assert receipt["amount_cents"] == 2000
            assert client.post("/v1/capture", json=capture).status_code == 409
            refund = {"operation_id": "refund-local-1", "proposal": payload["proposal"], "authorization": approved.json(), "rail_id": "stripe", "amount_cents": 2500}
            assert client.post("/v1/refund", json=refund).status_code == 409
            refund["amount_cents"] = 500
            refunded = client.post("/v1/refund", json=refund)
            assert refunded.status_code == 200, refunded.text
            assert calls == ["create", "capture", "refund"]
            events = client.get("/v1/audit").json()
            assert {"authorize.review", "review.approved", "rail.capture", "rail.refund"} <= {event["event_type"] for event in events}
        rail._client.close()
        ledger.close()
        restarted_app = create_production_app(settings)
        with serve(restarted_app) as restarted_url, httpx.Client(base_url=restarted_url, headers={"X-API-Key": "local-scenario-key"}, trust_env=False) as client:
            assert client.post("/v1/refund", json=refund).status_code == 200
            assert calls == ["create", "capture", "refund"]
            assert len(client.get("/v1/audit").json()) == len(events)
            restarted_receipts = restarted_app.state.orchestrator.receipts
            assert restarted_receipts.verify(captured.json()["receipt"], restarted_receipts._private_key.public_key())["amount_cents"] == 2000
        restarted_app.state.orchestrator.rails["stripe"]._client.close()
        restarted_app.state.authorizer.ledger.close()
    reloaded = SqliteAuthorityLedger(path, 10000, 20000, 15000)
    hold = reloaded.get(approved.json()["reservation_id"])
    assert hold.captured_cents == 2000 and hold.refunded_cents == 500
    available = reloaded.inspect_available(session_id="session:employee", principal_id="employee", mandate_id="seat-mandate")
    assert available.session_remaining_cents == 8500
    restarted = PrismPaymentAuthorizer(reloaded, default_corporate_policies(), signer=signer)
    assert restarted.process_authorization(proposal, authority, fact_metadata=fresh_metadata(None),).status.value == "REFUSED"
    reloaded._conn.close()
