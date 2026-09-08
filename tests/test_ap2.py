from __future__ import annotations

from conftest import fresh_metadata

import base64
import hashlib
import json
from datetime import datetime, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from prismagenticpay.adapters.ap2 import (
    AP2IngressAdapter,
    AP2VerificationError,
    CLOSED_PAYMENT_VCT,
    OPEN_PAYMENT_VCT,
)


def _es256_key():
    return ec.generate_private_key(ec.SECP256R1())


def _sign(private_key, claims: dict) -> str:
    return jwt.encode(claims, private_key, algorithm="ES256")


def test_closed_payment_mandate_projects_to_proposal():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    payment_key = _es256_key()
    checkout_key = _es256_key()
    checkout_jwt = _sign(checkout_key, {"exp": int(now.timestamp()) + 3600, "cart": "abc"})
    claims = {
        "vct": CLOSED_PAYMENT_VCT,
        "exp": int(now.timestamp()) + 3600,
        "checkout_hash": hashlib.sha256(checkout_jwt.encode("utf-8")).hexdigest(),
        "transaction_id": "tx_ap2",
        "amount_cents": 1999,
        "currency": "USD",
        "merchant_id": "merch_1",
        "merchant_name": "Books",
        "mcc": "5942",
        "domain": "books.example",
        "category": "books",
        "agent_id": "agent_1",
        "principal_id": "user_1",
        "mandate_id": "man_1",
        "nonce": "nonce_ap2",
    }
    payment_jwt = _sign(payment_key, claims)
    adapter = AP2IngressAdapter(
        payment_public_key=payment_key.public_key(),
        checkout_public_key=checkout_key.public_key(),
    )
    proposal = adapter.verify_and_project(payment_jwt, checkout_jwt, now=now)
    assert proposal.amount_cents == 1999
    assert proposal.mandate_type == "closed_payment"
    assert proposal.mandate_hash == hashlib.sha256(payment_jwt.encode("utf-8")).hexdigest()
    assert proposal.compute_canonical_payment_hash()


def test_tampered_signature_is_rejected():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    signer = _es256_key()
    other = _es256_key()
    checkout = _sign(signer, {"exp": int(now.timestamp()) + 60})
    payment = _sign(
        signer,
        {
            "vct": CLOSED_PAYMENT_VCT,
            "exp": int(now.timestamp()) + 60,
            "checkout_hash": hashlib.sha256(checkout.encode("utf-8")).hexdigest(),
            "transaction_id": "tx",
            "amount_cents": 100,
            "currency": "USD",
            "merchant_id": "m",
            "merchant_name": "M",
            "mcc": "5734",
            "domain": "m.example",
            "category": "software",
            "agent_id": "a",
            "principal_id": "p",
            "mandate_id": "mid",
            "nonce": "n",
        },
    )
    adapter = AP2IngressAdapter(payment_public_key=other.public_key())
    with pytest.raises(AP2VerificationError, match="signature failed"):
        adapter.verify(payment, checkout, now=now)


def test_checkout_hash_mismatch_is_rejected():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    key = _es256_key()
    checkout = _sign(key, {"exp": int(now.timestamp()) + 60})
    other_checkout = _sign(key, {"exp": int(now.timestamp()) + 60, "other": True})
    payment = _sign(
        key,
        {
            "vct": CLOSED_PAYMENT_VCT,
            "exp": int(now.timestamp()) + 60,
            "checkout_hash": hashlib.sha256(other_checkout.encode("utf-8")).hexdigest(),
            "transaction_id": "tx",
            "amount_cents": 100,
            "currency": "USD",
            "merchant_id": "m",
            "merchant_name": "M",
            "mcc": "5734",
            "domain": "m.example",
            "category": "software",
            "agent_id": "a",
            "principal_id": "p",
            "mandate_id": "mid",
            "nonce": "n",
        },
    )
    adapter = AP2IngressAdapter(payment_public_key=key.public_key())
    with pytest.raises(AP2VerificationError, match="checkout_hash"):
        adapter.verify(payment, checkout, now=now)


