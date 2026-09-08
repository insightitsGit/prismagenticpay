from conftest import fresh_metadata
from datetime import timedelta
from unittest.mock import Mock

import pytest

from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.domain.models import PaymentAuthStatus
from prismagenticpay.lifecycle.orchestrator import RailOrchestrator
from prismagenticpay.rails.base import RailResult
from prismagenticpay.policies.corporate import default_corporate_policies


@pytest.fixture
def rail():
    result = Mock()
    result.capture.return_value = RailResult(ok=True, rail_id="test", reference="capture-1")
    result.refund.return_value = RailResult(ok=True, rail_id="test", reference="refund-1")
    return result


@pytest.fixture
def orchestrator(ledger, gateway, signer, rail):
    return RailOrchestrator(ledger, gateway, {"test": rail}, signer)


@pytest.mark.parametrize("failure", ["review", "expired", "cart_swap", "zero", "negative", "over", "replay", "rail", "signature"])
def test_capture_rejects_before_remote_call(failure, orchestrator, rail, authorizer, proposal, authority, frozen_now):
    if failure == "review":
        authority = authority.model_copy(update={"requires_dual_signature": True})
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    now = frozen_now
    amount = None
    if failure == "expired":
        now += timedelta(seconds=60)
    if failure == "cart_swap":
        proposal = proposal.model_copy(update={"category": "hardware"})
    if failure in {"zero", "negative", "over"}:
        amount = {"zero": 0, "negative": -1, "over": 2501}[failure]
    if failure == "replay":
        authorizer.ledger.commit(decision.reservation_id, now=now)
    if failure == "rail":
        orchestrator.gateway.allowed_rails = set()
    if failure == "signature":
        decision = decision.model_copy(update={"signature": "invalid"})
    result, receipt = orchestrator.capture(proposal, decision, rail_id="test", payment_token="pm_local", amount_cents=amount, now=now)
    assert not result.ok
    assert receipt is None
    rail.capture.assert_not_called()


@pytest.mark.parametrize("failure", ["signature", "cart_swap", "over", "zero", "negative", "rail"])
def test_refund_rejects_before_remote_call(failure, orchestrator, rail, authorizer, proposal, authority, frozen_now):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert orchestrator.capture(proposal, decision, rail_id="test", payment_token="pm_local", now=frozen_now)[0].ok
    amount = 100
    if failure == "signature":
        decision = decision.model_copy(update={"signature": "invalid"})
    if failure == "cart_swap":
        proposal = proposal.model_copy(update={"amount_cents": 1})
    if failure in {"over", "zero", "negative"}:
        amount = {"over": 2501, "zero": 0, "negative": -1}[failure]
    if failure == "rail":
        orchestrator.gateway.allowed_rails = set()
    assert not orchestrator.refund(proposal, decision, rail_id="test", amount_cents=amount, operation_id="test-refund-1", now=frozen_now).ok
    rail.refund.assert_not_called()


def test_restart_cannot_promote_review(ledger, signer, proposal, authority, frozen_now):
    first = PrismPaymentAuthorizer(ledger, default_corporate_policies(), signer=signer)
    review = first.process_authorization(proposal, authority.model_copy(update={"requires_dual_signature": True}), now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert review.status is PaymentAuthStatus.REVIEW
    restarted = PrismPaymentAuthorizer(ledger, default_corporate_policies(), signer=signer)
    replay = restarted.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert replay.status is not PaymentAuthStatus.AUTHORIZED


def test_empty_rail_allowlist_is_respected(ledger, signer):
    assert SettlementGatewayHarness(ledger, signer=signer, allowed_rails=set()).allowed_rails == set()


@pytest.mark.parametrize("seed", ["", "ab", "z" * 64])
def test_production_requires_stable_signing_seed(seed):
    from prismagenticpay.config import Settings
    from prismagenticpay.runtime import create_production_app
    with pytest.raises(RuntimeError, match="32-byte seed"):
        create_production_app(Settings(environment="production", api_keys=["local-test"], signing_seed_hex=seed))


def test_concurrent_capture_calls_rail_once(orchestrator, rail, authorizer, proposal, authority, frozen_now):
    from concurrent.futures import ThreadPoolExecutor
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    def capture(_):
        return orchestrator.capture(proposal, decision, rail_id="test", payment_token="pm_local", now=frozen_now)[0].ok
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(capture, range(8)))
    assert sum(results) == 1
    rail.capture.assert_called_once()
