"""In-memory carrier credential resolution hooks and secure decryption.

Provides Fernet-based symmetric decryption for carrier passwords received from
the backend API. Reads decryption key from CARRIER_CREDENTIALS_KEY environment variable.
Never exposes keys, passwords, or plaintext in logs or exception messages.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class CredentialDecryptionError(RuntimeError):
    """Base exception for carrier credential decryption failures."""


class MissingKeyError(CredentialDecryptionError):
    """The decryption key environment variable is not configured."""


class InvalidKeyError(CredentialDecryptionError):
    """The decryption key is malformed or invalid."""


class InvalidCiphertextError(CredentialDecryptionError):
    """The ciphertext is corrupted, malformed, or failed authentication check."""


class CredentialResolver(Protocol):
    def decrypt(self, password: str) -> str: ...


class IdentityCredentialResolver:
    """Returns the password as-is (for unencrypted / local stub environments)."""

    def decrypt(self, password: str) -> str:
        return password


class FernetCredentialResolver:
    """Decrypts carrier credentials encrypted using Fernet (AES-128-CBC + HMAC-SHA256).

    Expects a 32-byte url-safe base64-encoded key configured in CARRIER_CREDENTIALS_KEY.
    All error messages are strictly sanitized to never leak keys or secret values.
    """

    def __init__(self, key: str | bytes | None = None, key_env: str = "CARRIER_CREDENTIALS_KEY"):
        self.key_env = key_env
        self._key = key

    def _get_fernet(self) -> Fernet:
        raw_key = self._key or os.environ.get(self.key_env)
        if not raw_key:
            raise MissingKeyError(
                f"Carrier credential decryption key is not configured (expected environment variable {self.key_env})"
            )
        if isinstance(raw_key, str):
            raw_key = raw_key.strip().encode("utf-8")
        try:
            return Fernet(raw_key)
        except Exception:
            raise InvalidKeyError("Carrier credential decryption key is malformed (must be 32 url-safe base64 bytes)") from None

    def decrypt(self, password: str) -> str:
        if not password:
            return ""
        fernet = self._get_fernet()
        try:
            token = password.strip().encode("utf-8")
            decrypted = fernet.decrypt(token)
            return decrypted.decode("utf-8")
        except InvalidToken:
            raise InvalidCiphertextError(
                "Failed to decrypt carrier credential: authentication tag verification failed or wrong key"
            ) from None
        except Exception:
            raise InvalidCiphertextError("Failed to decrypt carrier credential: malformed ciphertext format") from None


def make_credential_resolver(
    name: str | None = None,
    key: str | bytes | None = None,
) -> CredentialResolver:
    """Factory creating the appropriate credential resolver."""
    resolved_name = (name or "").lower().strip()
    if resolved_name == "fernet":
        return FernetCredentialResolver(key=key)
    if resolved_name == "identity":
        return IdentityCredentialResolver()
    if key is not None or os.environ.get("CARRIER_CREDENTIALS_KEY"):
        return FernetCredentialResolver(key=key)
    return IdentityCredentialResolver()
