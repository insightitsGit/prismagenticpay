"""Canonical, protocol-agnostic domain models for PrismAgenticPay."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MCC_RE = re.compile(r"^\d{4}$")


class MerchantIdentity(BaseModel):
    merchant_id: str
    merchant_name: str
    mcc: str  # ISO 18245 Merchant Category Code (e.g. '5734')
    domain: str

    @field_validator("merchant_id", "merchant_name", "domain")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("merchant fields must be non-empty")
        return value.strip()

    @field_validator("mcc")
    @classmethod
    def _mcc(cls, value: str) -> str:
        if not _MCC_RE.match(value):
            raise ValueError("mcc must be a 4-digit ISO 18245 code")
        return value


class LineItem(BaseModel):
    sku: str
    name: str
    amount_cents: int
    category: str

    @field_validator("amount_cents")
    @classmethod
    def _positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("line item amount_cents must be positive")
        return value

    def canonical(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "amount_cents": self.amount_cents,
            "category": self.category,
        }


class PaymentProposal(BaseModel):
    transaction_id: str
    amount_cents: int
    currency: str = "USD"
    merchant: MerchantIdentity
    category: str
    line_items: List[LineItem] = Field(default_factory=list)

    agent_id: str
    principal_id: str

    mandate_id: str
    mandate_type: str  # "closed_checkout" or "closed_payment"
    mandate_hash: str

    mandate_expires_at: Optional[datetime] = None
    requested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    idempotency_nonce: str

    @field_validator("amount_cents")
    @classmethod
    def _positive_cents(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("amount_cents must be a positive integer")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _CURRENCY_RE.match(normalized):
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return normalized

    @field_validator(
        "transaction_id",
        "category",
        "agent_id",
        "principal_id",
        "mandate_id",
        "mandate_hash",
        "idempotency_nonce",
    )
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("required text field is empty")
        return value.strip()

    @field_validator("mandate_type")
    @classmethod
    def _mandate_type(cls, value: str) -> str:
        allowed = {"closed_checkout", "closed_payment"}
        if value not in allowed:
            raise ValueError(f"mandate_type must be one of {sorted(allowed)}")
        return value

    def amount_decimal(self) -> Decimal:
        return (Decimal(self.amount_cents) / Decimal(100)).quantize(Decimal("0.01"))

    def compute_canonical_payment_hash(self) -> str:
        """Deterministic SHA-256 fingerprint of the exact financial mutation."""
        canonical = {
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "merchant_id": self.merchant.merchant_id,
            "mcc": self.merchant.mcc,
            "category": self.category,
            "line_items": [item.canonical() for item in self.line_items],
            "agent_id": self.agent_id,
            "principal_id": self.principal_id,
            "mandate_hash": self.mandate_hash,
            "mandate_id": self.mandate_id,
            "mandate_expires_at": self.mandate_expires_at.isoformat() if self.mandate_expires_at else None,
            "nonce": self.idempotency_nonce,
        }
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class AuthorityState(BaseModel):
    """Enterprise authority snapshot spanning multiple budgetary buckets."""

    transaction_cap_cents: int
    session_remaining_cents: int
    daily_remaining_cents: int
    mandate_remaining_cents: int

    vendor_status: str
    tier: str
    requires_dual_signature: bool = False

    snapshot_taken_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator(
        "transaction_cap_cents",
        "session_remaining_cents",
        "daily_remaining_cents",
        "mandate_remaining_cents",
    )
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("budget fields must be non-negative integers")
        return value

    @field_validator("vendor_status")
    @classmethod
    def _vendor_status(cls, value: str) -> str:
        allowed = {"approved", "blocked", "restricted", "unknown"}
        if value not in allowed:
            raise ValueError(f"vendor_status must be one of {sorted(allowed)}")
        return value

    def compute_snapshot_hash(self) -> str:
        raw = (
            f"{self.transaction_cap_cents}:{self.session_remaining_cents}:"
            f"{self.daily_remaining_cents}:{self.mandate_remaining_cents}:"
            f"{self.vendor_status}:{self.tier}:{self.requires_dual_signature}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PaymentAuthStatus(str, Enum):
    AUTHORIZED = "AUTHORIZED"
    REFUSED = "REFUSED"
    REVIEW = "REVIEW"
    STALLED = "STALLED"


class AuthorizationDecision(BaseModel):
    authorization_id: str
    status: PaymentAuthStatus
    reasoning_directive: str
    reservation_id: Optional[str] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime

    payment_hash: str
    authority_snapshot_hash: str
    mandate_hash: str
    policy_version: str

    rationale: str
    allowed_tools: List[str] = Field(default_factory=list)
    gather_fact_keys: List[str] = Field(default_factory=list)
    review_case_id: Optional[str] = None
    gather_case_id: Optional[str] = None
    issuer_id: str = "prismagenticpay"
    signature: Optional[str] = None

    @field_validator("reasoning_directive")
    @classmethod
    def _directive(cls, value: str) -> str:
        allowed = {"EXECUTE", "REFUSE", "ESCALATE", "GATHER"}
        normalized = value.strip().upper()
        if normalized not in allowed:
            raise ValueError(f"reasoning_directive must be one of {sorted(allowed)}")
        return normalized

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def is_valid_for_settlement(
        self,
        actual_payment_hash: str,
        now: Optional[datetime] = None,
        *,
        mandate_hash: Optional[str] = None,
        authority_snapshot_hash: Optional[str] = None,
    ) -> bool:
        check_time = now or datetime.now(timezone.utc)
        if self.status != PaymentAuthStatus.AUTHORIZED:
            return False
        if check_time < self.created_at or check_time >= self.expires_at:
            return False
        if actual_payment_hash != self.payment_hash:
            return False
        if mandate_hash is not None and mandate_hash != self.mandate_hash:
            return False
        if (
            authority_snapshot_hash is not None
            and authority_snapshot_hash != self.authority_snapshot_hash
        ):
            return False
        return True
