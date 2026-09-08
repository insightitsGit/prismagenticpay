"""AP2 v0.2 closed payment mandate ingress.

Verifies:
- compact ES256 JWS, or SD-JWT with disclosures + optional key-binding JWT
- checkout_hash binding to the merchant checkout JWT
- cnf.jwk binding to the trusted payment public key when present
- optional open-mandate constraints (amount cap and category scope)
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
from pydantic import BaseModel

from prismagenticpay.domain.models import LineItem, MerchantIdentity, PaymentProposal

CLOSED_PAYMENT_VCT = "mandate.payment.1"
OPEN_PAYMENT_VCT = "mandate.payment.open.1"
SUPPORTED_ALGORITHMS = ("ES256",)


class AP2VerificationError(ValueError):
    pass


class AP2ClosedPaymentClaims(BaseModel):
    vct: str
    exp: int
    checkout_hash: str
    transaction_id: str
    amount_cents: int
    currency: str
    merchant_id: str
    merchant_name: str
    mcc: str
    domain: str
    category: str
    agent_id: str
    principal_id: str
    mandate_id: str
    nonce: str
    cnf: Optional[Dict[str, Any]] = None
    items: Optional[List[Dict[str, Any]]] = None
    sd_hash: Optional[str] = None


class AP2OpenMandateClaims(BaseModel):
    vct: str
    exp: int
    max_amount_cents: int
    categories: List[str] = []
    cnf: Optional[Dict[str, Any]] = None


class AP2IngressAdapter:
    def __init__(
        self,
        *,
        payment_public_key: Any,
        checkout_public_key: Optional[Any] = None,
        open_public_key: Optional[Any] = None,
        require_agent_binding: bool = False,
    ):
        if payment_public_key is None:
            raise AP2VerificationError("payment_public_key is required")
        self.payment_public_key = payment_public_key
        self.checkout_public_key = checkout_public_key
        self.open_public_key = open_public_key
        self.require_agent_binding = require_agent_binding

    def verify_and_project(
        self,
        payment_token: str,
        checkout_jwt: str,
        now: Optional[datetime] = None,
        open_mandate_jwt: Optional[str] = None,
    ) -> PaymentProposal:
        claims = self.verify(
            payment_token, checkout_jwt, now=now, open_mandate_jwt=open_mandate_jwt
        )
        return self.to_proposal(claims, payment_token)

    def verify(
        self,
        payment_token: str,
        checkout_jwt: str,
        now: Optional[datetime] = None,
        open_mandate_jwt: Optional[str] = None,
    ) -> AP2ClosedPaymentClaims:
        if not checkout_jwt or checkout_jwt.count(".") != 2:
            raise AP2VerificationError("checkout JWT must be a compact JWS")

        current_time = now or datetime.now(timezone.utc)
        issuer_jwt, payload = self._decode_payment_token(payment_token, current_time)
        try:
            claims = AP2ClosedPaymentClaims.model_validate(payload)
        except Exception as exc:
            raise AP2VerificationError(f"closed payment claims are incomplete: {exc}") from exc

        if claims.vct != CLOSED_PAYMENT_VCT:
            raise AP2VerificationError(f"unsupported vct {claims.vct!r}")

        expected_checkout_hash = hashlib.sha256(checkout_jwt.encode("utf-8")).hexdigest()
        if claims.checkout_hash != expected_checkout_hash:
            raise AP2VerificationError("checkout_hash does not match the presented checkout JWT")

        if self.checkout_public_key is not None:
            self._decode_signed(checkout_jwt, self.checkout_public_key, current_time, "checkout JWT")

        self._assert_cnf(claims.cnf)
        if open_mandate_jwt:
            self._assert_open_constraints(open_mandate_jwt, claims, current_time)
        return claims

    def to_proposal(self, claims: AP2ClosedPaymentClaims, payment_token: str) -> PaymentProposal:
        issuer_jwt = payment_token.split("~", 1)[0]
        mandate_hash = hashlib.sha256(issuer_jwt.encode("utf-8")).hexdigest()
        items = []
        for raw in claims.items or []:
            items.append(
                LineItem(
                    sku=str(raw.get("sku", "item")),
                    name=str(raw.get("name", "item")),
                    amount_cents=int(raw["amount_cents"]),
                    category=str(raw.get("category", claims.category)),
                )
            )
        return PaymentProposal(
            transaction_id=claims.transaction_id,
            amount_cents=claims.amount_cents,
            currency=claims.currency,
            merchant=MerchantIdentity(
                merchant_id=claims.merchant_id,
                merchant_name=claims.merchant_name,
                mcc=claims.mcc,
                domain=claims.domain,
            ),
            category=claims.category,
            line_items=items,
            agent_id=claims.agent_id,
            principal_id=claims.principal_id,
            mandate_id=claims.mandate_id,
            mandate_type="closed_payment",
            mandate_hash=mandate_hash,
            idempotency_nonce=claims.nonce,
        )

    def _decode_payment_token(self, token: str, now: datetime) -> tuple[str, Mapping[str, Any]]:
        if "~" in token:
            return self._decode_sd_jwt(token, now)
        if token.count(".") != 2:
            raise AP2VerificationError("payment mandate must be a compact JWS or SD-JWT")
        payload = self._decode_signed(token, self.payment_public_key, now, "closed payment mandate")
        return token, payload

    def _decode_sd_jwt(self, token: str, now: datetime) -> tuple[str, Mapping[str, Any]]:
        parts = token.split("~")
        if len(parts) < 2:
            raise AP2VerificationError("SD-JWT is malformed")
        issuer_jwt = parts[0]
        kb_jwt = parts[-1] if parts[-1] and parts[-1].count(".") == 2 else ""
        disclosures = [part for part in parts[1:-1] if part] if kb_jwt else [part for part in parts[1:] if part]
        payload = dict(
            self._decode_signed(issuer_jwt, self.payment_public_key, now, "closed payment SD-JWT")
        )
        digest_set = set(payload.get("_sd") or [])
        for disclosure in disclosures:
            digest = _b64url(hashlib.sha256(disclosure.encode("ascii")).digest())
            if digest not in digest_set:
                raise AP2VerificationError("SD-JWT disclosure digest is not in _sd")
            decoded = json.loads(_b64url_decode(disclosure))
            if not isinstance(decoded, list) or len(decoded) != 3:
                raise AP2VerificationError("SD-JWT disclosure must be [salt, name, value]")
            payload[str(decoded[1])] = decoded[2]
        payload.pop("_sd", None)
        if kb_jwt:
            presentation = issuer_jwt + "~" + "~".join(disclosures) + "~"
            expected = _b64url(hashlib.sha256(presentation.encode("ascii")).digest())
            kb_payload = self._decode_signed(kb_jwt, self.payment_public_key, now, "key-binding JWT")
            if kb_payload.get("sd_hash") != expected:
                raise AP2VerificationError("key-binding sd_hash does not match the SD-JWT presentation")
        return issuer_jwt, payload

    def _assert_cnf(self, cnf: Optional[Dict[str, Any]]) -> None:
        if cnf is None:
            if self.require_agent_binding:
                raise AP2VerificationError("cnf agent key binding is required")
            return
        jwk = cnf.get("jwk")
        if not jwk:
            raise AP2VerificationError("cnf.jwk is required when cnf is present")
        presented = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
        if not _public_keys_equal(presented, self.payment_public_key):
            raise AP2VerificationError("cnf.jwk does not match the trusted payment key")

    def _assert_open_constraints(
        self,
        open_mandate_jwt: str,
        closed: AP2ClosedPaymentClaims,
        now: datetime,
    ) -> None:
        key = self.open_public_key or self.payment_public_key
        payload = self._decode_signed(open_mandate_jwt, key, now, "open payment mandate")
        try:
            open_claims = AP2OpenMandateClaims.model_validate(payload)
        except Exception as exc:
            raise AP2VerificationError(f"open mandate claims are incomplete: {exc}") from exc
        if open_claims.vct != OPEN_PAYMENT_VCT:
            raise AP2VerificationError(f"unsupported open vct {open_claims.vct!r}")
        if closed.amount_cents > open_claims.max_amount_cents:
            raise AP2VerificationError("closed amount exceeds open mandate max_amount_cents")
        if open_claims.categories and closed.category not in open_claims.categories:
            raise AP2VerificationError("closed category is outside open mandate scope")
        if open_claims.cnf:
            self._assert_cnf(open_claims.cnf)

    @staticmethod
    def _decode_signed(token: str, key: Any, now: datetime, label: str) -> Mapping[str, Any]:
        try:
            payload = jwt.decode(
                token,
                key=key,
                algorithms=list(SUPPORTED_ALGORITHMS),
                options={"require": ["exp"], "verify_exp": False},
            )
        except jwt.InvalidTokenError as exc:
            raise AP2VerificationError(f"{label} signature failed: {exc}") from exc
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp <= int(now.timestamp()):
            raise AP2VerificationError(f"{label} is expired")
        return payload


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _public_keys_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, EllipticCurvePublicKey) or not isinstance(right, EllipticCurvePublicKey):
        return False
    return left.public_numbers() == right.public_numbers()
