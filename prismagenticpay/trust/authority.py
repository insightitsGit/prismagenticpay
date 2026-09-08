"""Server-owned authority snapshots, optionally refreshed from a configured ERP."""
import json
from pathlib import Path
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from prismagenticpay.domain.models import AuthorityState
from prismagenticpay.connectors.base import FactMeta
from prismagenticpay.core.evaluator import PrismThinkerPaymentEvaluator


class AuthorityGrant(BaseModel):
    principal_id: str
    agent_id: str
    mandate_id: str
    authority: AuthorityState
    ttl_seconds: int = Field(default=300, gt=0)
    connector: str | None = None


class AuthorityResolver:
    def __init__(self, path, connectors=None):
        self.path = path
        self.connectors = connectors or {}
        self._grants()

    def _grants(self):
        records = json.loads(Path(self.path).read_text(encoding="utf-8"))
        return [AuthorityGrant.model_validate(item) for item in records["grants"]]

    def resolve(self, proposal, now=None):
        now = now or datetime.now(timezone.utc)
        grants = self._grants()
        matching = [g for g in grants if (g.principal_id, g.agent_id, g.mandate_id) ==
                    (proposal.principal_id, proposal.agent_id, proposal.mandate_id)]
        if len(matching) != 1:
            raise ValueError("no unique server-owned authority grant")
        grant = matching[0]
        authority = grant.authority
        metadata = {
            key: FactMeta(source="authority_grant", fetched_at=authority.snapshot_taken_at, ttl_seconds=grant.ttl_seconds)
            for key in PrismThinkerPaymentEvaluator._structured_facts(proposal, authority)
        }
        # These facts originate in the already-verified immutable payment mandate.
        for key in ("amount_cents", "currency", "mcc", "category", "merchant_id", "agent_id", "principal_id"):
            metadata[key] = FactMeta(source="verified_mandate", fetched_at=now, ttl_seconds=60)
        if grant.connector:
            connector = self.connectors.get(grant.connector)
            if connector is None:
                raise ValueError("configured authority connector unavailable")
            # Missing connector fields are withheld, never replaced by optimistic defaults.
            bundle = connector.fetch(proposal, now=now)
            updates = {}
            for field, fact_key in [("vendor_status", "vendor_status"), ("tier", "user_tier"),
                                    ("requires_dual_signature", "requires_dual_signature")]:
                metadata.pop(fact_key, None)
                if field in bundle.raw_facts and field in bundle.metadata:
                    updates[field] = bundle.raw_facts[field].value
                    metadata[fact_key] = bundle.metadata[field]
            authority = AuthorityState.model_validate({**authority.model_dump(), **updates})
        return authority, metadata
