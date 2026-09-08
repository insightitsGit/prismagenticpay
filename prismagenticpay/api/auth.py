"""API key authentication. Keys are compared with a constant-time digest."""

from __future__ import annotations

import hmac
import secrets
from typing import Iterable, Optional

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


class ApiKeyGate:
    def __init__(self, keys: Iterable[str], required: bool = False):
        self._keys = [key for key in keys if key]
        self.required = required or bool(self._keys)

    def __call__(self, api_key: Optional[str] = Security(_HEADER)) -> str:
        if not self.required:
            return "anonymous"
        if not api_key:
            raise HTTPException(status_code=401, detail="missing API key")
        for candidate in self._keys:
            if hmac.compare_digest(api_key, candidate):
                return api_key
        raise HTTPException(status_code=403, detail="invalid API key")


def new_api_key() -> str:
    return secrets.token_urlsafe(32)


class Identity:
    def __init__(self, subject, roles, principal_id=None, agent_id=None):
        self.subject, self.roles = subject, set(roles)
        self.principal_id, self.agent_id = principal_id, agent_id


class IdentityGate:
    """Store SHA-256 digests of high-entropy keys; never trust request actor names."""
    def __init__(self, path):
        import json
        from pathlib import Path
        self.records = json.loads(Path(path).read_text(encoding="utf-8"))["identities"]
        if not self.records:
            raise ValueError("identity registry is empty")
        digests = set()
        for record in self.records:
            digest = record["key_sha256"]
            if len(digest) != 64 or digest in digests or not record.get("subject"):
                raise ValueError("invalid or duplicate identity key digest")
            bytes.fromhex(digest)
            digests.add(digest)

    def __call__(self, api_key: Optional[str] = Security(_HEADER)) -> Identity:
        import hashlib
        if not api_key:
            raise HTTPException(401, "missing API key")
        digest = hashlib.sha256(api_key.encode()).hexdigest()
        for record in self.records:
            if hmac.compare_digest(digest, record["key_sha256"]):
                return Identity(record["subject"], record["roles"], record.get("principal_id"), record.get("agent_id"))
        raise HTTPException(403, "invalid API key")
