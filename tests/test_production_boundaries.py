import hashlib
import json
from datetime import datetime, timezone, timedelta
import jwt
import pytest
import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from conftest import write_identities, fresh_metadata
from prismagenticpay.config import Settings
from prismagenticpay.runtime import create_production_app, build_rails
from prismagenticpay.rails.stripe_rail import StripeRail, StripeRailError


@pytest.fixture
def production(tmp_path, authority, signer):
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    trust = tmp_path / "trust.json"
    trust.write_text(json.dumps({"issuers": [{"issuer_id": "trusted", "purposes": ["payment", "checkout"],
        "jwk": json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))}]}))
    grant = tmp_path / "authority.json"
    grant.write_text(json.dumps({"grants": [{"principal_id": "employee", "agent_id": "purchasing-agent", "mandate_id": "mandate",
        "authority": authority.model_copy(update={"snapshot_taken_at": now}).model_dump(mode="json")}]}))
    settings = Settings(environment="production", stripe_api_key="sk_test_never_used", stripe_api_base="http://127.0.0.1:1", database_url="sqlite:///" + str(tmp_path / "app.sqlite"),
        signing_seed_hex=signer.private_bytes().hex(), identity_registry_path=write_identities(tmp_path / "ids.json"),
        authority_registry_path=str(grant), trust_registry_path=str(trust))
    app = create_production_app(settings)
    checkout = jwt.encode({"exp": int(now.timestamp()) + 300}, key, algorithm="ES256")
    claims = dict(vct="mandate.payment.1", exp=int(now.timestamp()) + 300,
        checkout_hash=hashlib.sha256(checkout.encode()).hexdigest(), transaction_id="tx", amount_cents=2500,
        currency="USD", merchant_id="vendor", merchant_name="Vendor", mcc="5734", domain="vendor.example",
        category="software", agent_id="purchasing-agent", principal_id="employee", mandate_id="mandate", nonce="nonce")
    def request(**updates):
        return dict(payment_token=jwt.encode({**claims, **updates}, key, algorithm="ES256"), checkout_jwt=checkout,
                    payment_issuer_id="trusted", checkout_issuer_id="trusted")
    client = TestClient(app, headers={"X-API-Key": "local-scenario-key"})
    yield app, client, request, settings
    app.state.orchestrator.rails["stripe"]._client.close()
    app.state.authorizer.ledger.close()


@pytest.mark.parametrize("attack", ["wrong_key", "scope", "untrusted", "checkout", "expired", "currency"])
def test_verified_ingress_rejects_untrusted_requests(production, attack):
    app, client, request, settings = production
    payload = request()
    if attack == "wrong_key":
        other = ec.generate_private_key(ec.SECP256R1())
        claims = jwt.decode(payload["payment_token"], options={"verify_signature": False})
        payload["payment_token"] = jwt.encode(claims, other, algorithm="ES256")
    if attack == "scope": payload = request(principal_id="another-employee")
    if attack == "untrusted": payload["payment_issuer_id"] = "unknown"
    if attack == "checkout": payload["checkout_jwt"] = payload["payment_token"]
    if attack == "expired": payload = request(exp=1)
    if attack == "currency": payload = request(currency="EUR")
    result = client.post("/v1/authorize/ap2", json=payload)
    assert result.status_code in {403, 422}, result.text
    assert app.state.authorizer.ledger.snapshot()["reservations"] == {}


def test_server_owned_stale_authority_stalls_and_can_resume(production):
    app, client, request, settings = production
    from pathlib import Path
    path = Path(settings.authority_registry_path)
    grants = json.loads(path.read_text())
    grants["grants"][0]["authority"]["snapshot_taken_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(grants))
    response = client.post("/v1/authorize/ap2", json=request())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "STALLED"
    grants["grants"][0]["authority"]["snapshot_taken_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(grants))
    resumed = client.post("/v1/gathers/" + response.json()["gather_case_id"] + "/resume", json={})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "AUTHORIZED"


def test_signed_authorization_does_not_outlive_mandate(production):
    app, client, request, settings = production
    exp = int(datetime.now(timezone.utc).timestamp()) + 10
    response = client.post("/v1/authorize/ap2", json=request(exp=exp))
    assert response.status_code == 200, response.text
    assert datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00")).timestamp() <= exp


def test_policy_roles_and_persistence(production):
    app, client, request, settings = production
    rules = client.get("/v1/policies").json()
    assert client.delete("/v1/policies/" + rules[0]["id"]).status_code == 403
    result = client.delete("/v1/policies/" + rules[0]["id"], headers={"X-API-Key": "controller-key"})
    assert result.status_code == 200
    second = create_production_app(settings)
    try:
        assert len(second.state.authorizer.evaluator.policies) == len(rules) - 1
    finally:
        second.state.orchestrator.rails["stripe"]._client.close()
        second.state.authorizer.ledger.close()


def test_readiness_detects_storage_failure(production, monkeypatch):
    app, client, _, _ = production
    def broken(_): raise OSError("disk full")
    monkeypatch.setattr(app.state.authorizer.ledger, "_persist", broken)
    assert client.get("/readyz").status_code == 503


@pytest.mark.parametrize("rail", ["coinbase", "iso8583", "test"])
def test_incomplete_production_rails_fail_closed(rail):
    with pytest.raises(RuntimeError, match="only Stripe"):
        build_rails(Settings(allowed_rails=[rail]))


def test_reconciliation_checks_provider_evidence(proposal, authorizer, authority, frozen_now):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    body = dict(id="pi_remote", amount=2500, amount_received=2500, currency="usd", status="succeeded",
                metadata={"payment_hash": decision.payment_hash, "operation_id": "operation-one"})
    rail = StripeRail("sk_test", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    assert rail.reconcile(proposal, decision, 2500, operation_id="operation-one", kind="capture", reference="pi_remote").ok
    body["metadata"]["operation_id"] = "another-operation"
    with pytest.raises(StripeRailError, match="match"):
        rail.reconcile(proposal, decision, 2500, operation_id="operation-one", kind="capture", reference="pi_remote")
    rail._client.close()


def test_stripe_refund_pending_is_not_success(proposal, authorizer, authority, frozen_now):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    keys = []
    def handler(request):
        keys.append(request.headers["idempotency-key"])
        return httpx.Response(200, json={"id": "re_pending", "status": "pending"})
    rail = StripeRail("sk_test", transport=httpx.MockTransport(handler))
    assert not rail.refund(proposal, decision, 100, "pi_test", operation_id="refund-one").ok
    assert not rail.refund(proposal, decision, 100, "pi_test", operation_id="refund-two").ok
    assert keys == ["re_refund-one", "re_refund-two"]
    rail._client.close()


def test_existing_database_rejects_signing_key_change(production):
    app, client, request, settings = production
    with pytest.raises(RuntimeError, match="key differs"):
        create_production_app(settings.model_copy(update={"signing_seed_hex": "ab" * 32}))
    assert client.get("/readyz").status_code == 200


def test_enabled_stripe_requires_key():
    with pytest.raises(RuntimeError, match="STRIPE_API_KEY"):
        create_production_app(Settings(environment="production", signing_seed_hex="ab" * 32))
