"""Author and simulate PolicyRule documents."""

from __future__ import annotations

from typing import List

from prismthinker import PolicyRule

from prismagenticpay.core.evaluator import EvaluationOutcome, PrismThinkerPaymentEvaluator
from prismagenticpay.domain.models import AuthorityState, PaymentProposal
from prismagenticpay.policies.corporate import default_corporate_policies


class PolicyStudio:
    def __init__(self, policies: List[PolicyRule] | None = None):
        self.policies = list(policies or default_corporate_policies())

    def list(self) -> List[PolicyRule]:
        return list(self.policies)

    def upsert(self, rule: PolicyRule) -> PolicyRule:
        self.policies = [item for item in self.policies if item.id != rule.id] + [rule]
        return rule

    def delete(self, rule_id: str) -> bool:
        before = len(self.policies)
        self.policies = [item for item in self.policies if item.id != rule_id]
        return len(self.policies) != before

    def simulate(self, proposal: PaymentProposal, authority: AuthorityState) -> EvaluationOutcome:
        return PrismThinkerPaymentEvaluator(self.policies).evaluate_proposal(proposal, authority)
