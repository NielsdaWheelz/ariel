from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import secrets
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        encoded = (value + padding).encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("base64url value must be ascii") from exc
    return base64.b64decode(encoded, altchars=b"-_", validate=True)


def _derive_secret_bytes(secret: str) -> bytes:
    normalized = secret.strip()
    if not normalized:
        normalized = "dev-local-connector-secret"
    return hashlib.sha256(normalized.encode("utf-8")).digest()


SecretDecryptionFailureCode = Literal[
    "malformed_envelope",
    "unknown_key_version",
    "invalid_nonce",
    "invalid_payload",
    "integrity_check_failed",
    "invalid_plaintext",
]


class SecretDecryptionFailure(RuntimeError):
    def __init__(self, *, code: SecretDecryptionFailureCode) -> None:
        super().__init__(code)
        self.code = code


def _parse_connector_key_entries(configured_keys: str) -> dict[str, str]:
    normalized = configured_keys.strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
    except ValueError:
        entries: dict[str, str] = {}
        for raw_item in normalized.split(","):
            item = raw_item.strip()
            if not item:
                continue
            version, sep, key_value = item.partition(":")
            if not sep:
                msg = "connector_encryption_keys entry must be version:key"
                raise RuntimeError(msg)
            version_normalized = version.strip()
            key_normalized = key_value.strip()
            if not version_normalized or not key_normalized:
                msg = "connector_encryption_keys entry must not be blank"
                raise RuntimeError(msg)
            entries[version_normalized] = key_normalized
        return entries
    if not isinstance(payload, dict):
        msg = "connector_encryption_keys must be JSON object or version:key list"
        raise RuntimeError(msg)
    entries = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            msg = "connector_encryption_keys JSON must map string versions to string keys"
            raise RuntimeError(msg)
        key_normalized = key.strip()
        value_normalized = value.strip()
        if not key_normalized or not value_normalized:
            msg = "connector_encryption_keys JSON entries must not be blank"
            raise RuntimeError(msg)
        entries[key_normalized] = value_normalized
    return entries


def _decode_aead_key(raw_value: str) -> bytes:
    try:
        decoded = _urlsafe_b64decode(raw_value.strip())
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("connector encryption key must be base64url encoded") from exc
    if len(decoded) not in {16, 24, 32}:
        msg = "connector encryption key length must be 16, 24, or 32 bytes"
        raise RuntimeError(msg)
    return decoded


@dataclass(slots=True, frozen=True)
class SecretCipher:
    active_key_version: str
    keys_by_version: dict[str, bytes]
    single_secret_key: bytes | None = None

    def __post_init__(self) -> None:
        active = self.active_key_version.strip()
        if not active:
            raise RuntimeError("active_key_version must not be blank")
        if active not in self.keys_by_version:
            raise RuntimeError("active_key_version is missing from keys_by_version")
        copied: dict[str, bytes] = {}
        for version, key_bytes in self.keys_by_version.items():
            if not version.strip():
                raise RuntimeError("key version must not be blank")
            if len(key_bytes) not in {16, 24, 32}:
                raise RuntimeError("aead key length must be 16, 24, or 32 bytes")
            copied[version] = bytes(key_bytes)
        single_secret_key = self.single_secret_key
        if single_secret_key is not None and len(single_secret_key) not in {16, 24, 32}:
            raise RuntimeError("aead key length must be 16, 24, or 32 bytes")
        object.__setattr__(self, "active_key_version", active)
        object.__setattr__(self, "keys_by_version", copied)
        object.__setattr__(
            self,
            "single_secret_key",
            bytes(single_secret_key) if single_secret_key is not None else None,
        )

    @classmethod
    def from_config(
        cls,
        *,
        active_key_version: str,
        configured_keys: str | None,
        single_secret: str,
    ) -> SecretCipher:
        keys: dict[str, bytes] = {}
        if configured_keys is not None:
            entries = _parse_connector_key_entries(configured_keys)
            keys = {version: _decode_aead_key(raw_key) for version, raw_key in entries.items()}
        active = active_key_version.strip() or "v1"
        single_secret_key: bytes | None = None
        if not keys:
            single_secret_key = _derive_secret_bytes(single_secret)
            keys[active] = single_secret_key
        if active not in keys:
            msg = "active connector encryption key version is missing from configured keyring"
            raise RuntimeError(msg)
        return cls(
            active_key_version=active,
            keys_by_version=keys,
            single_secret_key=single_secret_key,
        )

    def encrypt(self, plaintext: str) -> str:
        key_bytes = self.keys_by_version[self.active_key_version]
        nonce = secrets.token_bytes(12)
        aad = f"ariel.connector.google:{self.active_key_version}".encode("utf-8")
        cipher = AESGCM(key_bytes)
        ciphertext = cipher.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return (
            f"aeadv1:{self.active_key_version}:"
            f"{_urlsafe_b64encode(nonce)}:{_urlsafe_b64encode(ciphertext)}"
        )

    def decrypt(self, ciphertext: str) -> str:
        if ciphertext.startswith("aeadv1:"):
            try:
                _, version, nonce_b64, payload_b64 = ciphertext.split(":", maxsplit=3)
            except ValueError as exc:
                raise SecretDecryptionFailure(code="malformed_envelope") from exc
            key_bytes = self.keys_by_version.get(version) or self.single_secret_key
            if key_bytes is None:
                raise SecretDecryptionFailure(code="unknown_key_version")
            try:
                nonce = _urlsafe_b64decode(nonce_b64)
            except (binascii.Error, ValueError) as exc:
                raise SecretDecryptionFailure(code="invalid_nonce") from exc
            if len(nonce) != 12:
                raise SecretDecryptionFailure(code="invalid_nonce")
            try:
                payload = _urlsafe_b64decode(payload_b64)
            except (binascii.Error, ValueError) as exc:
                raise SecretDecryptionFailure(code="invalid_payload") from exc
            aad = f"ariel.connector.google:{version}".encode("utf-8")
            try:
                plaintext = AESGCM(key_bytes).decrypt(nonce, payload, aad)
            except InvalidTag as exc:
                raise SecretDecryptionFailure(code="integrity_check_failed") from exc
            try:
                return plaintext.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SecretDecryptionFailure(code="invalid_plaintext") from exc
        raise SecretDecryptionFailure(code="malformed_envelope")


def encrypt_secret(
    *,
    plaintext: str,
    secret: str,
    key_version: str,
    encryption_keys: str | None = None,
) -> str:
    cipher = SecretCipher.from_config(
        active_key_version=key_version,
        configured_keys=encryption_keys,
        single_secret=secret,
    )
    return cipher.encrypt(plaintext)


def decrypt_secret(
    *,
    ciphertext: str,
    secret: str,
    expected_key_version: str,
    encryption_keys: str | None = None,
) -> str:
    cipher = SecretCipher.from_config(
        active_key_version=expected_key_version,
        configured_keys=encryption_keys,
        single_secret=secret,
    )
    return cipher.decrypt(ciphertext)
