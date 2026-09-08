from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prismagenticpay.state.ledger import AtomicAuthorityLedger, HoldStatus


def test_reserve_commit_and_release_lifecycle():
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    ledger = AtomicAuthorityLedger(10_000, 10_000, 10_000, default_hold_ttl_seconds=30)

    ok, res_id, hold, msg = ledger.reserve("hash-a", 1000, now=now, session_id="s1", principal_id="p1", mandate_id="m1")
    assert ok and msg == "HOLD_ACQUIRED"
    assert hold.status is HoldStatus.PENDING

    assert ledger.release(res_id)
    assert ledger.get(res_id).status is HoldStatus.RELEASED

    ok2, _, _, msg2 = ledger.reserve("hash-a", 1000, now=now)
    assert not ok2
    assert "released" in msg2


def test_hold_ttl_sweep_blocks_commit():
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    ledger = AtomicAuthorityLedger(10_000, 10_000, 10_000, default_hold_ttl_seconds=10)
    ok, res_id, _, _ = ledger.reserve("hash-b", 500, now=now)
    assert ok
    success, reason = ledger.commit(res_id, now=now + timedelta(seconds=10))
    assert not success
    assert reason == "HOLD_EXPIRED"
    assert ledger.get(res_id).status is HoldStatus.EXPIRED


def test_independent_sessions_do_not_consume_each_other():
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    ledger = AtomicAuthorityLedger(1000, 10_000, 10_000, default_hold_ttl_seconds=30)
    ok_a, _, _, _ = ledger.reserve("h1", 800, now=now, session_id="a", principal_id="p1", mandate_id="m1")
    ok_b, _, _, _ = ledger.reserve("h2", 800, now=now, session_id="b", principal_id="p2", mandate_id="m2")
    assert ok_a and ok_b
