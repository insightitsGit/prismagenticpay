from __future__ import annotations

from datetime import timedelta

import pytest

from prismagenticpay.connectors.base import FactMeta
from prismagenticpay.core.evaluator import PrismThinkerPaymentEvaluator
from prismagenticpay.domain.models import LineItem, PaymentAuthStatus
from prismagenticpay.state.ledger import HoldStatus
from prismagenticpay.state.sqlite_ledger import SqliteAuthorityLedger


def test_same_party_cannot_approve_review(authorizer, proposal, authority, frozen_now):
    dual = authority.model_copy(update={"requires_dual_signature": True})
    decision = authorizer.process_authorization(proposal, dual, now=frozen_now)
    with pytest.raises(ValueError, match="distinct"):
        authorizer.approve_review(decision.review_case_id, proposal.principal_id, now=frozen_now)


def test_gather_resume_reauthorizes_after_fresh_facts(authorizer, proposal, authority, frozen_now):
    stale = frozen_now - timedelta(seconds=301)
    metadata = {
        key: FactMeta(source="erp", fetched_at=frozen_now, ttl_seconds=300)
        for key in PrismThinkerPaymentEvaluator._structured_facts(proposal, authority)
    }
    metadata["vendor_status"] = FactMeta(source="erp", fetched_at=stale, ttl_seconds=300)
    stalled = authorizer.process_authorization(
        proposal, authority, fact_metadata=metadata, now=frozen_now
    )
    assert stalled.status is PaymentAuthStatus.STALLED
    assert stalled.gather_case_id is not None

    fresh = {
        key: FactMeta(source="erp", fetched_at=frozen_now, ttl_seconds=300)
        for key in PrismThinkerPaymentEvaluator._structured_facts(proposal, authority)
    }
    resumed = authorizer.resume_gather(
        stalled.gather_case_id, authority, fact_metadata=fresh, now=frozen_now
    )
    assert resumed.status is PaymentAuthStatus.AUTHORIZED


def test_line_items_change_payment_hash(proposal):
    with_items = proposal.model_copy(
        update={
            "line_items": [
                LineItem(sku="sku-1", name="Seat", amount_cents=2500, category="software")
            ]
        }
    )
    assert with_items.compute_canonical_payment_hash() != proposal.compute_canonical_payment_hash()


def test_sqlite_ledger_survives_reload(tmp_path, frozen_now):
    path = str(tmp_path / "ledger.sqlite")
    first = SqliteAuthorityLedger(path, 10_000, 10_000, 10_000, default_hold_ttl_seconds=60)
    ok, res_id, _, _ = first.reserve("hash-persist", 500, now=frozen_now, session_id="s", principal_id="p", mandate_id="m")
    assert ok
    first.commit(res_id, now=frozen_now)

    second = SqliteAuthorityLedger(path, 10_000, 10_000, 10_000, default_hold_ttl_seconds=60)
    hold = second.get(res_id)
    assert hold is not None
    assert hold.status is HoldStatus.SETTLED
    ok2, _, _, msg = second.reserve("hash-persist", 500, now=frozen_now)
    assert not ok2
    assert "already settled" in msg
