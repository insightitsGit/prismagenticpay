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
