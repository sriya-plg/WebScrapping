"""Tests for carrier credential decryption, Fernet resolver, and error sanitization."""
import os
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.backend.credentials import (
    CredentialDecryptionError,
    FernetCredentialResolver,
    IdentityCredentialResolver,
    InvalidCiphertextError,
    InvalidKeyError,
    MissingKeyError,
    make_credential_resolver,
)


def test_identity_resolver_returns_password_unchanged():
    resolver = IdentityCredentialResolver()
    assert resolver.decrypt("plaintext_pass") == "plaintext_pass"
    assert resolver.decrypt("") == ""


def test_fernet_valid_decryption_with_test_key():
    test_key = Fernet.generate_key()
    fernet = Fernet(test_key)
    plain = "CarrierSecret123!"
    ciphertext = fernet.encrypt(plain.encode("utf-8")).decode("utf-8")

    resolver = FernetCredentialResolver(key=test_key)
    decrypted = resolver.decrypt(ciphertext)
    assert decrypted == plain


def test_fernet_decryption_reads_key_from_env(monkeypatch):
    test_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CARRIER_CREDENTIALS_KEY", test_key)

    plain = "MyTestPortalPass!"
    ciphertext = Fernet(test_key.encode("utf-8")).encrypt(plain.encode("utf-8")).decode("utf-8")

    resolver = FernetCredentialResolver()
    assert resolver.decrypt(ciphertext) == plain


def test_fernet_missing_key_raises_sanitized_missing_key_error(monkeypatch):
    monkeypatch.delenv("CARRIER_CREDENTIALS_KEY", raising=False)
    resolver = FernetCredentialResolver()

    with pytest.raises(MissingKeyError) as exc_info:
        resolver.decrypt("dummy_token")

    err = str(exc_info.value)
    assert "CARRIER_CREDENTIALS_KEY" in err
    assert "dummy_token" not in err


def test_fernet_malformed_key_raises_sanitized_invalid_key_error():
    resolver = FernetCredentialResolver(key="not_valid_base64_and_not_32_bytes")

    with pytest.raises(InvalidKeyError) as exc_info:
        resolver.decrypt("dummy_token")

    err = str(exc_info.value)
    assert "malformed" in err
    assert "not_valid" not in err  # Never leaks the key string


def test_fernet_invalid_ciphertext_raises_sanitized_error():
    test_key = Fernet.generate_key()
    resolver = FernetCredentialResolver(key=test_key)

    with pytest.raises(InvalidCiphertextError) as exc_info:
        resolver.decrypt("corrupted_or_not_a_valid_fernet_token")

    err = str(exc_info.value)
    assert "corrupted" not in err  # Sanitized, no input payload echoed
    assert "Failed to decrypt" in err


def test_fernet_wrong_key_raises_invalid_ciphertext_error():
    key1 = Fernet.generate_key()
    key2 = Fernet.generate_key()

    plain = "SecretData"
    ciphertext = Fernet(key1).encrypt(plain.encode("utf-8")).decode("utf-8")

    resolver = FernetCredentialResolver(key=key2)
    with pytest.raises(InvalidCiphertextError) as exc_info:
        resolver.decrypt(ciphertext)

    assert "authentication tag verification failed or wrong key" in str(exc_info.value)
    assert plain not in str(exc_info.value)


def test_empty_password_returns_empty():
    test_key = Fernet.generate_key()
    resolver = FernetCredentialResolver(key=test_key)
    assert resolver.decrypt("") == ""


def test_make_credential_resolver_factory(monkeypatch):
    assert isinstance(make_credential_resolver("identity"), IdentityCredentialResolver)

    test_key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("CARRIER_CREDENTIALS_KEY", test_key)
    resolver = make_credential_resolver()
    assert isinstance(resolver, FernetCredentialResolver)
