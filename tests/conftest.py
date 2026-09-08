from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.core.signing import DecisionSigner
from prismagenticpay.domain.models import AuthorityState, MerchantIdentity, PaymentProposal
from prismagenticpay.policies.corporate import default_corporate_policies
from prismagenticpay.state.ledger import AtomicAuthorityLedger


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 9, 8, 19, 30, tzinfo=timezone.utc)


@pytest.fixture
def merchant() -> MerchantIdentity:
    return MerchantIdentity(
        merchant_id="merch_cloud",
        merchant_name="Example Cloud",
        mcc="5734",
        domain="cloud.example",
    )


@pytest.fixture
def proposal(merchant: MerchantIdentity, frozen_now: datetime) -> PaymentProposal:
    return PaymentProposal(
        transaction_id="tx_1001",
        amount_cents=2500,
        currency="USD",
        merchant=merchant,
        category="software",
        agent_id="agent_shopper",
        principal_id="principal_42",
        mandate_id="mandate_42",
        mandate_type="closed_payment",
        mandate_hash="a" * 64,
        requested_at=frozen_now,
        idempotency_nonce="nonce_1001",
    )


@pytest.fixture
def authority(frozen_now: datetime) -> AuthorityState:
    return AuthorityState(
        transaction_cap_cents=10_000,
        session_remaining_cents=10_000,
        daily_remaining_cents=20_000,
        mandate_remaining_cents=15_000,
        vendor_status="approved",
        tier="standard",
        requires_dual_signature=False,
        snapshot_taken_at=frozen_now,
    )


@pytest.fixture
def ledger() -> AtomicAuthorityLedger:
    return AtomicAuthorityLedger(
        session_budget_cents=10_000,
        daily_budget_cents=20_000,
        mandate_budget_cents=15_000,
        default_hold_ttl_seconds=120,
    )


@pytest.fixture
def signer() -> DecisionSigner:
    return DecisionSigner.generate()


@pytest.fixture
def authorizer(ledger: AtomicAuthorityLedger, signer: DecisionSigner) -> PrismPaymentAuthorizer:
    return PrismPaymentAuthorizer(ledger, default_corporate_policies(), signer=signer)


@pytest.fixture
def gateway(ledger: AtomicAuthorityLedger, signer: DecisionSigner) -> SettlementGatewayHarness:
    return SettlementGatewayHarness(ledger, signer=signer)


def fresh_metadata(now=None):
    """Explicit trusted test-source metadata; production never fabricates it."""
    from prismagenticpay.connectors.base import FactMeta
    from prismagenticpay.core.evaluator import PrismThinkerPaymentEvaluator
    stamp = now or datetime.now(timezone.utc)
    return {key: FactMeta(source="test_authority", fetched_at=stamp, ttl_seconds=300)
            for key in PrismThinkerPaymentEvaluator._fact_specs()}


def write_identities(path):
    import hashlib, json
    records = [
        dict(subject="employee", roles=["authorize", "capture", "refund", "audit"],
             principal_id="employee", agent_id="purchasing-agent", key="local-scenario-key"),
        dict(subject="employee", roles=["review"], key="employee-review-key"),
        dict(subject="finance-controller", roles=["review", "audit", "policy", "reconcile"], key="controller-key"),
    ]
    for record in records:
        record["key_sha256"] = hashlib.sha256(record.pop("key").encode()).hexdigest()
    path.write_text(json.dumps({"identities": records}))
    return str(path)
