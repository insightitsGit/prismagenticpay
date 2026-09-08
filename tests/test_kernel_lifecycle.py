from __future__ import annotations

from conftest import fresh_metadata

from datetime import datetime, timedelta, timezone

from prismagenticpay.connectors.base import FactMeta
from prismagenticpay.core.evaluator import PrismThinkerPaymentEvaluator
from prismagenticpay.domain.models import PaymentAuthStatus
from prismagenticpay.policies.corporate import default_corporate_policies
from prismagenticpay.state.ledger import HoldStatus


def test_authorize_then_settle_commits_hold(authorizer, gateway, proposal, authority, frozen_now, ledger):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert decision.status is PaymentAuthStatus.AUTHORIZED
    assert decision.reasoning_directive == "EXECUTE"
    assert decision.reservation_id is not None
    assert decision.allowed_tools == ["settle_payment_rail"]
    assert decision.is_valid_for_settlement(proposal.compute_canonical_payment_hash(), now=frozen_now)

    hold = ledger.get(decision.reservation_id)
    assert hold is not None
    assert hold.status is HoldStatus.PENDING

    assert gateway.settle_transaction(
        decision, proposal.compute_canonical_payment_hash(), now=frozen_now
    )
    assert ledger.get(decision.reservation_id).status is HoldStatus.SETTLED


def test_policy_veto_releases_hold_and_blocks_settlement(
    authorizer, gateway, proposal, authority, frozen_now, ledger
):
    blocked = authority.model_copy(update={"vendor_status": "blocked"})
    decision = authorizer.process_authorization(proposal, blocked, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert decision.status is PaymentAuthStatus.REFUSED
    assert decision.reasoning_directive == "REFUSE"
    assert decision.reservation_id is None
    assert decision.allowed_tools == []

    pending = [r for r in ledger._reservations.values() if r.status is HoldStatus.PENDING]
    assert pending == []

    assert not decision.is_valid_for_settlement(
        proposal.compute_canonical_payment_hash(), now=frozen_now
    )
    assert not gateway.settle_transaction(
        decision, proposal.compute_canonical_payment_hash(), now=frozen_now
    )


def test_insufficient_session_budget_refuses_without_hold(authorizer, proposal, authority, frozen_now, ledger):
    over = proposal.model_copy(update={"amount_cents": 50_000, "idempotency_nonce": "nonce_over"})
    decision = authorizer.process_authorization(over, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert decision.status is PaymentAuthStatus.REFUSED
    assert "Session limit exceeded" in decision.rationale
    assert all(r.status is not HoldStatus.PENDING for r in ledger._reservations.values())


def test_dual_signature_keeps_hold_and_requires_second_approver(
    authorizer, gateway, proposal, authority, frozen_now, ledger
):
    dual = authority.model_copy(update={"requires_dual_signature": True})
    decision = authorizer.process_authorization(proposal, dual, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert decision.status is PaymentAuthStatus.REVIEW
    assert decision.reasoning_directive == "ESCALATE"
    assert decision.reservation_id is not None
    assert decision.review_case_id is not None
    assert ledger.get(decision.reservation_id).status is HoldStatus.PENDING
    assert not gateway.settle_transaction(
        decision, proposal.compute_canonical_payment_hash(), now=frozen_now
    )

    approved = authorizer.approve_review(
        decision.review_case_id, approver_id="controller_7", now=frozen_now
    )
    assert approved.status is PaymentAuthStatus.AUTHORIZED
    assert authorizer.signer.verify(approved)
    assert gateway.settle_transaction(
        approved, proposal.compute_canonical_payment_hash(), now=frozen_now
    )


def test_stale_required_fact_stalls(authorizer, proposal, authority, frozen_now):
    stale = frozen_now - timedelta(seconds=301)
    metadata = {
        key: FactMeta(source="erp", fetched_at=frozen_now, ttl_seconds=300)
        for key in PrismThinkerPaymentEvaluator._structured_facts(proposal, authority)
    }
    metadata["vendor_status"] = FactMeta(source="erp", fetched_at=stale, ttl_seconds=300)

    decision = authorizer.process_authorization(
        proposal, authority, fact_metadata=metadata, now=frozen_now
    )
    assert decision.status is PaymentAuthStatus.STALLED
    assert decision.reasoning_directive == "GATHER"


def test_settled_replay_is_rejected(authorizer, gateway, proposal, authority, frozen_now):
    first = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert gateway.settle_transaction(first, proposal.compute_canonical_payment_hash(), now=frozen_now)

    second = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert second.status is PaymentAuthStatus.REFUSED
    assert "already settled" in second.rationale


def test_released_replay_is_rejected(authorizer, proposal, authority, frozen_now):
    first = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert first.status is PaymentAuthStatus.AUTHORIZED
    assert authorizer.ledger.release(first.reservation_id)

    second = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert second.status is PaymentAuthStatus.REFUSED
    assert "released" in second.rationale


def test_idempotent_in_flight_returns_same_reservation(authorizer, proposal, authority, frozen_now):
    first = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    second = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    assert first.status is PaymentAuthStatus.AUTHORIZED
    assert second.status is PaymentAuthStatus.AUTHORIZED
    assert first.reservation_id == second.reservation_id
    assert first.payment_hash == second.payment_hash


def test_expired_hold_cannot_commit(authorizer, gateway, proposal, authority, frozen_now, ledger):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    later = frozen_now + timedelta(seconds=121)
    assert not gateway.settle_transaction(
        decision, proposal.compute_canonical_payment_hash(), now=later
    )
    assert ledger.get(decision.reservation_id).status in {HoldStatus.RELEASED, HoldStatus.EXPIRED}


def test_auth_window_expiry_releases_hold(authorizer, gateway, proposal, authority, frozen_now, ledger):
    decision = authorizer.process_authorization(
        proposal, authority, auth_validity_seconds=60, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),
    )
    later = frozen_now + timedelta(seconds=60)
    assert not decision.is_valid_for_settlement(
        proposal.compute_canonical_payment_hash(), now=later
    )
    assert not gateway.settle_transaction(
        decision, proposal.compute_canonical_payment_hash(), now=later
    )
    assert ledger.get(decision.reservation_id).status is HoldStatus.RELEASED


def test_hash_mismatch_blocks_settlement(authorizer, gateway, proposal, authority, frozen_now, ledger):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now),)
    mutated = proposal.model_copy(update={"category": "hardware", "idempotency_nonce": "nonce_1001"})
    assert mutated.compute_canonical_payment_hash() != decision.payment_hash
    assert not gateway.settle_transaction(
        decision, mutated.compute_canonical_payment_hash(), now=frozen_now
    )
    assert ledger.get(decision.reservation_id).status is HoldStatus.RELEASED


