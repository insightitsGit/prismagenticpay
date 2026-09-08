"""Locked FX quotes. Rates are never invented; the caller or a feed must supply them."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Dict, Optional

from pydantic import BaseModel, Field


class FxQuoteExpired(ValueError):
    pass


class FxQuote(BaseModel):
    quote_id: str
    base_currency: str
    quote_currency: str
    rate: str
    source: str
    as_of: datetime
    ttl_seconds: int = 60

    def decimal_rate(self) -> Decimal:
        rate = Decimal(self.rate)
        if rate <= 0:
            raise ValueError("FX rate must be positive")
        return rate

    def quote_hash(self) -> str:
        payload = {
            "quote_id": self.quote_id,
            "base": self.base_currency,
            "quote": self.quote_currency,
            "rate": self.rate,
            "source": self.source,
            "as_of": self.as_of.astimezone(timezone.utc).isoformat(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        check = now or datetime.now(timezone.utc)
        return (check - self.as_of).total_seconds() <= self.ttl_seconds

    def convert_cents(self, amount_cents: int, now: Optional[datetime] = None) -> int:
        if not self.is_fresh(now):
            raise FxQuoteExpired("FX quote exceeded TTL")
        converted = (Decimal(amount_cents) * self.decimal_rate()).to_integral_value(
            rounding=ROUND_HALF_EVEN
        )
        return int(converted)


class LockedRateBook:
    def __init__(self):
        self._quotes: Dict[str, FxQuote] = {}

    def put(self, quote: FxQuote) -> FxQuote:
        self._quotes[quote.quote_id] = quote
        return quote

    def get(self, quote_id: str) -> Optional[FxQuote]:
        return self._quotes.get(quote_id)

    def convert_to_home(
        self,
        amount_cents: int,
        currency: str,
        home_currency: str,
        quote: Optional[FxQuote],
        now: Optional[datetime] = None,
    ) -> int:
        if currency == home_currency:
            return amount_cents
        if quote is None:
            raise ValueError("FX quote is required when currencies differ")
        if quote.base_currency != currency or quote.quote_currency != home_currency:
            raise ValueError("FX quote pair does not match the proposal and home currency")
        return quote.convert_cents(amount_cents, now=now)
