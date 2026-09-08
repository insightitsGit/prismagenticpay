from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
import pytest
from conftest import fresh_metadata
from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.lifecycle.orchestrator import RailOrchestrator
from prismagenticpay.policies.corporate import default_corporate_policies
from prismagenticpay.rails.base import RailResult
from prismagenticpay.state.ledger import HoldStatus
from prismagenticpay.state.sqlite_ledger import SqliteAuthorityLedger


def stack(path, signer, rail):
    ledger = SqliteAuthorityLedger(str(path), 10000, 20000, 15000)
    authorizer = PrismPaymentAuthorizer(ledger, default_corporate_policies(), signer=signer)
    gateway = SettlementGatewayHarness(ledger, signer=signer, allowed_rails={"test"})
    return ledger, authorizer, RailOrchestrator(ledger, gateway, {"test": rail}, signer)


class IdempotentProvider:
    supports_recovery = True
    def __init__(self):
        self.remote = {}
        self.calls = 0
        self.lose_response = True
    def capture(self, proposal, decision, amount, token, *, operation_id, capture_reference=""):
        if operation_id not in self.remote:
            self.calls += 1
            self.remote[operation_id] = RailResult(ok=True, rail_id="test", reference="remote-1")
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost after payment")
        return self.remote[operation_id]
    def refund(self, proposal, decision, amount, reference, *, operation_id, refund_reference=""):
        if operation_id not in self.remote:
            self.calls += 1
            self.remote[operation_id] = RailResult(ok=True, rail_id="test", reference="refund-" + str(self.calls))
        return self.remote[operation_id]
    def reconcile(self, proposal, decision, amount, *, operation_id, **kwargs):
        return self.remote[operation_id]


def test_lost_response_restart_recovery_no_double_payment(tmp_path, signer, proposal, authority, frozen_now):
    provider = IdempotentProvider()
    path = tmp_path / "recovery.sqlite"
    ledger, auth, orchestrator = stack(path, signer, provider)
    decision = auth.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    assert not orchestrator.capture(proposal, decision, rail_id="test", payment_token="local", now=frozen_now)[0].ok
    assert ledger.get(decision.reservation_id).status is HoldStatus.PROCESSING
    assert not ledger.release(decision.reservation_id)
    assert not ledger.commit(decision.reservation_id, now=frozen_now)[0]
    assert not ledger.expire(decision.reservation_id, now=frozen_now + timedelta(days=1))[0]
    ledger.close()
    ledger, auth, orchestrator = stack(path, signer, provider)
    assert orchestrator.recover("capture_" + decision.reservation_id, now=frozen_now + timedelta(minutes=3))[0].ok
    assert provider.calls == 1
    assert ledger.get(decision.reservation_id).captured_cents == 2500
    for key in ["refund-one", "refund-one", "refund-two"]:
        assert orchestrator.refund(proposal, decision, rail_id="test", amount_cents=500, operation_id=key, now=frozen_now).ok
    assert provider.calls == 3
    assert ledger.get(decision.reservation_id).refunded_cents == 1000
    ledger.close()


def test_crash_after_provider_before_database_commit(tmp_path, signer, proposal, authority, frozen_now, monkeypatch):
    provider = IdempotentProvider()
    provider.lose_response = False
    path = tmp_path / "commit.sqlite"
    ledger, auth, orchestrator = stack(path, signer, provider)
    decision = auth.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    original = ledger._persist
    def fail_completion(snapshot):
        if any(o["status"] == "done" for o in snapshot.get("records", {}).get("operations", {}).values()):
            raise OSError("disk failure")
        original(snapshot)
    monkeypatch.setattr(ledger, "_persist", fail_completion)
    with pytest.raises(OSError):
        orchestrator.capture(proposal, decision, rail_id="test", payment_token="local", now=frozen_now)
    ledger.close()
    ledger, auth, orchestrator = stack(path, signer, provider)
    assert ledger.get(decision.reservation_id).status is HoldStatus.PROCESSING
    assert orchestrator.recover("capture_" + decision.reservation_id, now=frozen_now + timedelta(seconds=121))[0].ok
    assert provider.calls == 1
    ledger.close()


def test_old_unknown_requires_read_only_reconciliation(tmp_path, signer, proposal, authority, frozen_now):
    provider = IdempotentProvider()
    ledger, auth, orchestrator = stack(tmp_path / "old.sqlite", signer, provider)
    decision = auth.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    orchestrator.capture(proposal, decision, rail_id="test", payment_token="local", now=frozen_now)
    key = "capture_" + decision.reservation_id
    later = frozen_now + timedelta(days=2)
    assert orchestrator.recover(key, now=later)[0].detail == "manual_reconciliation_required"
    assert provider.calls == 1
    assert orchestrator.reconcile(key, reference="remote-1", now=later).ok
    assert ledger.get(decision.reservation_id).captured_cents == 2500
    ledger.close()


