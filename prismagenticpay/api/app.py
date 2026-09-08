"""Production HTTP surface: authorize, settle, rails, review, studio, audit, console."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from prismthinker import PolicyRule

from prismagenticpay.api.auth import ApiKeyGate
from prismagenticpay.api.rate_limit import SlidingWindowLimiter, limit_request
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
    auth_validity_seconds: int = 60
    actor_id: str = "api"
    fx_quote: Optional[FxQuote] = None


class SettleRequest(BaseModel):
    authorization: AuthorizationDecision
    payment_hash: str
    rail_id: str = "test"


class ReviewActionRequest(BaseModel):
    approver_id: str


class GatherResumeRequest(BaseModel):
    authority: AuthorityState
    actor_id: str = "api"


class CaptureRequest(BaseModel):
    proposal: PaymentProposal
    authorization: AuthorizationDecision
    rail_id: str
    payment_token: str
    amount_cents: Optional[int] = None


class RefundRequest(BaseModel):
    proposal: PaymentProposal
    authorization: AuthorizationDecision
    rail_id: str
    amount_cents: int


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
) -> FastAPI:
    settings = settings or Settings()
    studio = studio or PolicyStudio(authorizer.evaluator.policies)
    trust = trust or TrustRegistry()
    rates = rates or LockedRateBook()
    gate = ApiKeyGate(settings.api_keys, required=settings.is_production)

    app = FastAPI(title=settings.app_name, version="1.3.0")
    app.state.limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    if CONSOLE_DIR.exists():
        app.mount("/console/assets", StaticFiles(directory=CONSOLE_DIR), name="console-assets")

    @app.get("/healthz")
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> Dict[str, str]:
        authorizer.ledger.inspect_available(
            session_id="ready", principal_id="ready", mandate_id="ready"
        )
        return {"status": "ready"}

    @app.get("/console")
    def console() -> FileResponse:
        page = CONSOLE_DIR / "index.html"
        if not page.exists():
            raise HTTPException(status_code=404, detail="console not packaged")
        return FileResponse(page)

    @app.post("/v1/authorize", response_model=AuthorizationDecision, dependencies=[Depends(limit_request)])
    def authorize(request: AuthorizeRequest, _: str = Depends(gate)) -> AuthorizationDecision:
        if request.fx_quote:
            rates.put(request.fx_quote)
            request.proposal.amount_cents = rates.convert_to_home(
                request.proposal.amount_cents,
                request.proposal.currency,
                settings.home_currency,
                request.fx_quote,
            )
            request.proposal.currency = settings.home_currency
        return authorizer.process_authorization(
            request.proposal,
            request.authority,
            auth_validity_seconds=request.auth_validity_seconds,
            session_id=request.session_id,
            actor_id=request.actor_id,
        )

    @app.post("/v1/settle", dependencies=[Depends(limit_request)])
    def settle(request: SettleRequest, _: str = Depends(gate)) -> Dict[str, Any]:
        ok = gateway.settle_transaction(
            request.authorization,
            request.payment_hash,
            rail_id=request.rail_id,
        )
        if not ok:
            raise HTTPException(status_code=409, detail="settlement rejected")
        return {"ok": True}

    @app.post("/v1/capture", dependencies=[Depends(limit_request)])
    def capture(request: CaptureRequest, _: str = Depends(gate)) -> Dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="rail orchestrator is not configured")
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

    @app.post("/v1/refund", dependencies=[Depends(limit_request)])
    def refund(request: RefundRequest, _: str = Depends(gate)) -> Dict[str, Any]:
        if orchestrator is None:
            raise HTTPException(status_code=503, detail="rail orchestrator is not configured")
        result = orchestrator.refund(
            request.proposal,
            request.authorization,
            rail_id=request.rail_id,
            amount_cents=request.amount_cents,
        )
        if not result.ok:
            raise HTTPException(status_code=409, detail=result.detail)
        return {"ok": True, "reference": result.reference}

    @app.post("/v1/reviews/{case_id}/approve", response_model=AuthorizationDecision)
    def approve(case_id: str, request: ReviewActionRequest, _: str = Depends(gate)) -> AuthorizationDecision:
        try:
            return authorizer.approve_review(case_id, request.approver_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/reviews/{case_id}/deny", response_model=AuthorizationDecision)
    def deny(case_id: str, request: ReviewActionRequest, _: str = Depends(gate)) -> AuthorizationDecision:
        try:
            return authorizer.deny_review(case_id, request.approver_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/gathers/{case_id}/resume", response_model=AuthorizationDecision)
    def resume(case_id: str, request: GatherResumeRequest, _: str = Depends(gate)) -> AuthorizationDecision:
        try:
            return authorizer.resume_gather(
                case_id, request.authority, actor_id=request.actor_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/audit", response_model=List[AuditEventView])
    def audit(payment_hash: Optional[str] = None, _: str = Depends(gate)) -> List[AuditEventView]:
        return [
            AuditEventView.model_validate(event.model_dump())
            for event in authorizer.audit.list(payment_hash=payment_hash)
        ]

    @app.get("/v1/policies")
    def list_policies(_: str = Depends(gate)) -> List[dict]:
        return [rule.model_dump() for rule in studio.list()]

    @app.post("/v1/policies")
    def upsert_policy(rule: PolicyRule, _: str = Depends(gate)) -> dict:
        authorizer.evaluator.policies = studio.upsert(rule) and studio.list()
        return rule.model_dump()

    @app.delete("/v1/policies/{rule_id}")
    def delete_policy(rule_id: str, _: str = Depends(gate)) -> Dict[str, bool]:
        ok = studio.delete(rule_id)
        authorizer.evaluator.policies = studio.list()
        return {"ok": ok}

    @app.post("/v1/policies/simulate")
    def simulate(request: AuthorizeRequest, _: str = Depends(gate)) -> dict:
        outcome = studio.simulate(request.proposal, request.authority)
        return {
            "directive": outcome.directive,
            "allowed_tools": outcome.allowed_tools,
            "gather_fact_keys": outcome.gather_fact_keys,
            "rationale": outcome.decision_graph.recommended_rationale,
        }

    @app.get("/v1/trust")
    def list_trust(_: str = Depends(gate)) -> List[dict]:
        return [issuer.model_dump() for issuer in trust.list()]

    return app
