"""Local issuer/agent trust list. This is not a card-network directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Optional

from pydantic import BaseModel


class TrustedIssuer(BaseModel):
    issuer_id: str
    algorithm: str = "ES256"
    jwk: Dict[str, str]


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
