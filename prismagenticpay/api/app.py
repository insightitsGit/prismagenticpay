"""HTTP contract for authorize, settle, review, gather, and audit."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from prismagenticpay.core.authorizer import PrismPaymentAuthorizer
from prismagenticpay.core.settlement import SettlementGatewayHarness
from prismagenticpay.domain.models import (
    AuthorizationDecision,
    AuthorityState,
    PaymentProposal,
)


class AuthorizeRequest(BaseModel):
    proposal: PaymentProposal
    authority: AuthorityState
    session_id: Optional[str] = None
    auth_validity_seconds: int = 60
    actor_id: str = "api"


class SettleRequest(BaseModel):
    authorization: AuthorizationDecision
    payment_hash: str
    rail_id: str = "test"


class ReviewActionRequest(BaseModel):
    approver_id: str


class GatherResumeRequest(BaseModel):
    authority: AuthorityState
    actor_id: str = "api"


class AuditEventView(BaseModel):
    event_id: str
    event_type: str
    authorization_id: Optional[str] = None
    payment_hash: Optional[str] = None
    actor_id: str
    detail: str


def create_app(authorizer: PrismPaymentAuthorizer, gateway: SettlementGatewayHarness) -> FastAPI:
    app = FastAPI(title="PrismAgenticPay", version="1.2.0")

    @app.post("/v1/authorize", response_model=AuthorizationDecision)
    def authorize(request: AuthorizeRequest) -> AuthorizationDecision:
        return authorizer.process_authorization(
            request.proposal,
            request.authority,
            auth_validity_seconds=request.auth_validity_seconds,
            session_id=request.session_id,
            actor_id=request.actor_id,
        )

    @app.post("/v1/settle")
    def settle(request: SettleRequest) -> Dict[str, Any]:
        ok = gateway.settle_transaction(
            request.authorization,
            request.payment_hash,
            rail_id=request.rail_id,
        )
        if not ok:
            raise HTTPException(status_code=409, detail="settlement rejected")
        return {"ok": True}

    @app.post("/v1/reviews/{case_id}/approve", response_model=AuthorizationDecision)
    def approve(case_id: str, request: ReviewActionRequest) -> AuthorizationDecision:
        try:
            return authorizer.approve_review(case_id, request.approver_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/reviews/{case_id}/deny", response_model=AuthorizationDecision)
    def deny(case_id: str, request: ReviewActionRequest) -> AuthorizationDecision:
        try:
            return authorizer.deny_review(case_id, request.approver_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/gathers/{case_id}/resume", response_model=AuthorizationDecision)
    def resume(case_id: str, request: GatherResumeRequest) -> AuthorizationDecision:
        try:
            return authorizer.resume_gather(
                case_id, request.authority, actor_id=request.actor_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/audit", response_model=List[AuditEventView])
    def audit(payment_hash: Optional[str] = None) -> List[AuditEventView]:
        return [
            AuditEventView.model_validate(event.model_dump())
            for event in authorizer.audit.list(payment_hash=payment_hash)
        ]

    return app
