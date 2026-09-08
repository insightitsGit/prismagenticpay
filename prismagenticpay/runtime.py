"""Production process wiring from environment settings."""

from __future__ import annotations

import hashlib
from cryptography.hazmat.primitives.asymmetric import ec
from prismthinker import PolicyRule
from prismagenticpay.trust.authority import AuthorityResolver

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
from prismagenticpay.rails.stripe_rail import StripeRail
from prismagenticpay.state.sqlite_ledger import SqliteAuthorityLedger
from prismagenticpay.studio.policies import PolicyStudio
from prismagenticpay.trust.registry import TrustRegistry


def build_ledger(settings: Settings):
    path = settings.database_url
    if path.startswith("sqlite:///"):
        path = path[len("sqlite:///") :]
    elif path.startswith("postgres"):
        raise RuntimeError("PostgreSQL is not supported; configure a local sqlite:/// database")
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
    if settings.coinbase_api_key or settings.iso8583_host or set(settings.allowed_rails) - {"stripe"}:
        raise RuntimeError("only Stripe settlement is supported; Coinbase and ISO authorization are experimental")
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
    if settings.is_production:
        try:
            seed = bytes.fromhex(settings.signing_seed_hex)
        except ValueError as exc:
            raise RuntimeError("PAP_SIGNING_SEED_HEX must encode a 32-byte seed") from exc
        if len(seed) != 32:
            raise RuntimeError("PAP_SIGNING_SEED_HEX must encode a 32-byte seed")
    if settings.is_production and "stripe" in settings.allowed_rails and not settings.stripe_api_key:
        raise RuntimeError("STRIPE_API_KEY is required for the enabled Stripe rail")
    ledger = build_ledger(settings)
    signer = (
        DecisionSigner.from_private_bytes(bytes.fromhex(settings.signing_seed_hex))
        if len(settings.signing_seed_hex) == 64
        else DecisionSigner.generate()
    )
    if settings.is_production and not all([settings.identity_registry_path, settings.authority_registry_path, settings.trust_registry_path]):
        ledger.close()
        raise RuntimeError("production requires identity, authority and trust registries")
    try:
        with ledger.transaction():
            fingerprint = hashlib.sha256(signer.public_bytes()).hexdigest()
            prior = ledger.get_record("configuration", "signing_key")
            if prior and prior != fingerprint:
                raise RuntimeError("signing key differs from durable workflow key; perform an explicit key migration")
            ledger.put_record("configuration", "signing_key", fingerprint)
            saved_policies = ledger.get_record("workflow", "policies")
            if saved_policies is None:
                saved_policies = [p.model_dump(mode="json") for p in default_corporate_policies()]
                ledger.put_record("workflow", "policies", saved_policies)
        policies = [PolicyRule.model_validate(p) for p in saved_policies] if saved_policies else default_corporate_policies()
        authorizer = PrismPaymentAuthorizer(ledger, policies, signer=signer)
        # Domain separation derives a stable receipt key from the persisted deployment seed.
        order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
        receipt_scalar = int.from_bytes(hashlib.sha256(b"PAP receipt key v1" + signer.private_bytes()).digest(), "big") % (order - 1) + 1
        receipt_key = ec.derive_private_key(receipt_scalar, ec.SECP256R1())
        gateway = SettlementGatewayHarness(ledger, signer=signer, allowed_rails=set(settings.allowed_rails))
        orchestrator = RailOrchestrator(ledger, gateway, build_rails(settings), signer, audit=authorizer.audit, receipt_key=receipt_key)
        return create_app(
            authorizer,
            gateway,
            settings=settings,
            orchestrator=orchestrator,
            studio=PolicyStudio(authorizer.evaluator.policies),
            trust=TrustRegistry.from_path(settings.trust_registry_path),
            authority_resolver=AuthorityResolver(settings.authority_registry_path, build_connectors(settings)) if settings.authority_registry_path else None,
        )
    except BaseException:
        ledger.close()
        raise
