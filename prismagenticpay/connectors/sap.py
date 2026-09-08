from __future__ import annotations

import httpx

from prismagenticpay.connectors.http_erp import HttpErpConnector


class SapConnector(HttpErpConnector):
    def __init__(
        self,
        base_url: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ):
        super().__init__(
            source="sap",
            base_url=base_url,
            token=token,
            header_name="Authorization",
            header_template="Bearer {token}",
            path_template="/sap/opu/odata/sap/API_BUSINESS_PARTNER/vendors/{merchant_id}",
            mapping={
                "vendor_status": "d.VendorStatus",
                "tier": "d.BusinessPartnerGrouping",
                "requires_dual_signature": "d.DualControlRequired",
            },
            transport=transport,
        )
