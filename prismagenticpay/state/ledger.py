"""Thread-safe multi-bucket authority ledger with hold TTL and replay-safe states."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

from pydantic import BaseModel


class HoldStatus(str, Enum):
    PENDING = "pending"
    SETTLED = "settled"
    RELEASED = "released"
    EXPIRED = "expired"
    GATHERED = "gathered"


class MultiBucketReservation(BaseModel):
    reservation_id: str
    payment_hash: str
    amount_cents: int
    created_at: datetime
    expires_at: datetime
    status: HoldStatus
    session_id: str
    principal_id: str
    mandate_id: str
    day: str


class BucketAvailability(BaseModel):
    session_remaining_cents: int
    daily_remaining_cents: int
    mandate_remaining_cents: int


class AtomicAuthorityLedger:
    def __init__(
        self,
        session_budget_cents: int,
        daily_budget_cents: int,
        mandate_budget_cents: int,
        default_hold_ttl_seconds: int = 120,
    ):
        if min(session_budget_cents, daily_budget_cents, mandate_budget_cents) < 0:
            raise ValueError("budget caps must be non-negative")
        if default_hold_ttl_seconds <= 0:
            raise ValueError("default_hold_ttl_seconds must be positive")

        self._lock = threading.Lock()
        self._session_budget_cents = session_budget_cents
        self._daily_budget_cents = daily_budget_cents
        self._mandate_budget_cents = mandate_budget_cents
        self._default_hold_ttl = default_hold_ttl_seconds

        self._session_settled: Dict[str, int] = {}
        self._daily_settled: Dict[str, int] = {}
        self._mandate_settled: Dict[str, int] = {}
        self._settled_cents = 0

        self._reservations: Dict[str, MultiBucketReservation] = {}
        self._hash_to_res_id: Dict[str, str] = {}
        self._on_mutate: Optional[Callable[[Dict[str, Any]], None]] = None

    def _sweep_expired_holds(self, now: datetime) -> None:
        for res in self._reservations.values():
            if res.status == HoldStatus.PENDING and now >= res.expires_at:
                res.status = HoldStatus.EXPIRED

    def _pending_for(self, predicate) -> int:
        return sum(
            r.amount_cents
            for r in self._reservations.values()
            if r.status == HoldStatus.PENDING and predicate(r)
        )

    def _availability(
        self,
        *,
        session_id: str,
        principal_id: str,
        mandate_id: str,
        day: str,
    ) -> BucketAvailability:
        session_pending = self._pending_for(lambda r: r.session_id == session_id)
        daily_pending = self._pending_for(lambda r: r.principal_id == principal_id and r.day == day)
        mandate_pending = self._pending_for(lambda r: r.mandate_id == mandate_id)
        return BucketAvailability(
            session_remaining_cents=(
                self._session_budget_cents - self._session_settled.get(session_id, 0) - session_pending
            ),
            daily_remaining_cents=(
                self._daily_budget_cents - self._daily_settled.get(f"{principal_id}:{day}", 0) - daily_pending
            ),
            mandate_remaining_cents=(
                self._mandate_budget_cents - self._mandate_settled.get(mandate_id, 0) - mandate_pending
            ),
        )

    def inspect_available(
        self,
        *,
        session_id: str,
        principal_id: str,
        mandate_id: str,
        now: Optional[datetime] = None,
    ) -> BucketAvailability:
        current_time = now or datetime.now(timezone.utc)
        day = current_time.date().isoformat()
        with self._lock:
            self._sweep_expired_holds(current_time)
            return self._availability(
                session_id=session_id,
                principal_id=principal_id,
                mandate_id=mandate_id,
                day=day,
            )

    def reserve(
        self,
        payment_hash: str,
        amount_cents: int,
        now: Optional[datetime] = None,
        *,
        session_id: str = "default",
        principal_id: str = "default",
        mandate_id: str = "default",
    ) -> Tuple[bool, Optional[str], Optional[MultiBucketReservation], str]:
        if amount_cents <= 0:
            return False, None, None, "BREACH: amount_cents must be positive"
        if not payment_hash:
            return False, None, None, "BREACH: payment_hash is required"

        current_time = now or datetime.now(timezone.utc)
        day = current_time.date().isoformat()
        with self._lock:
            self._sweep_expired_holds(current_time)

            if payment_hash in self._hash_to_res_id:
                res_id = self._hash_to_res_id[payment_hash]
                prior = self._reservations[res_id]
                if prior.status == HoldStatus.SETTLED:
                    return False, res_id, prior, "REPLAY_REJECTED: Transaction already settled."
                if prior.status == HoldStatus.PENDING:
                    return True, res_id, prior, "IDEMPOTENT_IN_FLIGHT: Active hold exists."
                if prior.status is HoldStatus.GATHERED:
                    pass
                elif prior.status in (HoldStatus.RELEASED, HoldStatus.EXPIRED):
                    return (
                        False,
                        res_id,
                        prior,
                        f"REPLAY_REJECTED: Transaction previously {prior.status.value}.",
                    )

            available = self._availability(
                session_id=session_id,
                principal_id=principal_id,
                mandate_id=mandate_id,
                day=day,
            )
            if amount_cents > available.session_remaining_cents:
                return False, None, None, (
                    f"BREACH: Session limit exceeded (avail: {available.session_remaining_cents}c)"
                )
            if amount_cents > available.daily_remaining_cents:
                return False, None, None, (
                    f"BREACH: Daily limit exceeded (avail: {available.daily_remaining_cents}c)"
                )
            if amount_cents > available.mandate_remaining_cents:
                return False, None, None, (
                    f"BREACH: Mandate limit exceeded (avail: {available.mandate_remaining_cents}c)"
                )

            res_id = f"res_{uuid.uuid4().hex}"
            reservation = MultiBucketReservation(
                reservation_id=res_id,
                payment_hash=payment_hash,
                amount_cents=amount_cents,
                created_at=current_time,
                expires_at=current_time + timedelta(seconds=self._default_hold_ttl),
                status=HoldStatus.PENDING,
                session_id=session_id,
                principal_id=principal_id,
                mandate_id=mandate_id,
                day=day,
            )
            self._reservations[res_id] = reservation
            self._hash_to_res_id[payment_hash] = res_id
            self._emit()
            return True, res_id, reservation, "HOLD_ACQUIRED"

    def commit(
        self, reservation_id: str, now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        current_time = now or datetime.now(timezone.utc)
        with self._lock:
            self._sweep_expired_holds(current_time)
            res = self._reservations.get(reservation_id)
            if not res:
                return False, "RESERVATION_NOT_FOUND"
            if res.status == HoldStatus.EXPIRED or (
                res.status == HoldStatus.PENDING and current_time >= res.expires_at
            ):
                res.status = HoldStatus.EXPIRED
                self._emit()
                return False, "HOLD_EXPIRED"
            if res.status != HoldStatus.PENDING:
                return False, f"CANNOT_COMMIT: Hold is in status {res.status.value}"

            res.status = HoldStatus.SETTLED
            self._settled_cents += res.amount_cents
            self._session_settled[res.session_id] = (
                self._session_settled.get(res.session_id, 0) + res.amount_cents
            )
            daily_key = f"{res.principal_id}:{res.day}"
            self._daily_settled[daily_key] = self._daily_settled.get(daily_key, 0) + res.amount_cents
            self._mandate_settled[res.mandate_id] = (
                self._mandate_settled.get(res.mandate_id, 0) + res.amount_cents
            )
            self._emit()
            return True, "SETTLED"

    def release(self, reservation_id: str, *, gathered: bool = False) -> bool:
        with self._lock:
            res = self._reservations.get(reservation_id)
            if not res or res.status != HoldStatus.PENDING:
                return False
            res.status = HoldStatus.GATHERED if gathered else HoldStatus.RELEASED
            self._emit()
            return True

    def expire(
        self, reservation_id: str, now: Optional[datetime] = None
    ) -> Tuple[bool, str]:
        current_time = now or datetime.now(timezone.utc)
        with self._lock:
            self._sweep_expired_holds(current_time)
            res = self._reservations.get(reservation_id)
            if not res:
                return False, "RESERVATION_NOT_FOUND"
            if res.status == HoldStatus.EXPIRED:
                return True, "EXPIRED"
            if res.status != HoldStatus.PENDING:
                return False, f"CANNOT_EXPIRE: Hold is in status {res.status.value}"
            if current_time < res.expires_at:
                return False, "HOLD_STILL_VALID"
            res.status = HoldStatus.EXPIRED
            self._emit()
            return True, "EXPIRED"

    def get(self, reservation_id: str) -> Optional[MultiBucketReservation]:
        with self._lock:
            return self._reservations.get(reservation_id)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def load_snapshot(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._settled_cents = int(data.get("settled_cents", 0))
            self._session_settled = {k: int(v) for k, v in data.get("session_settled", {}).items()}
            self._daily_settled = {k: int(v) for k, v in data.get("daily_settled", {}).items()}
            self._mandate_settled = {k: int(v) for k, v in data.get("mandate_settled", {}).items()}
            self._reservations = {
                key: MultiBucketReservation.model_validate(value)
                for key, value in data.get("reservations", {}).items()
            }
            self._hash_to_res_id = dict(data.get("hash_to_res_id", {}))

    def _snapshot_unlocked(self) -> Dict[str, Any]:
        return {
            "settled_cents": self._settled_cents,
            "session_settled": dict(self._session_settled),
            "daily_settled": dict(self._daily_settled),
            "mandate_settled": dict(self._mandate_settled),
            "reservations": {k: v.model_dump(mode="json") for k, v in self._reservations.items()},
            "hash_to_res_id": dict(self._hash_to_res_id),
        }

    def _emit(self) -> None:
        if self._on_mutate is not None:
            self._on_mutate(self._snapshot_unlocked())