def test_category_and_mcc_are_in_payment_hash(proposal, merchant):
    other = proposal.model_copy(
        update={"merchant": merchant.model_copy(update={"mcc": "5812"}), "idempotency_nonce": "n2"}
    )
    assert proposal.compute_canonical_payment_hash() != other.compute_canonical_payment_hash()


def test_multi_bucket_isolation_does_not_share_session_caps(authorizer, proposal, authority, frozen_now, merchant):
    first = authorizer.process_authorization(
        proposal.model_copy(update={"amount_cents": 8000, "idempotency_nonce": "n-a"}),
        authority,
        now=frozen_now,
        session_id="session-a", fact_metadata=fresh_metadata(frozen_now),
    )
    other_principal = proposal.model_copy(
        update={
            "transaction_id": "tx_1002",
            "principal_id": "principal_99",
            "mandate_id": "mandate_99",
            "amount_cents": 8000,
            "idempotency_nonce": "n-b",
        }
    )
    second = authorizer.process_authorization(
        other_principal,
        authority,
        now=frozen_now,
        session_id="session-b", fact_metadata=fresh_metadata(frozen_now),
    )
    assert first.status is PaymentAuthStatus.AUTHORIZED
    assert second.status is PaymentAuthStatus.AUTHORIZED
    assert first.reservation_id != second.reservation_id


def test_same_session_second_hold_respects_remaining(authorizer, proposal, authority, frozen_now):
    first = authorizer.process_authorization(
        proposal.model_copy(update={"amount_cents": 8000, "idempotency_nonce": "n-1"}),
        authority,
        now=frozen_now,
        session_id="shared-session", fact_metadata=fresh_metadata(frozen_now),
    )
    second = authorizer.process_authorization(
        proposal.model_copy(update={"amount_cents": 8000, "idempotency_nonce": "n-2", "transaction_id": "tx_2"}),
        authority,
        now=frozen_now,
        session_id="shared-session", fact_metadata=fresh_metadata(frozen_now),
    )
    assert first.status is PaymentAuthStatus.AUTHORIZED
    assert second.status is PaymentAuthStatus.REFUSED
    assert "Session limit exceeded" in second.rationale


def test_evaluator_uses_to_chorusgraph_refuse_on_hard_veto(proposal, authority):
    evaluator = PrismThinkerPaymentEvaluator(default_corporate_policies())
    blocked = authority.model_copy(update={"vendor_status": "blocked"})
    outcome = evaluator.evaluate_proposal(proposal, blocked, fact_metadata=fresh_metadata(None),)
    assert outcome.directive == "REFUSE"
    assert outcome.allowed_tools == []
    assert outcome.decision_graph.disposition.value == "hard_veto"
