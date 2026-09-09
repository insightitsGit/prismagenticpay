"""Production HTTP surface: authorize, settle, rails, review, studio, audit, console."""

from __future__ import annotations

from pathlib import Path
import uuid
import httpx
from prismagenticpay.state.audit import AuditEvent
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from prismthinker import PolicyRule

from prismagenticpay.api.auth import ApiKeyGate, IdentityGate, Identity
from prismagenticpay.adapters.ap2 import AP2IngressAdapter
from prismagenticpay.connectors.base import FactMeta
from prismagenticpay.api.rate_limit import SlidingWindowLimiter
from prismagenticpay import __version__
from prismagenticpay.config import Settings
from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.domain.models import (
    AuthorizationDecision,
    AuthorityState,
    PaymentProposal,
)
from prismagenticpay.fx.provider import FxQuote, LockedRateBook
from prismagenticpay.lifecycle.orchestrator import RailOrchestrator
from prismagenticpay.studio.policies import PolicyStudio
from prismagenticpay.trust.registry import TrustRegistry


class AuthorizeRequest(BaseModel):
    proposal: PaymentProposal
    authority: AuthorityState
    session_id: Optional[str] = None
    auth_validity_seconds: int = Field(default=60, gt=0)
    actor_id: str = "api"
    fx_quote: Optional[FxQuote] = None
    fact_metadata: Dict[str, FactMeta] = Field(default_factory=dict)


class SettleRequest(BaseModel):
    authorization: AuthorizationDecision
    payment_hash: str
    rail_id: str = "test"


class ReviewActionRequest(BaseModel):
    approver_id: Optional[str] = None


class GatherResumeRequest(BaseModel):
    authority: Optional[AuthorityState] = None
    fact_metadata: Dict[str, FactMeta] = Field(default_factory=dict)
    actor_id: str = "api"


class VerifiedAuthorizeRequest(BaseModel):
    payment_token: str
    checkout_jwt: str
    payment_issuer_id: str
    checkout_issuer_id: str
    open_mandate_jwt: Optional[str] = None
    auth_validity_seconds: int = Field(default=60, gt=0, le=120)


class CaptureRequest(BaseModel):
    proposal: PaymentProposal
    authorization: AuthorizationDecision
    rail_id: str
    payment_token: str
    amount_cents: Optional[int] = Field(default=None, gt=0)


class RefundRequest(BaseModel):
    operation_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    proposal: PaymentProposal
    authorization: AuthorizationDecision
    rail_id: str
    amount_cents: int = Field(gt=0)


class ReconcileRequest(BaseModel):
    provider_reference: Optional[str] = Field(default=None, max_length=100)


class AuditEventView(BaseModel):
    event_id: str
    event_type: str
    authorization_id: Optional[str] = None
    payment_hash: Optional[str] = None
    actor_id: str
    detail: str


CONSOLE_DIR = Path(__file__).resolve().parent.parent / "console"


