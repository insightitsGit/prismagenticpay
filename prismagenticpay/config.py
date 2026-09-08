"""Environment-driven production settings. Secrets never have defaults."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import List

from pydantic import BaseModel, Field


def _split(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseModel):
    app_name: str = "PrismAgenticPay"
    environment: str = "development"
    log_level: str = "INFO"
    home_currency: str = "USD"

    session_budget_cents: int = 1_000_000
    daily_budget_cents: int = 5_000_000
    mandate_budget_cents: int = 2_000_000
    hold_ttl_seconds: int = 120

    api_keys: List[str] = Field(default_factory=list)
    allowed_rails: List[str] = Field(default_factory=lambda: ["stripe"])
    rate_limit_per_minute: int = 120

    database_url: str = "sqlite:///./prismagenticpay.sqlite"
    redis_url: str = ""

    stripe_api_key: str = ""
    stripe_api_base: str = "https://api.stripe.com"
    coinbase_api_key: str = ""
    coinbase_api_base: str = "https://api.commerce.coinbase.com"
    iso8583_host: str = ""

    sap_base_url: str = ""
    sap_token: str = ""
    netsuite_base_url: str = ""
    netsuite_token: str = ""
    coupa_base_url: str = ""
    coupa_token: str = ""

    identity_registry_path: str = ""
    authority_registry_path: str = ""
    trust_registry_path: str = ""
    signing_seed_hex: str = ""
    cors_origins: List[str] = Field(default_factory=list)

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("PAP_APP_NAME", "PrismAgenticPay"),
            environment=os.getenv("PAP_ENV", "development"),
            log_level=os.getenv("PAP_LOG_LEVEL", "INFO"),
            home_currency=os.getenv("PAP_HOME_CURRENCY", "USD"),
            session_budget_cents=int(os.getenv("PAP_SESSION_BUDGET_CENTS", "1000000")),
            daily_budget_cents=int(os.getenv("PAP_DAILY_BUDGET_CENTS", "5000000")),
            mandate_budget_cents=int(os.getenv("PAP_MANDATE_BUDGET_CENTS", "2000000")),
            hold_ttl_seconds=int(os.getenv("PAP_HOLD_TTL_SECONDS", "120")),
            api_keys=_split(os.getenv("PAP_API_KEYS", "")),
            allowed_rails=_split(os.getenv("PAP_ALLOWED_RAILS", "stripe")),
            rate_limit_per_minute=int(os.getenv("PAP_RATE_LIMIT_PER_MINUTE", "120")),
            database_url=os.getenv("PAP_DATABASE_URL", "sqlite:///./prismagenticpay.sqlite"),
            redis_url=os.getenv("PAP_REDIS_URL", ""),
            stripe_api_key=os.getenv("STRIPE_API_KEY", ""),
            stripe_api_base=os.getenv("STRIPE_API_BASE", "https://api.stripe.com"),
            coinbase_api_key=os.getenv("COINBASE_COMMERCE_API_KEY", ""),
            coinbase_api_base=os.getenv("COINBASE_COMMERCE_API_BASE", "https://api.commerce.coinbase.com"),
            iso8583_host=os.getenv("PAP_ISO8583_HOST", ""),
            sap_base_url=os.getenv("PAP_SAP_BASE_URL", ""),
            sap_token=os.getenv("PAP_SAP_TOKEN", ""),
            netsuite_base_url=os.getenv("PAP_NETSUITE_BASE_URL", ""),
            netsuite_token=os.getenv("PAP_NETSUITE_TOKEN", ""),
            coupa_base_url=os.getenv("PAP_COUPA_BASE_URL", ""),
            coupa_token=os.getenv("PAP_COUPA_TOKEN", ""),
            identity_registry_path=os.getenv("PAP_IDENTITY_REGISTRY_PATH", ""),
            authority_registry_path=os.getenv("PAP_AUTHORITY_REGISTRY_PATH", ""),
            trust_registry_path=os.getenv("PAP_TRUST_REGISTRY_PATH", ""),
            signing_seed_hex=os.getenv("PAP_SIGNING_SEED_HEX", ""),
            cors_origins=_split(os.getenv("PAP_CORS_ORIGINS", "")),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
