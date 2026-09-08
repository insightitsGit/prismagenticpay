"""Hold → evaluate → time-bound signed authorization. Never bypasses to_chorusgraph()."""

from __future__ import annotations

import threading
import hashlib
import json
from functools import wraps
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from prismthinker import PolicyRule

from prismagenticpay.connectors.base import FactMeta
from prismagenticpay.core.clock import Clock, SystemClock
from prismagenticpay.core.evaluator import PrismThinkerPaymentEvaluator
from prismagenticpay.core.signing import DecisionSigner
from prismagenticpay.domain.models import (
    AuthorizationDecision,
    AuthorityState,
    PaymentAuthStatus,
    PaymentProposal,
)
from prismagenticpay.state.audit import AuditEvent, AuditLog, DurableAuditLog
from prismagenticpay.state.cases import CaseStatus, CaseStore, DurableCaseStore
from prismagenticpay.state.ledger import AtomicAuthorityLedger

_DIRECTIVE_STATUS = {
    "EXECUTE": PaymentAuthStatus.AUTHORIZED,
    "REFUSE": PaymentAuthStatus.REFUSED,
    "ESCALATE": PaymentAuthStatus.REVIEW,
    "GATHER": PaymentAuthStatus.STALLED,
}


def _atomic(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.ledger.transaction():
            return method(self, *args, **kwargs)
    return call


class PrismPaymentAuthorizer:
    def __init__(
        self,
        ledger: AtomicAuthorityLedger,
        policies: List[PolicyRule],
        *,
        signer: Optional[DecisionSigner] = None,
        clock: Optional[Clock] = None,
        cases: Optional[CaseStore] = None,
        audit: Optional[AuditLog] = None,
    ):
        self.ledger = ledger
        self.evaluator = PrismThinkerPaymentEvaluator(policies)
        self.signer = signer or DecisionSigner.generate()
        self.clock = clock or SystemClock()
        self.cases = cases or DurableCaseStore(ledger)
        self.audit = audit or DurableAuditLog(ledger)
        self._lock = threading.Lock()
        self._decisions: Dict[str, AuthorizationDecision] = {}

    @_atomic
    def process_authorization(
        self,
        proposal: PaymentProposal,
        authority: AuthorityState,
        auth_validity_seconds: int = 60,
        session_id: Optional[str] = None,
        fact_metadata: Optional[Dict[str, FactMeta]] = None,
        now: Optional[datetime] = None,
        actor_id: str = "system",
    ) -> AuthorizationDecision:
        if auth_validity_seconds <= 0:
            raise ValueError("auth_validity_seconds must be positive")

        saved_policies = self.ledger.get_record("workflow", "policies")
        if saved_policies is not None:
            self.evaluator.policies = [PolicyRule.model_validate(p) for p in saved_policies]
        current_time = now or self.clock.now()
        if proposal.mandate_expires_at is not None and current_time >= proposal.mandate_expires_at:
            raise ValueError("payment mandate expired")
        payment_hash = proposal.compute_canonical_payment_hash()
        scope_session = session_id or f"session:{proposal.principal_id}"

        terminal = self.ledger.get_record("terminal", payment_hash)
        if terminal:
            return AuthorizationDecision.model_validate(terminal)
        available = self.ledger.inspect_available(
            session_id=scope_session,
            principal_id=proposal.principal_id,
            mandate_id=proposal.mandate_id,
            now=current_time,
        )
        evaluation_authority = authority.model_copy(
            update={
                "session_remaining_cents": min(
                    authority.session_remaining_cents, available.session_remaining_cents
                ),
                "daily_remaining_cents": min(
                    authority.daily_remaining_cents, available.daily_remaining_cents
                ),
                "mandate_remaining_cents": min(
                    authority.mandate_remaining_cents, available.mandate_remaining_cents
                ),
            }
        )

        ok, res_id, hold, msg = self.ledger.reserve(
            payment_hash,
            proposal.amount_cents,
            now=current_time,
            session_id=scope_session,
            principal_id=proposal.principal_id,
            mandate_id=proposal.mandate_id,
        )
        if not ok:
            decision = self._decision(
                status=PaymentAuthStatus.REFUSED,
                directive="REFUSE",
                proposal=proposal,
                authority=evaluation_authority,
                payment_hash=payment_hash,
                now=current_time,
                expires_at=current_time,
                rationale=msg,
            )
            if res_id is None:
                self.ledger.put_record("terminal", payment_hash, decision.model_dump(mode="json"))
            self._audit("authorize.refused", decision, actor_id, msg)
            return decision

        if msg.startswith("IDEMPOTENT_IN_FLIGHT"):
            cached = self._cached(payment_hash)
            if cached is not None:
                return cached
            # A hold proves only that budget was reserved, never policy approval.
            # An interrupted legacy workflow may lack a decision: fail closed after restart or
            # while another request is still evaluating this payment.
            decision = self._decision(
                status=PaymentAuthStatus.REFUSED,
                directive="REFUSE",
                proposal=proposal,
                authority=evaluation_authority,
                payment_hash=payment_hash,
                now=current_time,
                expires_at=current_time,
                rationale="IN_FLIGHT_DECISION_UNAVAILABLE: original decision required",
            )
            self._audit("authorize.replay_unavailable", decision, actor_id, decision.rationale)
            return decision

        outcome = self.evaluator.evaluate_proposal(
            proposal,
            evaluation_authority,
            fact_metadata=fact_metadata,
            now=current_time,
        )

        if outcome.directive == "ESCALATE":
            auth_expires = current_time + timedelta(seconds=auth_validity_seconds)
            if hold is not None:
                auth_expires = min(auth_expires, hold.expires_at)
            decision = self._decision(
                status=PaymentAuthStatus.REVIEW,
                directive="ESCALATE",
                proposal=proposal,
                authority=evaluation_authority,
                payment_hash=payment_hash,
                now=current_time,
                expires_at=auth_expires,
                rationale=outcome.decision_graph.recommended_rationale
                or "Evaluation resulted in ESCALATE",
                reservation_id=res_id,
                allowed_tools=[],
            )
            review = self.cases.open_review(
                authorization_id=decision.authorization_id,
                payment_hash=payment_hash,
                reservation_id=res_id,
                proposal=proposal,
                authority=evaluation_authority,
                now=current_time,
            )
            decision = self.signer.sign(
                decision.model_copy(update={"review_case_id": review.case_id})
            )
            self._store(payment_hash, decision)
            self._audit("authorize.review", decision, actor_id, "hold retained for dual control")
            return decision

        if outcome.directive != "EXECUTE":
            self.ledger.release(res_id, gathered=outcome.directive == "GATHER")
            decision = self._decision(
                status=_DIRECTIVE_STATUS.get(outcome.directive, PaymentAuthStatus.REFUSED),
                directive=outcome.directive,
                proposal=proposal,
                authority=evaluation_authority,
                payment_hash=payment_hash,
                now=current_time,
                expires_at=current_time,
                rationale=outcome.decision_graph.recommended_rationale
                or f"Evaluation resulted in {outcome.directive}",
                allowed_tools=[],
                gather_fact_keys=list(outcome.gather_fact_keys),
            )
            if outcome.directive == "GATHER":
                gather = self.cases.open_gather(
                    authorization_id=decision.authorization_id,
                    payment_hash=payment_hash,
                    proposal=proposal,
                    authority=evaluation_authority,
                    missing_fact_keys=list(outcome.gather_fact_keys),
                    session_id=scope_session,
                    now=current_time,
                )
                decision = self.signer.sign(
                    decision.model_copy(update={"gather_case_id": gather.case_id})
                )
            self._audit("authorize.blocked", decision, actor_id, decision.rationale)
            return decision

        auth_expires = current_time + timedelta(seconds=auth_validity_seconds)
        if hold is not None:
            auth_expires = min(auth_expires, hold.expires_at)
        decision = self._decision(
            status=PaymentAuthStatus.AUTHORIZED,
            directive="EXECUTE",
            proposal=proposal,
            authority=evaluation_authority,
            payment_hash=payment_hash,
            now=current_time,
            expires_at=auth_expires,
            rationale=outcome.decision_graph.recommended_rationale
            or "All corporate invariants satisfied.",
            reservation_id=res_id,
            allowed_tools=list(outcome.allowed_tools),
        )
        self._store(payment_hash, decision)
        self._audit("authorize.authorized", decision, actor_id, "EXECUTE")
        return decision

    @_atomic
    def approve_review(
        self,
        case_id: str,
        approver_id: str,
        now: Optional[datetime] = None,
        auth_validity_seconds: int = 60,
    ) -> AuthorizationDecision:
        if auth_validity_seconds <= 0:
            raise ValueError("auth_validity_seconds must be positive")
        current_time = now or self.clock.now()
        case = self.cases.get_review(case_id)
        if case is None or case.status is not CaseStatus.OPEN:
            raise ValueError("review case is not open")
        if approver_id in {case.principal_id, case.agent_id}:
            raise ValueError("approver must be distinct from principal and agent")
        hold = self.ledger.get(case.reservation_id)
        if hold is None or hold.status.value != "pending" or current_time >= hold.expires_at:
            raise ValueError("review hold is no longer pending")
        proposal = self.cases.proposal_for(case.payment_hash)
        authority = self.cases.authority_for(case.payment_hash)
        if proposal is None or authority is None:
            raise ValueError("review case is missing the original proposal")
        if proposal.mandate_expires_at is not None and current_time >= proposal.mandate_expires_at:
            raise ValueError("payment mandate expired")
        self.cases.mark_review(case_id, CaseStatus.APPROVED, approver_id, current_time)
        decision = self._decision(
            status=PaymentAuthStatus.AUTHORIZED,
            directive="EXECUTE",
            proposal=proposal,
            authority=authority,
            payment_hash=case.payment_hash,
            now=current_time,
            expires_at=min(hold.expires_at, current_time + timedelta(seconds=auth_validity_seconds)),
            rationale=f"Dual-control approved by {approver_id}",
            reservation_id=case.reservation_id,
            allowed_tools=["settle_payment_rail"],
            review_case_id=case_id,
        )
        self._store(case.payment_hash, decision)
        self._audit("review.approved", decision, approver_id, case_id)
        return decision

    @_atomic
    def deny_review(self, case_id: str, approver_id: str, now: Optional[datetime] = None) -> AuthorizationDecision:
        current_time = now or self.clock.now()
        case = self.cases.get_review(case_id)
        if case is None or case.status is not CaseStatus.OPEN:
            raise ValueError("review case is not open")
        self.ledger.release(case.reservation_id)
        self.cases.mark_review(case_id, CaseStatus.DENIED, approver_id, current_time)
        proposal = self.cases.proposal_for(case.payment_hash)
        authority = self.cases.authority_for(case.payment_hash)
        if proposal is None or authority is None:
            raise ValueError("review case is missing the original proposal")
        decision = self._decision(
            status=PaymentAuthStatus.REFUSED,
            directive="REFUSE",
            proposal=proposal,
            authority=authority,
            payment_hash=case.payment_hash,
            now=current_time,
            expires_at=current_time,
            rationale=f"Dual-control denied by {approver_id}",
            review_case_id=case_id,
        )
        self._audit("review.denied", decision, approver_id, case_id)
        return decision

    @_atomic
    def resume_gather(
        self,
        case_id: str,
        authority: AuthorityState,
        fact_metadata: Optional[Dict[str, FactMeta]] = None,
        now: Optional[datetime] = None,
        auth_validity_seconds: int = 60,
        session_id: Optional[str] = None,
        actor_id: str = "system",
    ) -> AuthorizationDecision:
        case = self.cases.get_gather(case_id)
        if case is None or case.status is not CaseStatus.OPEN:
            raise ValueError("gather case is not open")
        proposal = self.cases.proposal_for(case.payment_hash)
        if proposal is None:
            raise ValueError("gather case is missing the original proposal")
        current_time = now or self.clock.now()
        if session_id is not None and session_id != case.session_id:
            raise ValueError("gather session cannot change")
        result = self.process_authorization(
            proposal,
            authority,
            auth_validity_seconds=auth_validity_seconds,
            session_id=case.session_id,
            fact_metadata=fact_metadata,
            now=current_time,
            actor_id=actor_id,
        )

        self.cases.mark_gather(case_id, CaseStatus.RESUMED, current_time)
        return result

    def _cached(self, payment_hash: str) -> Optional[AuthorizationDecision]:
        data = self.ledger.get_record("decisions", payment_hash)
        return AuthorizationDecision.model_validate(data) if data else None

    def _store(self, payment_hash: str, decision: AuthorizationDecision) -> None:
        self.ledger.put_record("decisions", payment_hash, decision.model_dump(mode="json"))

    def _audit(self, event_type: str, decision: AuthorizationDecision, actor_id: str, detail: str) -> None:
        self.audit.append(
            AuditEvent(
                event_id=f"aud_{uuid.uuid4().hex[:12]}",
                event_type=event_type,
                authorization_id=decision.authorization_id,
                payment_hash=decision.payment_hash,
                actor_id=actor_id,
                detail=detail,
            )
        )

    def _decision(
        self,
        *,
        status: PaymentAuthStatus,
        directive: str,
        proposal: PaymentProposal,
        authority: AuthorityState,
        payment_hash: str,
        now: datetime,
        expires_at: datetime,
        rationale: str,
        reservation_id: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        gather_fact_keys: Optional[List[str]] = None,
        review_case_id: Optional[str] = None,
        gather_case_id: Optional[str] = None,
    ) -> AuthorizationDecision:
        if proposal.mandate_expires_at is not None:
            expires_at = min(expires_at, proposal.mandate_expires_at)
        unsigned = AuthorizationDecision(
            authorization_id=f"auth_{uuid.uuid4().hex[:12]}",
            status=status,
            reasoning_directive=directive,
            reservation_id=reservation_id,
            created_at=now,
            expires_at=expires_at,
            payment_hash=payment_hash,
            authority_snapshot_hash=authority.compute_snapshot_hash(),
            mandate_hash=proposal.mandate_hash,
            policy_version=hashlib.sha256(json.dumps([p.model_dump(mode="json") for p in self.evaluator.policies], sort_keys=True).encode()).hexdigest(),
            rationale=rationale,
            allowed_tools=list(allowed_tools or []),
            gather_fact_keys=list(gather_fact_keys or []),
            review_case_id=review_case_id,
            gather_case_id=gather_case_id,
            issuer_id=self.signer.issuer_id,
        )
        return self.signer.sign(unsigned)