def create_app(
    authorizer: PrismPaymentAuthorizer,
    gateway: SettlementGatewayHarness,
    *,
    settings: Optional[Settings] = None,
    orchestrator: Optional[RailOrchestrator] = None,
    studio: Optional[PolicyStudio] = None,
    trust: Optional[TrustRegistry] = None,
    rates: Optional[LockedRateBook] = None,
    authority_resolver=None,
) -> FastAPI:
    settings = settings or Settings()
    studio = studio or PolicyStudio(authorizer.evaluator.policies)
    trust = trust or TrustRegistry()
    rates = rates or LockedRateBook()
    if settings.is_production and not settings.identity_registry_path:
        raise ValueError("production requires an identity registry")
    gate = IdentityGate(settings.identity_registry_path) if settings.identity_registry_path else ApiKeyGate(settings.api_keys)

    def require(role):
        def dependency(request: Request, identity=Depends(gate)):
            if not isinstance(identity, Identity):
                identity = Identity("development", [role])
            if role not in identity.roles:
                raise HTTPException(403, "role required: " + role)
            request.app.state.limiter.check(identity.subject)
            return identity
        return dependency

    def owns(identity, proposal):
        if settings.is_production and (identity.principal_id != proposal.principal_id or identity.agent_id != proposal.agent_id):
            raise HTTPException(403, "payment is outside authenticated identity scope")

    def currency(proposal):
        if proposal.currency != settings.home_currency:
            raise HTTPException(422, "cross-currency settlement is disabled")

    app = FastAPI(title=settings.app_name, version=__version__)
    app.state.authorizer = authorizer
    app.state.orchestrator = orchestrator
    app.state.limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )



    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Dict[str, str]:
        try:
            if not authorizer.ledger.readiness():
                raise RuntimeError("storage unavailable")
        except Exception as exc:
            raise HTTPException(503, "storage unavailable") from exc
        return {"status": "ready"}

    @app.get("/console")
    def console(identity=Depends(require("audit"))) -> FileResponse:
        page = CONSOLE_DIR / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="console not packaged")
        return FileResponse(page)

    @app.post("/v1/authorize", response_model=AuthorizationDecision)
    def authorize(request: AuthorizeRequest, identity=Depends(require("authorize"))) -> AuthorizationDecision:
        if settings.is_production:
            raise HTTPException(403, "use /v1/authorize/ap2 with a signed mandate")
        currency(request.proposal)
        if request.fx_quote:
            raise HTTPException(422, "caller-supplied FX is disabled")
        return authorizer.process_authorization(
            request.proposal,
            request.authority,
            auth_validity_seconds=request.auth_validity_seconds,
            session_id=request.session_id,
            actor_id=identity.subject,
            fact_metadata=request.fact_metadata,
        )

    @app.post("/v1/authorize/ap2", response_model=AuthorizationDecision)
    def authorize_ap2(request: VerifiedAuthorizeRequest, identity=Depends(require("authorize"))):
        if authority_resolver is None:
            raise HTTPException(503, "authority resolver unavailable")
        try:
            adapter = AP2IngressAdapter(
                payment_public_key=trust.public_key(request.payment_issuer_id, "payment"),
                checkout_public_key=trust.public_key(request.checkout_issuer_id, "checkout"),
            )
            proposal = adapter.verify_and_project(request.payment_token, request.checkout_jwt,
                                                 open_mandate_jwt=request.open_mandate_jwt)
            owns(identity, proposal)
            currency(proposal)
            authority, metadata = authority_resolver.resolve(proposal)
        except (OSError, httpx.HTTPError) as exc:
            raise HTTPException(503, "authority source unavailable") from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, "mandate or authority verification failed") from exc
        try:
            return authorizer.process_authorization(proposal, authority, fact_metadata=metadata,
                   auth_validity_seconds=request.auth_validity_seconds, actor_id=identity.subject)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/settle")
    def settle(request: SettleRequest, identity=Depends(require("capture"))) -> Dict[str, Any]:
        if settings.is_production:
            raise HTTPException(403, "direct settlement is disabled; use capture")
        ok = gateway.settle_transaction(
            request.authorization,
            request.payment_hash,
            rail_id=request.rail_id,
        )
        if not ok:
            raise HTTPException(status_code=409, detail="settlement rejected")
        return {"ok": True}

    @app.post("/v1/capture")
    def capture(request: CaptureRequest, identity=Depends(require("capture"))) -> Dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="rail orchestrator is not configured")
        owns(identity, request.proposal)
        currency(request.proposal)
        if request.rail_id == "stripe":
            from prismagenticpay.rails.stripe_rail import _reject_pan
            try:
                _reject_pan(request.payment_token)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        result, receipt = orchestrator.capture(
            request.proposal,
            request.authorization,
            rail_id=request.rail_id,
            payment_token=request.payment_token,
            amount_cents=request.amount_cents,
        )
        if not result.ok:
            raise HTTPException(status_code=409, detail=result.detail)
        return {
            "ok": True,
            "reference": result.reference,
            "receipt": None if receipt is None else receipt.token,
        }

    @app.post("/v1/refund")
    def refund(request: RefundRequest, identity=Depends(require("refund"))) -> Dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="rail orchestrator is not configured")
        owns(identity, request.proposal)
        result = orchestrator.refund(
            request.proposal,
            request.authorization,
            rail_id=request.rail_id,
            amount_cents=request.amount_cents,
            operation_id=request.operation_id,
        )
        if not result.ok:
            raise HTTPException(status_code=409, detail=result.detail)
        return {"ok": True, "reference": result.reference}

    @app.post("/v1/reviews/{case_id}/approve", response_model=AuthorizationDecision)
    def approve(case_id: str, request: ReviewActionRequest, identity=Depends(require("review"))) -> AuthorizationDecision:
        try:
            return authorizer.approve_review(case_id, identity.subject if settings.is_production else (request.approver_id or identity.subject))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/reviews/{case_id}/deny", response_model=AuthorizationDecision)
    def deny(case_id: str, request: ReviewActionRequest, identity=Depends(require("review"))) -> AuthorizationDecision:
        try:
            return authorizer.deny_review(case_id, identity.subject if settings.is_production else (request.approver_id or identity.subject))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/gathers/{case_id}/resume", response_model=AuthorizationDecision)
    def resume(case_id: str, request: GatherResumeRequest, identity=Depends(require("authorize"))) -> AuthorizationDecision:
        try:
            case = authorizer.cases.get_gather(case_id)
            if case is None:
                raise ValueError("gather case not found")
            proposal = authorizer.cases.proposal_for(case.payment_hash)
            owns(identity, proposal)
            authority, metadata = (authority_resolver.resolve(proposal) if settings.is_production
                                   else (request.authority, request.fact_metadata))
            if authority is None:
                raise ValueError("authority required")
            return authorizer.resume_gather(case_id, authority, fact_metadata=metadata, actor_id=identity.subject)
        except (OSError, httpx.HTTPError) as exc:
            raise HTTPException(503, "authority source unavailable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/operations")
    def operations(identity=Depends(require("reconcile"))):
        return [{k: o.get(k) for k in ("id", "kind", "rail", "amount", "status", "reference", "last_error", "created_at")}
                for o in authorizer.ledger.list_records("operations")]

    @app.post("/v1/operations/{operation_id}/retry")
    def retry_operation(operation_id: str, identity=Depends(require("reconcile"))):
        if orchestrator is None:
            raise HTTPException(503, "rail unavailable")
        result, receipt = orchestrator.recover(operation_id)
        if not result.ok:
            raise HTTPException(409, result.detail)
        return {"ok": True, "reference": result.reference, "receipt": receipt.token if receipt else None}

    @app.post("/v1/operations/{operation_id}/reconcile")
    def reconcile_operation(operation_id: str, request: ReconcileRequest, identity=Depends(require("reconcile"))):
        if orchestrator is None:
            raise HTTPException(503, "rail unavailable")
        result = orchestrator.reconcile(operation_id, request.provider_reference)
        if not result.ok and result.detail != "confirmed_not_paid":
            raise HTTPException(409, result.detail)
        return {"ok": result.ok, "reference": result.reference, "outcome": result.detail}

    @app.get("/v1/audit", response_model=List[AuditEventView])
    def audit(payment_hash: Optional[str] = None, identity=Depends(require("audit"))) -> List[AuditEventView]:
        return [
            AuditEventView.model_validate(event.model_dump())
            for event in authorizer.audit.list(payment_hash=payment_hash)
        ]

    def refresh_policies():
        saved = authorizer.ledger.get_record("workflow", "policies")
        if saved is not None:
            studio.policies = [PolicyRule.model_validate(p) for p in saved]
        return studio.list()

    def audit_policy(action, rule_id, identity):
        authorizer.audit.append(AuditEvent(event_id="aud_" + uuid.uuid4().hex,
            event_type="policy." + action, actor_id=identity.subject, detail=rule_id))

    @app.get("/v1/policies")
    def list_policies(identity=Depends(require("audit"))) -> List[dict]:
        return [rule.model_dump() for rule in refresh_policies()]

    @app.post("/v1/policies")
    def upsert_policy(rule: PolicyRule, identity=Depends(require("policy"))) -> dict:
        with authorizer.ledger.transaction():
            refresh_policies()
            studio.upsert(rule)
            audit_policy("upsert", rule.id, identity)
            authorizer.ledger.put_record("workflow", "policies", [r.model_dump(mode="json") for r in studio.list()])
            authorizer.evaluator.policies = studio.list()
        return rule.model_dump()

    @app.delete("/v1/policies/{rule_id}")
    def delete_policy(rule_id: str, identity=Depends(require("policy"))) -> Dict[str, bool]:
        with authorizer.ledger.transaction():
            if len(refresh_policies()) <= 1:
                raise HTTPException(409, "at least one policy is required")
            ok = studio.delete(rule_id)
            audit_policy("delete", rule_id, identity)
            authorizer.ledger.put_record("workflow", "policies", [r.model_dump(mode="json") for r in studio.list()])
            authorizer.evaluator.policies = studio.list()
        return {"ok": ok}

    @app.post("/v1/policies/simulate")
    def simulate(request: AuthorizeRequest, identity=Depends(require("audit"))) -> dict:
        from prismagenticpay.core.evaluator import PrismThinkerPaymentEvaluator
        outcome = PrismThinkerPaymentEvaluator(refresh_policies()).evaluate_proposal(request.proposal, request.authority, fact_metadata=request.fact_metadata)
        return {
            "directive": outcome.directive,
            "allowed_tools": outcome.allowed_tools,
            "gather_fact_keys": outcome.gather_fact_keys,
            "rationale": outcome.decision_graph.recommended_rationale,
        }

    @app.get("/v1/trust")
    def list_trust(identity=Depends(require("audit"))) -> List[dict]:
        return [issuer.model_dump() for issuer in trust.list()]

    return app