def test_review_and_audit_survive_restart_and_approval_is_atomic(tmp_path, signer, proposal, authority, frozen_now):
    path = tmp_path / "review.sqlite"
    first, auth, _ = stack(path, signer, Mock())
    review = auth.process_authorization(proposal, authority.model_copy(update={"requires_dual_signature": True}),
        now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    first.close()
    a, auth_a, _ = stack(path, signer, Mock())
    b, auth_b, _ = stack(path, signer, Mock())
    assert auth_a.cases.get_review(review.review_case_id).status.value == "open"
    assert auth_a.audit.list()[0].event_type == "authorize.review"
    def approve(auth):
        try:
            return auth.approve_review(review.review_case_id, "controller", now=frozen_now).status.value
        except ValueError:
            return "closed"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, [auth_a, auth_b]))
    assert sorted(results) == ["AUTHORIZED", "closed"]
    assert len([e for e in auth_a.audit.list() if e.event_type == "review.approved"]) == 1
    a.close(); b.close()


def test_two_sqlite_connections_cannot_overspend(tmp_path, frozen_now):
    path = str(tmp_path / "budget.sqlite")
    a = SqliteAuthorityLedger(path, 100, 100, 100)
    b = SqliteAuthorityLedger(path, 100, 100, 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pair: pair[0].reserve(pair[1], 75, now=frozen_now)[0], [(a, "a"), (b, "b")]))
    assert sum(results) == 1
    a.close(); b.close()


def test_transaction_failure_rolls_back_everything(tmp_path, frozen_now, monkeypatch):
    ledger = SqliteAuthorityLedger(str(tmp_path / "rollback.sqlite"), 100, 100, 100)
    original = ledger._persist
    monkeypatch.setattr(ledger, "_persist", Mock(side_effect=OSError("disk failure")))
    with pytest.raises(OSError):
        with ledger.transaction():
            ledger.reserve("a", 75, now=frozen_now)
            ledger.put_record("audit", "a", {"event": "reserved"})
    monkeypatch.setattr(ledger, "_persist", original)
    assert ledger.list_records("audit") == []
    assert ledger.inspect_available(session_id="default", principal_id="default", mandate_id="default", now=frozen_now).session_remaining_cents == 100
    ledger.close()


def test_no_metadata_or_future_metadata_is_not_authority(authorizer, proposal, authority, frozen_now):
    result = authorizer.process_authorization(proposal, authority, now=frozen_now)
    assert result.status.value == "STALLED"
    metadata = fresh_metadata(frozen_now + timedelta(minutes=1))
    result = authorizer.resume_gather(result.gather_case_id, authority, now=frozen_now, fact_metadata=metadata)
    assert result.status.value == "STALLED"


def test_gather_scope_persists(tmp_path, signer, proposal, authority, frozen_now):
    path = tmp_path / "gather.sqlite"
    ledger, auth, _ = stack(path, signer, Mock())
    gathered = auth.process_authorization(proposal, authority, session_id="original", now=frozen_now)
    ledger.close()
    ledger, auth, _ = stack(path, signer, Mock())
    with pytest.raises(ValueError, match="session"):
        auth.resume_gather(gathered.gather_case_id, authority, session_id="new", now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    decision = auth.resume_gather(gathered.gather_case_id, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    assert ledger.get(decision.reservation_id).session_id == "original"
    ledger.close()


@pytest.mark.parametrize("field,value", [("rationale", "changed"), ("allowed_tools", ["another_tool"]), ("review_case_id", "changed")])
def test_all_decision_evidence_is_signed(field, value, authorizer, proposal, authority, frozen_now):
    decision = authorizer.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    assert not authorizer.signer.verify(decision.model_copy(update={field: value}))


def test_confirmed_failure_releases_only_after_provider_evidence(tmp_path, signer, proposal, authority, frozen_now):
    provider = IdempotentProvider()
    ledger, auth, orchestrator = stack(tmp_path / "failed.sqlite", signer, provider)
    decision = auth.process_authorization(proposal, authority, now=frozen_now, fact_metadata=fresh_metadata(frozen_now))
    orchestrator.capture(proposal, decision, rail_id="test", payment_token="local", now=frozen_now)
    provider.reconcile = lambda *a, **k: RailResult(ok=False, rail_id="test", reference="remote-1", detail="confirmed_not_paid")
    result = orchestrator.reconcile("capture_" + decision.reservation_id, now=frozen_now)
    assert result.detail == "confirmed_not_paid"
    assert ledger.get(decision.reservation_id).status is HoldStatus.RELEASED
    ledger.close()
