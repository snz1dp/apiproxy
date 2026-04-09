"""Utilities for API key generation and encryption."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from typing import Final, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_API_KEY_AES_ENV: Final[str] = "APIPROXY_API_KEY_AES_KEY"
_API_KEY_HASH_SECRET_ENV: Final[str] = "APIPROXY_API_KEY_HASH_SECRET"
_DEFAULT_SECRET: Final[bytes] = hashlib.sha256(b"apiproxy-default-secret").digest()
_NONCE_LABEL: Final[bytes] = b"apiproxy-api-key"
_TOKEN_SEPARATOR: Final[str] = ":"
_TOKEN_V2_PREFIX: Final[str] = "sk"
_TOKEN_V2_SEPARATOR: Final[str] = "_"

class ApiKeyEncryptionError(RuntimeError):
    """Raised when an API key cannot be encrypted or decrypted."""


class ApiKeyTokenError(ValueError):
    """Raised when an API key token cannot be parsed."""


class ApiKeyHashingError(ValueError):
    """Raised when an API key cannot be hashed."""


@dataclass(slots=True, frozen=True)
class _AESCipher:
    """AES-GCM cipher helper that produces deterministic ciphertext."""

    key: bytes
    nonce_key: bytes

    @classmethod
    def from_secret(cls, secret: bytes) -> "_AESCipher":
        normalized_key = cls._normalize_key(secret)
        nonce_key = hashlib.sha256(normalized_key + _NONCE_LABEL).digest()
        return cls(key=normalized_key, nonce_key=nonce_key)

    @staticmethod
    def _normalize_key(raw: bytes) -> bytes:
        if len(raw) in (16, 24, 32):
            return raw
        return hashlib.sha256(raw).digest()

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            msg = "API key plaintext must not be empty"
            raise ApiKeyEncryptionError(msg)

        plain_bytes = plaintext.encode("utf-8")
        nonce = hmac.new(self.nonce_key, plain_bytes, hashlib.sha256).digest()[:12]
        aesgcm = AESGCM(self.key)
        ciphertext = aesgcm.encrypt(nonce, plain_bytes, None)
        payload = nonce + ciphertext
        return base64.urlsafe_b64encode(payload).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            msg = "Encrypted API key must not be empty"
            raise ApiKeyEncryptionError(msg)
        try:
            payload = base64.urlsafe_b64decode(token.encode("ascii"))
        except (ValueError, binascii.Error) as exc:
            raise ApiKeyEncryptionError("Invalid encrypted API key") from exc

        if len(payload) <= 12:
            msg = "Malformed encrypted API key"
            raise ApiKeyEncryptionError(msg)

        nonce, ciphertext = payload[:12], payload[12:]
        aesgcm = AESGCM(self.key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise ApiKeyEncryptionError("Failed to decrypt API key") from exc
        return plaintext.decode("utf-8")


def _load_secret() -> bytes:
    secret = os.getenv(_API_KEY_AES_ENV)
    if not secret:
        return _DEFAULT_SECRET
    try:
        return base64.urlsafe_b64decode(secret.encode("ascii"))
    except (ValueError, binascii.Error):
        return secret.encode("utf-8")


def _load_hash_secret() -> bytes:
    hash_secret = os.getenv(_API_KEY_HASH_SECRET_ENV)
    if not hash_secret:
        return _load_secret()
    try:
        return base64.urlsafe_b64decode(hash_secret.encode("ascii"))
    except (ValueError, binascii.Error):
        return hash_secret.encode("utf-8")


@lru_cache(maxsize=1)
def _get_cipher() -> _AESCipher:
    return _AESCipher.from_secret(_load_secret())


def reset_cipher_cache() -> None:
    """Reset the cached cipher (mainly for testing)."""

    _get_cipher.cache_clear()  # type: ignore[attr-defined]


def generate_api_key(length: int = 12) -> str:
    """Generate a random API key string."""

    if length <= 0:
        msg = "API key length must be positive"
        raise ValueError(msg)

    # token_urlsafe emits URL-safe ASCII and keeps enough entropy for API keys.
    generated = secrets.token_urlsafe(length)
    return generated[:length]


def hash_api_key(ownerapp_id: str, plaintext_key: str) -> str:
    """Compute an irreversible owner-scoped API key hash."""

    if not ownerapp_id or not plaintext_key:
        msg = "Owner app id and API key must not be empty"
        raise ApiKeyHashingError(msg)

    payload = f"{ownerapp_id}{_TOKEN_SEPARATOR}{plaintext_key}".encode("utf-8")
    digest = hmac.new(_load_hash_secret(), payload, hashlib.sha256).hexdigest()
    return digest


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt the given API key using AES-GCM."""

    cipher = _get_cipher()
    return cipher.encrypt(plaintext)


def decrypt_api_key(token: str) -> str:
    """Decrypt the stored API key back into plaintext."""

    cipher = _get_cipher()
    return cipher.decrypt(token)


def compose_api_key_token(ownerapp_id: str, plaintext_key: str) -> str:
    """Compose the API key token that embeds the owner application id."""

    if not ownerapp_id or not plaintext_key:
        msg = "Owner app id and plaintext key must not be empty"
        raise ApiKeyTokenError(msg)
    return f"{ownerapp_id}{_TOKEN_SEPARATOR}{plaintext_key}"


def parse_api_key_token(token: str) -> Tuple[str, str]:
    """Parse the composite API key token into ownerapp id and plaintext key."""

    if not token:
        msg = "API key token must not be empty"
        raise ApiKeyTokenError(msg)
    ownerapp_id, sep, plaintext_key = token.partition(_TOKEN_SEPARATOR)
    if not sep or not ownerapp_id or not plaintext_key:
        msg = "Invalid API key token format"
        raise ApiKeyTokenError(msg)
    return ownerapp_id, plaintext_key


def compose_api_key_token_v2(ownerapp_id: str, plaintext_key: str) -> str:
    """Compose version 2 API token as `ak2.<ownerapp_id_b64>.<plaintext_key>`."""

    if not ownerapp_id or not plaintext_key:
        msg = "Owner app id and plaintext key must not be empty"
        raise ApiKeyTokenError(msg)
    owner_encoded = base64.urlsafe_b64encode(ownerapp_id.encode("utf-8")).decode("ascii")
    return f"{_TOKEN_V2_PREFIX}{_TOKEN_V2_SEPARATOR}{owner_encoded}{_TOKEN_V2_SEPARATOR}{plaintext_key}"


def parse_api_key_token_v2(token: str) -> Tuple[str, str]:
    """Parse version 2 API token into ownerapp id and plaintext key."""

    if not token:
        msg = "API key token must not be empty"
        raise ApiKeyTokenError(msg)

    parts = token.split(_TOKEN_V2_SEPARATOR, 2)
    if len(parts) != 3 or parts[0] != _TOKEN_V2_PREFIX:
        msg = "Invalid v2 API key token format"
        raise ApiKeyTokenError(msg)

    owner_encoded = parts[1]
    plaintext_key = parts[2]
    if not owner_encoded or not plaintext_key:
        msg = "Invalid v2 API key token payload"
        raise ApiKeyTokenError(msg)

    try:
        ownerapp_id = base64.urlsafe_b64decode(owner_encoded.encode("ascii")).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiKeyTokenError("Invalid v2 owner app id") from exc

    if not ownerapp_id:
        msg = "Invalid v2 owner app id"
        raise ApiKeyTokenError(msg)

    return ownerapp_id, plaintext_key
