"""Reference corporate PolicyRule set for the v1.1.0 kernel."""

from __future__ import annotations

from typing import List

from prismthinker import DeonticModality, PolicyRule, RuleSeverity

CORPORATE_POLICY_VERSION = "1.1.0"
SETTLE_TOOL = "settle_payment_rail"


def default_corporate_policies() -> List[PolicyRule]:
    return [
        PolicyRule(
            id="vendor-blocked",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.vendor_status == blocked",
            severity=RuleSeverity.HARD_VETO,
            text="Blocked vendors cannot be paid.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="vendor-restricted",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.vendor_status == restricted",
            severity=RuleSeverity.CAUTION,
            text="Restricted vendors require human review.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="vendor-unknown",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.vendor_status == unknown",
            severity=RuleSeverity.CAUTION,
            text="Unknown vendor standing is not sufficient evidence to settle.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="dual-signature",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.requires_dual_signature == true",
            severity=RuleSeverity.CAUTION,
            text="Dual-signature payments must escalate to a second approver.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="session-cap",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.session_breach == true",
            severity=RuleSeverity.HARD_VETO,
            text="Amount exceeds remaining session capacity.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="daily-cap",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.daily_breach == true",
            severity=RuleSeverity.HARD_VETO,
            text="Amount exceeds remaining daily capacity.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="mandate-cap",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.mandate_breach == true",
            severity=RuleSeverity.HARD_VETO,
            text="Amount exceeds remaining mandate capacity.",
            action_name=SETTLE_TOOL,
        ),
        PolicyRule(
            id="transaction-cap",
            modality=DeonticModality.PROHIBITION,
            predicate="fact.transaction_breach == true",
            severity=RuleSeverity.HARD_VETO,
            text="Amount exceeds the per-transaction cap.",
            action_name=SETTLE_TOOL,
        ),
    ]
