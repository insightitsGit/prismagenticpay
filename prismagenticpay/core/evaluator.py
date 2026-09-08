"""PrismThinker v1.1 bridge: ReasoningContext in, to_chorusgraph() directive out."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from prismthinker import (
    ActionKind,
    CandidateAction,
    DecisionGraph,
    FactSpec,
    FactType,
    FactValue,
    Hypothesis,
    PolicyRule,
    PrismThinker,
    ReasoningContext,
)
from prismthinker.adapters.chorusgraph import to_chorusgraph
from prismthinker.core.schemas import ChorusGraphDirective, EpistemicRegime

from prismagenticpay.connectors.base import FactMeta, FactTtlValidator
from prismagenticpay.domain.models import AuthorityState, PaymentProposal
from prismagenticpay.policies.corporate import SETTLE_TOOL

_SETTLE_TOOLS = [SETTLE_TOOL]
_DIRECTIVE_ALIASES = {
    ChorusGraphDirective.EXECUTE: "EXECUTE",
    ChorusGraphDirective.REFUSE: "REFUSE",
    ChorusGraphDirective.ESCALATE: "ESCALATE",
    ChorusGraphDirective.GATHER: "GATHER",
    ChorusGraphDirective.ANSWER: "REFUSE",
}


class EvaluationOutcome(BaseModel):
    directive: str
    decision_graph: DecisionGraph
    allowed_tools: List[str] = Field(default_factory=list)
    gather_fact_keys: List[str] = Field(default_factory=list)


class PrismThinkerPaymentEvaluator:
    def __init__(self, policies: List[PolicyRule]):
        if not policies:
            raise ValueError("at least one PolicyRule is required")
        self.policies = list(policies)
        self.engine = PrismThinker()

    def evaluate_proposal(
        self,
        proposal: PaymentProposal,
        authority: AuthorityState,
        fact_metadata: Optional[Dict[str, FactMeta]] = None,
        now: Optional[datetime] = None,
    ) -> EvaluationOutcome:
        check_time = now or datetime.now(timezone.utc)
        facts = self._structured_facts(proposal, authority)
        specs = self._fact_specs()
        metadata = fact_metadata or {}
        facts = FactTtlValidator.filter_fresh_facts(facts, metadata, now=check_time)

        context = ReasoningContext(
            query=(
                f"Allow corporate statutory disbursement for tx {proposal.transaction_id} "
                f"only if policy permits"
            ),
            hypothesis=Hypothesis(
                id=f"hyp_{proposal.transaction_id}",
                statement=(
                    f"Authorize {proposal.currency} {proposal.amount_decimal()} "
                    f"payment to {proposal.merchant.merchant_name}"
                ),
                action=CandidateAction(
                    id=f"act_{proposal.transaction_id}",
                    kind=ActionKind.TOOL_INVOCATION,
                    name=SETTLE_TOOL,
                    payload={
                        "payment_hash": proposal.compute_canonical_payment_hash(),
                        "amount_cents": proposal.amount_cents,
                        "currency": proposal.currency,
                        "merchant_id": proposal.merchant.merchant_id,
                        "mcc": proposal.merchant.mcc,
                    },
                ),
            ),
            structured_facts=facts,
            fact_specs=specs,
            policy_rules=self.policies,
            domain="finance",
            force_regime=EpistemicRegime.POLICY_NORMATIVE,
        )

        decision_graph = self.engine.evaluate(context)
        envelope = to_chorusgraph(decision_graph, allowed_tools=list(_SETTLE_TOOLS))
        directive = _DIRECTIVE_ALIASES.get(envelope.directive, "REFUSE")
        return EvaluationOutcome(
            directive=directive,
            decision_graph=decision_graph,
            allowed_tools=list(envelope.allowed_tools),
            gather_fact_keys=list(envelope.gather_fact_keys),
        )

    @staticmethod
    def _structured_facts(
        proposal: PaymentProposal,
        authority: AuthorityState,
    ) -> Dict[str, FactValue]:
        return {
            "amount_cents": FactValue(key="amount_cents", value=proposal.amount_cents),
            "currency": FactValue(key="currency", value=proposal.currency),
            "mcc": FactValue(key="mcc", value=proposal.merchant.mcc),
            "category": FactValue(key="category", value=proposal.category),
            "merchant_id": FactValue(key="merchant_id", value=proposal.merchant.merchant_id),
            "agent_id": FactValue(key="agent_id", value=proposal.agent_id),
            "principal_id": FactValue(key="principal_id", value=proposal.principal_id),
            "vendor_status": FactValue(key="vendor_status", value=authority.vendor_status),
            "user_tier": FactValue(key="user_tier", value=authority.tier),
            "requires_dual_signature": FactValue(
                key="requires_dual_signature", value=authority.requires_dual_signature
            ),
            "transaction_cap_cents": FactValue(
                key="transaction_cap_cents", value=authority.transaction_cap_cents
            ),
            "session_remaining_cents": FactValue(
                key="session_remaining_cents", value=authority.session_remaining_cents
            ),
            "daily_remaining_cents": FactValue(
                key="daily_remaining_cents", value=authority.daily_remaining_cents
            ),
            "mandate_remaining_cents": FactValue(
                key="mandate_remaining_cents", value=authority.mandate_remaining_cents
            ),
            "session_breach": FactValue(
                key="session_breach",
                value=proposal.amount_cents > authority.session_remaining_cents,
            ),
            "daily_breach": FactValue(
                key="daily_breach",
                value=proposal.amount_cents > authority.daily_remaining_cents,
            ),
            "mandate_breach": FactValue(
                key="mandate_breach",
                value=proposal.amount_cents > authority.mandate_remaining_cents,
            ),
            "transaction_breach": FactValue(
                key="transaction_breach",
                value=proposal.amount_cents > authority.transaction_cap_cents,
            ),
        }

    @staticmethod
    def _fact_specs() -> Dict[str, FactSpec]:
        return {
            "amount_cents": FactSpec(
                key="amount_cents", fact_type=FactType.INT, required=True, mutable=False, minimum=1
            ),
            "currency": FactSpec(key="currency", fact_type=FactType.STRING, required=True),
            "mcc": FactSpec(key="mcc", fact_type=FactType.STRING, required=True),
            "category": FactSpec(key="category", fact_type=FactType.STRING, required=True),
            "merchant_id": FactSpec(key="merchant_id", fact_type=FactType.STRING, required=True),
            "agent_id": FactSpec(key="agent_id", fact_type=FactType.STRING, required=True),
            "principal_id": FactSpec(key="principal_id", fact_type=FactType.STRING, required=True),
            "vendor_status": FactSpec(
                key="vendor_status",
                fact_type=FactType.ENUM,
                required=True,
                enum_values=["approved", "blocked", "restricted", "unknown"],
            ),
            "user_tier": FactSpec(key="user_tier", fact_type=FactType.STRING, required=True),
            "requires_dual_signature": FactSpec(
                key="requires_dual_signature", fact_type=FactType.BOOL, required=True
            ),
            "transaction_cap_cents": FactSpec(
                key="transaction_cap_cents", fact_type=FactType.INT, required=True, minimum=0
            ),
            "session_remaining_cents": FactSpec(
                key="session_remaining_cents",
                fact_type=FactType.INT,
                required=True,
                mutable=True,
                minimum=0,
            ),
            "daily_remaining_cents": FactSpec(
                key="daily_remaining_cents",
                fact_type=FactType.INT,
                required=True,
                mutable=True,
                minimum=0,
            ),
            "mandate_remaining_cents": FactSpec(
                key="mandate_remaining_cents",
                fact_type=FactType.INT,
                required=True,
                mutable=True,
                minimum=0,
            ),
            "session_breach": FactSpec(key="session_breach", fact_type=FactType.BOOL, required=True),
            "daily_breach": FactSpec(key="daily_breach", fact_type=FactType.BOOL, required=True),
            "mandate_breach": FactSpec(key="mandate_breach", fact_type=FactType.BOOL, required=True),
            "transaction_breach": FactSpec(
                key="transaction_breach", fact_type=FactType.BOOL, required=True
            ),
        }

    @staticmethod
    def _fresh_metadata(facts: Dict[str, FactValue], now: datetime) -> Dict[str, FactMeta]:
        return {
            key: FactMeta(source="authority_snapshot", fetched_at=now, ttl_seconds=300)
            for key in facts
        }
