"""Local issuer/agent trust list. This is not a card-network directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Optional

from pydantic import BaseModel, Field, field_validator


class TrustedIssuer(BaseModel):
    issuer_id: str
    algorithm: str = "ES256"
    jwk: Dict[str, str]
    purposes: list[str] = Field(default_factory=lambda: ["payment"])

    @field_validator("jwk")
    @classmethod
    def public_only(cls, value):
        if "d" in value or value.get("kty") != "EC" or value.get("crv") != "P-256":
            raise ValueError("trust registry requires a public P-256 JWK")
        if not value.get("x") or not value.get("y"):
            raise ValueError("public JWK coordinates required")
        return value



class TrustRegistry:
    def __init__(self, issuers: Optional[Iterable[TrustedIssuer]] = None):
        self._issuers: Dict[str, TrustedIssuer] = {
            item.issuer_id: item for item in (issuers or [])
        }

    @classmethod
    def from_path(cls, path: str) -> "TrustRegistry":
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(TrustedIssuer.model_validate(item) for item in raw.get("issuers", []))

    def get(self, issuer_id: str) -> Optional[TrustedIssuer]:
        return self._issuers.get(issuer_id)

    def allow(self, issuer_id: str) -> bool:
        return issuer_id in self._issuers

    def add(self, issuer: TrustedIssuer) -> None:
        self._issuers[issuer.issuer_id] = issuer

    def list(self) -> list[TrustedIssuer]:
        return list(self._issuers.values())


    def public_key(self, issuer_id, purpose):
        import jwt
        issuer = self.get(issuer_id)
        if issuer is None or issuer.algorithm != "ES256" or purpose not in issuer.purposes:
            raise ValueError("issuer is not trusted for this purpose")
        return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(issuer.jwk))
