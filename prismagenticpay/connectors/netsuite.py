from __future__ import annotations

import httpx

from prismagenticpay.connectors.http_erp import HttpErpConnector


class NetSuiteConnector(HttpErpConnector):
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            source="netsuite",
            base_url=base_url,
            token=token,
            header_name="Authorization",
            header_template="Bearer {token}",
            path_template="/services/rest/record/v1/vendor/{merchant_id}",
            mapping={
                "vendor_status": "custentity_vendor_status",
                "tier": "custentity_vendor_tier",
                "requires_dual_signature": "custentity_dual_control",
            },
            transport=transport,
        )
