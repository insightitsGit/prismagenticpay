"""Human-review and gather-resume cases."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from prismagenticpay.domain.models import AuthorityState, PaymentProposal


class CaseStatus(str, Enum):
    OPEN = "open"
    APPROVED = "approved"
    DENIED = "denied"
    RESUMED = "resumed"
    EXPIRED = "expired"


class ReviewCase(BaseModel):
    case_id: str
    authorization_id: str
    payment_hash: str
    reservation_id: str
    principal_id: str
    agent_id: str
    approver_id: Optional[str] = None
    status: CaseStatus = CaseStatus.OPEN
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None


class GatherCase(BaseModel):
    case_id: str
    authorization_id: str
    payment_hash: str
    session_id: Optional[str] = None
    missing_fact_keys: List[str] = Field(default_factory=list)
    status: CaseStatus = CaseStatus.OPEN
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: Optional[datetime] = None


class CaseStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._reviews: Dict[str, ReviewCase] = {}
        self._gathers: Dict[str, GatherCase] = {}
        self._proposals: Dict[str, PaymentProposal] = {}
        self._authorities: Dict[str, AuthorityState] = {}

    def open_review(
        self,
        *,
        authorization_id: str,
        payment_hash: str,
        reservation_id: str,
        proposal: PaymentProposal,
        authority: AuthorityState,
        now: datetime,
    ) -> ReviewCase:
        case = ReviewCase(
            case_id=f"rev_{uuid.uuid4().hex[:12]}",
            authorization_id=authorization_id,
            payment_hash=payment_hash,
            reservation_id=reservation_id,
            principal_id=proposal.principal_id,
            agent_id=proposal.agent_id,
            created_at=now,
        )
        with self._lock:
            self._reviews[case.case_id] = case
            self._proposals[payment_hash] = proposal
            self._authorities[payment_hash] = authority
        return case

    def open_gather(
        self,
        *,
        authorization_id: str,
        payment_hash: str,
        proposal: PaymentProposal,
        authority: AuthorityState,
        missing_fact_keys: List[str],
        session_id: Optional[str] = None,
        now: datetime,
    ) -> GatherCase:
        case = GatherCase(
            case_id=f"gth_{uuid.uuid4().hex[:12]}",
            authorization_id=authorization_id,
            payment_hash=payment_hash,
            missing_fact_keys=list(missing_fact_keys),
            session_id=session_id,
            created_at=now,
        )
        with self._lock:
            self._gathers[case.case_id] = case
            self._proposals[payment_hash] = proposal
            self._authorities[payment_hash] = authority
        return case

    def get_review(self, case_id: str) -> Optional[ReviewCase]:
        with self._lock:
            return self._reviews.get(case_id)

    def get_gather(self, case_id: str) -> Optional[GatherCase]:
        with self._lock:
            return self._gathers.get(case_id)

    def proposal_for(self, payment_hash: str) -> Optional[PaymentProposal]:
        with self._lock:
            return self._proposals.get(payment_hash)

    def authority_for(self, payment_hash: str) -> Optional[AuthorityState]:
        with self._lock:
            return self._authorities.get(payment_hash)

    def mark_review(self, case_id: str, status: CaseStatus, approver_id: str, now: datetime) -> ReviewCase:
        with self._lock:
            case = self._reviews[case_id]
            case.status = status
            case.approver_id = approver_id
            case.resolved_at = now
            return case

    def mark_gather(self, case_id: str, status: CaseStatus, now: datetime) -> GatherCase:
        with self._lock:
            case = self._gathers[case_id]
            case.status = status
            case.resolved_at = now
            return case


class DurableCaseStore(CaseStore):
    """Cases participate in the same transaction as decisions and holds."""
    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger

    def _call(self, name, *args, **kwargs):
        with self.ledger.transaction():
            data = self.ledger.get_record("workflow", "cases") or {}
            for attr, model in [("_reviews", ReviewCase), ("_gathers", GatherCase),
                                ("_proposals", PaymentProposal), ("_authorities", AuthorityState)]:
                setattr(self, attr, {k: model.model_validate(v) for k, v in data.get(attr, {}).items()})
            result = getattr(super(), name)(*args, **kwargs)
            self.ledger.put_record("workflow", "cases", {
                attr: {k: v.model_dump(mode="json") for k, v in getattr(self, attr).items()}
                for attr in ["_reviews", "_gathers", "_proposals", "_authorities"]
            })
            return result


def _durable_case_method(name):
    def call(self, *args, **kwargs):
        return self._call(name, *args, **kwargs)
    return call


for _name in ("open_review", "open_gather", "get_review", "get_gather", "proposal_for",
              "authority_for", "mark_review", "mark_gather"):
    setattr(DurableCaseStore, _name, _durable_case_method(_name))
