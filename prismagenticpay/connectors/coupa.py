from __future__ import annotations

import httpx

from prismagenticpay.connectors.http_erp import HttpErpConnector


class CoupaConnector(HttpErpConnector):
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            source="coupa",
            base_url=base_url,
            token=token,
            header_name="X-COUPA-API-KEY",
            header_template="{token}",
            path_template="/api/suppliers/{merchant_id}",
            mapping={
                "vendor_status": "status",
                "tier": "po-method",
                "requires_dual_signature": "requires-approval",
            },
            transport=transport,
        )
