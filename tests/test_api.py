from __future__ import annotations

from fastapi.testclient import TestClient

from prismagenticpay.api.app import create_app
from prismagenticpay.domain.models import PaymentAuthStatus


def test_http_authorize_and_settle(authorizer, gateway, proposal, authority):
    client = TestClient(create_app(authorizer, gateway))
    response = client.post(
        "/v1/authorize",
        json={
            "proposal": proposal.model_dump(mode="json"),
            "authority": authority.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == PaymentAuthStatus.AUTHORIZED.value
    settle = client.post(
        "/v1/settle",
        json={
            "authorization": body,
            "payment_hash": proposal.compute_canonical_payment_hash(),
            "rail_id": "test",
        },
    )
    assert settle.status_code == 200
    audit = client.get("/v1/audit")
    assert audit.status_code == 200
    assert audit.json()
