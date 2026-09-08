"""Production process wiring from environment settings."""

from __future__ import annotations

from prismagenticpay.api.app import create_app
from prismagenticpay.config import Settings, get_settings
from prismagenticpay.connectors.coupa import CoupaConnector
from prismagenticpay.connectors.netsuite import NetSuiteConnector
from prismagenticpay.connectors.sap import SapConnector
from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.core.signing import DecisionSigner
from prismagenticpay.lifecycle.orchestrator import RailOrchestrator
from prismagenticpay.policies.corporate import default_corporate_policies
from prismagenticpay.rails.coinbase_rail import CoinbaseRail
from prismagenticpay.rails.iso8583 import Iso8583HostClient
from prismagenticpay.rails.stripe_rail import StripeRail
from prismagenticpay.state.sqlite_ledger import SqliteAuthorityLedger
from prismagenticpay.studio.policies import PolicyStudio
from prismagenticpay.trust.registry import TrustRegistry


def build_ledger(settings: Settings):
    path = settings.database_url
    if path.startswith("sqlite:///"):
        path = path[len("sqlite:///") :]
    elif path.startswith("postgres"):
        raise RuntimeError("Use SqliteAuthorityLedger or deploy Postgres via PAP_DATABASE_URL=sqlite for this build; Postgres DSN snapshot support is the sqlite file plus external failover")
    return SqliteAuthorityLedger(
        path,
        settings.session_budget_cents,
        settings.daily_budget_cents,
        settings.mandate_budget_cents,
        default_hold_ttl_seconds=settings.hold_ttl_seconds,
    )


def build_rails(settings: Settings) -> dict:
    rails = {}
    if settings.stripe_api_key:
        rails["stripe"] = StripeRail(settings.stripe_api_key, api_base=settings.stripe_api_base)
    if settings.coinbase_api_key:
        rails["coinbase"] = CoinbaseRail(settings.coinbase_api_key, api_base=settings.coinbase_api_base)
    if settings.iso8583_host:
        rails["iso8583"] = Iso8583HostClient(settings.iso8583_host)
    return rails


def build_connectors(settings: Settings) -> dict:
    connectors = {}
    if settings.sap_base_url and settings.sap_token:
        connectors["sap"] = SapConnector(settings.sap_base_url, settings.sap_token)
    if settings.netsuite_base_url and settings.netsuite_token:
        connectors["netsuite"] = NetSuiteConnector(settings.netsuite_base_url, settings.netsuite_token)
    if settings.coupa_base_url and settings.coupa_token:
        connectors["coupa"] = CoupaConnector(settings.coupa_base_url, settings.coupa_token)
    return connectors


def create_production_app(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.is_production and not settings.api_keys:
        raise RuntimeError("PAP_API_KEYS is required in production")
    ledger = build_ledger(settings)
    signer = (
        DecisionSigner.from_private_bytes(bytes.fromhex(settings.signing_seed_hex))
        if len(settings.signing_seed_hex) == 64
        else DecisionSigner.generate()
    )
    authorizer = PrismPaymentAuthorizer(ledger, default_corporate_policies(), signer=signer)
    gateway = SettlementGatewayHarness(ledger, signer=signer, allowed_rails=set(settings.allowed_rails))
    orchestrator = RailOrchestrator(ledger, gateway, build_rails(settings), signer, audit=authorizer.audit)
    return create_app(
        authorizer,
        gateway,
        settings=settings,
        orchestrator=orchestrator,
        studio=PolicyStudio(authorizer.evaluator.policies),
        trust=TrustRegistry.from_path(settings.trust_registry_path),
    )
