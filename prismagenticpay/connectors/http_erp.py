"""Generic HTTP ERP reader used by SAP, NetSuite, and Coupa adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping

import httpx
from prismthinker import FactValue

from prismagenticpay.connectors.base import FactMeta
from prismagenticpay.connectors.protocol import FactBundle
from prismagenticpay.domain.models import PaymentProposal


class HttpErpConnector:
    def __init__(
        self,
        source: str,
        base_url: str,
        token: str,
        *,
        header_name: str = "Authorization",
        header_template: str = "Bearer {token}",
        path_template: str = "/vendors/{merchant_id}",
        mapping: Mapping[str, str] | None = None,
        ttl_seconds: int = 300,
        transport: httpx.BaseTransport | None = None,
    ):
        if not base_url or not token:
            raise ValueError(f"{source} base URL and token are required")
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.path_template = path_template
        self.mapping = dict(mapping or {
            "vendor_status": "vendor_status",
            "tier": "tier",
            "requires_dual_signature": "requires_dual_signature",
        })
        self.ttl_seconds = ttl_seconds
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=15.0,
            transport=transport,
            headers={header_name: header_template.format(token=token)},
        )

    def fetch(self, proposal: PaymentProposal, now: datetime | None = None) -> FactBundle:
        check = now or datetime.now(timezone.utc)
        path = self.path_template.format(
            merchant_id=proposal.merchant.merchant_id,
            principal_id=proposal.principal_id,
            mandate_id=proposal.mandate_id,
        )
        response = self._client.get(path)
        response.raise_for_status()
        payload = response.json()
        facts: Dict[str, FactValue] = {}
        metadata: Dict[str, FactMeta] = {}
        for fact_key, json_path in self.mapping.items():
            value = _read_path(payload, json_path)
            if value is None:
                continue
            facts[fact_key] = FactValue(key=fact_key, value=value)
            metadata[fact_key] = FactMeta(source=self.source, fetched_at=check, ttl_seconds=self.ttl_seconds)
        return FactBundle(facts, metadata)


def _read_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