def test_verified_mandate_can_authorize_and_settle(authorizer, gateway, authority, frozen_now):
    payment_key = _es256_key()
    checkout_key = _es256_key()
    checkout_jwt = _sign(checkout_key, {"exp": int(frozen_now.timestamp()) + 3600, "cart": "closed"})
    claims = {
        "vct": CLOSED_PAYMENT_VCT,
        "exp": int(frozen_now.timestamp()) + 3600,
        "checkout_hash": hashlib.sha256(checkout_jwt.encode("utf-8")).hexdigest(),
        "transaction_id": "tx_ap2_settle",
        "amount_cents": 1500,
        "currency": "USD",
        "merchant_id": "merch_cloud",
        "merchant_name": "Example Cloud",
        "mcc": "5734",
        "domain": "cloud.example",
        "category": "software",
        "agent_id": "agent_shopper",
        "principal_id": "principal_42",
        "mandate_id": "mandate_42",
        "nonce": "nonce_ap2_settle",
    }
    payment_jwt = _sign(payment_key, claims)
    adapter = AP2IngressAdapter(
        payment_public_key=payment_key.public_key(),
        checkout_public_key=checkout_key.public_key(),
    )
    proposal = adapter.verify_and_project(payment_jwt, checkout_jwt, now=frozen_now)
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert decision.status.value == "AUTHORIZED"
    assert gateway.settle_transaction(
        decision, proposal.compute_canonical_payment_hash(), now=frozen_now
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base_claims(now: datetime, checkout_jwt: str, **extra) -> dict:
    claims = {
        "vct": CLOSED_PAYMENT_VCT,
        "exp": int(now.timestamp()) + 60,
        "checkout_hash": hashlib.sha256(checkout_jwt.encode("utf-8")).hexdigest(),
        "transaction_id": "tx",
        "amount_cents": 100,
        "currency": "USD",
        "merchant_id": "m",
        "merchant_name": "M",
        "mcc": "5734",
        "domain": "m.example",
        "category": "software",
        "agent_id": "a",
        "principal_id": "p",
        "mandate_id": "mid",
        "nonce": "n",
    }
    claims.update(extra)
    return claims


def test_cnf_must_match_trusted_payment_key():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    key = _es256_key()
    other = _es256_key()
    checkout = _sign(key, {"exp": int(now.timestamp()) + 60})
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(other.public_key()))
    payment = _sign(key, _base_claims(now, checkout, cnf={"jwk": jwk}))
    adapter = AP2IngressAdapter(payment_public_key=key.public_key(), require_agent_binding=True)
    with pytest.raises(AP2VerificationError, match="cnf.jwk"):
        adapter.verify(payment, checkout, now=now)


def test_cnf_matching_trusted_key_is_accepted():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    key = _es256_key()
    checkout = _sign(key, {"exp": int(now.timestamp()) + 60})
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    payment = _sign(key, _base_claims(now, checkout, cnf={"jwk": jwk}))
    adapter = AP2IngressAdapter(payment_public_key=key.public_key(), require_agent_binding=True)
    claims = adapter.verify(payment, checkout, now=now)
    assert claims.cnf is not None


def test_open_mandate_rejects_over_budget():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    key = _es256_key()
    checkout = _sign(key, {"exp": int(now.timestamp()) + 60})
    payment = _sign(key, _base_claims(now, checkout, amount_cents=5000))
    open_jwt = _sign(
        key,
        {
            "vct": OPEN_PAYMENT_VCT,
            "exp": int(now.timestamp()) + 60,
            "max_amount_cents": 1000,
            "categories": ["software"],
        },
    )
    adapter = AP2IngressAdapter(payment_public_key=key.public_key())
    with pytest.raises(AP2VerificationError, match="max_amount"):
        adapter.verify(payment, checkout, now=now, open_mandate_jwt=open_jwt)


def test_sd_jwt_disclosure_is_required_and_bound():
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    key = _es256_key()
    checkout = _sign(key, {"exp": int(now.timestamp()) + 60})
    disclosure = _b64url(json.dumps(["salt1", "category", "software"], separators=(",", ":")).encode())
    digest = _b64url(hashlib.sha256(disclosure.encode("ascii")).digest())
    claims = _base_claims(now, checkout)
    del claims["category"]
    claims["_sd"] = [digest]
    issuer = _sign(key, claims)
    token = f"{issuer}~{disclosure}~"
    adapter = AP2IngressAdapter(payment_public_key=key.public_key())
    verified = adapter.verify(token, checkout, now=now)
    assert verified.category == "software"


@pytest.mark.parametrize("attack", ["duplicate", "collision", "algorithm"])
def test_sd_jwt_rejects_ambiguous_disclosures(attack):
    now = datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)
    key = _es256_key()
    checkout = _sign(key, {"exp": int(now.timestamp()) + 60})
    disclosure = _b64url(json.dumps(["salt", "category", "software"]).encode())
    digest = _b64url(hashlib.sha256(disclosure.encode()).digest())
    claims = _base_claims(now, checkout)
    if attack != "collision": del claims["category"]
    claims["_sd"] = [digest]
    if attack == "algorithm": claims["_sd_alg"] = "sha-512"
    issuer = _sign(key, claims)
    token = issuer + "~" + disclosure + "~"
    if attack == "duplicate": token += disclosure + "~"
    with pytest.raises(AP2VerificationError):
        AP2IngressAdapter(payment_public_key=key.public_key()).verify(token, checkout, now=now)
