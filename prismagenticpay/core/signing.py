"""Ed25519 signatures over AuthorizationDecision canonical bytes."""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from prismagenticpay.domain.models import AuthorizationDecision


class DecisionSigner:
    def __init__(self, private_key: Ed25519PrivateKey, issuer_id: str = "prismagenticpay"):
        self._private = private_key
        self.public_key = private_key.public_key()
        self.issuer_id = issuer_id

    @classmethod
    def generate(cls, issuer_id: str = "prismagenticpay") -> "DecisionSigner":
        return cls(Ed25519PrivateKey.generate(), issuer_id=issuer_id)

    def sign(self, decision: AuthorizationDecision) -> AuthorizationDecision:
        signed = decision.model_copy(
            update={"issuer_id": self.issuer_id, "signature": None}
        )
        signature = self._private.sign(signed.canonical_bytes()).hex()
        return signed.model_copy(update={"signature": signature})

    def verify(self, decision: AuthorizationDecision) -> bool:
        if not decision.signature:
            return False
        try:
            self.public_key.verify(bytes.fromhex(decision.signature), decision.canonical_bytes())
        except (InvalidSignature, ValueError):
            return False
        return True

    def public_bytes(self) -> bytes:
        return self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def private_bytes(self) -> bytes:
        return self._private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    @classmethod
    def from_private_bytes(
        cls, raw: bytes, issuer_id: str = "prismagenticpay"
    ) -> "DecisionSigner":
        return cls(Ed25519PrivateKey.from_private_bytes(raw), issuer_id=issuer_id)

    @classmethod
    def verifier_from_public_bytes(cls, raw: bytes) -> Ed25519PublicKey:
        return Ed25519PublicKey.from_public_bytes(raw)
