"""ISO 8583-1987 authorization packer and host client.

This codec builds and parses 0100/0110 messages. It does not impersonate a
card network. The host URL is a configured switch or lab gateway.
PAN/track data are rejected: only tokens and already-scoped credentials may
appear in DE 2 as a token alias.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

from prismagenticpay.domain.models import AuthorizationDecision, PaymentProposal

# ISO 8583-1987 data-element encodings used by this codec.
# F = fixed numeric, V = LLVAR numeric/alphanumeric.
_FIELDS = {
    2: ("V", 19),
    3: ("F", 6),
    4: ("F", 12),
    7: ("F", 10),
    11: ("F", 6),
    18: ("F", 4),
    37: ("F", 12),
    39: ("F", 2),
    41: ("F", 8),
    42: ("F", 15),
    49: ("F", 3),
}


class Iso8583Error(ValueError):
    pass


def _bitmap(fields: Dict[int, str]) -> str:
    bits = ["0"] * 64
    for field in fields:
        if field < 1 or field > 64:
            raise Iso8583Error(f"primary bitmap only supports DE 1-64, got {field}")
        bits[field - 1] = "1"
    return "".join(bits)


def pack(mti: str, fields: Dict[int, str]) -> str:
    if len(mti) != 4 or not mti.isdigit():
        raise Iso8583Error("MTI must be 4 digits")
    body: list[str] = [mti, _bitmap(fields)]
    for field in sorted(fields):
        if field not in _FIELDS:
            raise Iso8583Error(f"unsupported data element {field}")
        kind, size = _FIELDS[field]
        value = fields[field]
        if kind == "F":
            if not value.isdigit() and field in {3, 4, 7, 11, 18, 49}:
                raise Iso8583Error(f"DE {field} must be numeric")
            body.append(value.rjust(size, "0")[:size] if value.isdigit() else value.ljust(size)[:size])
        else:
            if len(value) > size:
                raise Iso8583Error(f"DE {field} exceeds max length {size}")
            if field == 2 and value.isdigit() and 13 <= len(value) <= 19:
                raise Iso8583Error("raw PAN is not permitted; supply a payment token")
            body.append(f"{len(value):02d}{value}")
    return "".join(body)


def unpack(message: str) -> tuple[str, Dict[int, str]]:
    if len(message) < 68:
        raise Iso8583Error("message too short")
    mti = message[:4]
    bitmap = message[4:68]
    cursor = 68
    fields: Dict[int, str] = {}
    for index, bit in enumerate(bitmap, start=1):
        if bit != "1":
            continue
        if index not in _FIELDS:
            raise Iso8583Error(f"unsupported data element {index} set in bitmap")
        kind, size = _FIELDS[index]
        if kind == "F":
            fields[index] = message[cursor : cursor + size]
            cursor += size
        else:
            length = int(message[cursor : cursor + 2])
            cursor += 2
            fields[index] = message[cursor : cursor + length]
            cursor += length
    return mti, fields


def authorization_fields(
    proposal: PaymentProposal,
    stan: str,
    *,
    now: Optional[datetime] = None,
    payment_token: str,
    terminal_id: str = "PAPTERM1",
    merchant_id: str = "PAPMERCHANT0001",
) -> Dict[int, str]:
    if payment_token.isdigit() and 13 <= len(payment_token) <= 19:
        raise Iso8583Error("raw PAN is not permitted")
    stamp = (now or datetime.now(timezone.utc)).strftime("%m%d%H%M%S")
    rrn = (proposal.transaction_id.replace("-", "") + "000000000000")[:12]
    return {
        2: payment_token[:19],
        3: "000000",
        4: f"{proposal.amount_cents:012d}",
        7: stamp,
        11: stan.zfill(6)[-6:],
        18: proposal.merchant.mcc,
        37: rrn,
        41: terminal_id[:8].ljust(8),
        42: merchant_id[:15].ljust(15),
        49: "840" if proposal.currency == "USD" else proposal.currency,
    }


class Iso8583HostClient:
    def __init__(self, host: str, timeout_seconds: float = 10.0, transport: httpx.BaseTransport | None = None):
        if not host:
            raise Iso8583Error("ISO 8583 host is not configured")
        self.host = host.rstrip("/")
        self._client = httpx.Client(timeout=timeout_seconds, transport=transport)

    def authorize(
        self,
        proposal: PaymentProposal,
        decision: AuthorizationDecision,
        *,
        payment_token: str,
        stan: str,
        now: Optional[datetime] = None,
    ) -> dict:
        request = pack("0100", authorization_fields(proposal, stan, now=now, payment_token=payment_token))
        response = self._client.post(
            f"{self.host}/iso8583",
            content=request,
            headers={"Content-Type": "text/plain", "X-Payment-Hash": decision.payment_hash},
        )
        response.raise_for_status()
        mti, fields = unpack(response.text)
        if mti != "0110":
            raise Iso8583Error(f"expected MTI 0110, got {mti}")
        return {"mti": mti, "fields": fields, "approved": fields.get(39) == "00"}
