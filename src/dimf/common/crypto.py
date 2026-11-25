"""Cryptographic helpers: canonical JSON and Ed25519 signatures.

Canonical JSON rules (used by Python, the API and the browser auditor):

* object keys are sorted lexicographically (Unicode code points);
* no insignificant whitespace: separators are ``","`` and ``":"``;
* UTF-8 encoding, non-ASCII characters emitted literally;
* only JSON types produced by ``json.dumps`` are allowed.

All synthetic records use strings and integers (amounts in paise), so the
JavaScript and Python canonicalizations agree without float formatting
subtleties.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical_json(obj: Any) -> str:
    """Return the deterministic canonical JSON representation of *obj*."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_bytes(obj: Any) -> bytes:
    """Canonical JSON encoded as UTF-8 bytes."""
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


@dataclass(frozen=True)
class KeyPair:
    """An Ed25519 key pair with helpers for this prototype."""

    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @classmethod
    def from_seed(cls, seed: bytes | str) -> "KeyPair":
        """Deterministically derive a key pair from a seed string or bytes.

        This is convenient for reproducible demos and tests.  It is NOT a
        secure key-generation procedure for production.
        """
        if isinstance(seed, str):
            seed = seed.encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        private = Ed25519PrivateKey.from_private_bytes(digest)
        return cls(private_key=private, public_key=private.public_key())

    @classmethod
    def generate(cls) -> "KeyPair":
        private = Ed25519PrivateKey.generate()
        return cls(private_key=private, public_key=private.public_key())

    def sign(self, data: bytes) -> bytes:
        return self.private_key.sign(data)

    def verify(self, signature: bytes, data: bytes) -> bool:
        try:
            self.public_key.verify(signature, data)
            return True
        except InvalidSignature:
            return False

    def public_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def private_bytes(self) -> bytes:
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def public_key_hex(self) -> str:
        return self.public_bytes().hex()

    def key_id(self) -> str:
        """Stable short identifier derived from the public key."""
        return sha256_hex(self.public_bytes())[:32]


def public_key_from_hex(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(text))


def verify_signature(public_key_hex: str, signature_b64: str, payload: Any) -> bool:
    """Verify a base64 Ed25519 signature over the canonical JSON of payload."""
    try:
        signature = b64d(signature_b64)
        public_key = public_key_from_hex(public_key_hex)
        public_key.verify(signature, canonical_bytes(payload))
        return True
    except (ValueError, InvalidSignature):
        return False
