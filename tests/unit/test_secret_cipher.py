from __future__ import annotations

import json
import os

import pytest

from ariel.secret_cipher import SecretCipher, SecretDecryptionFailure


def test_secret_cipher_round_trip_uses_aead_envelope_format() -> None:
    cipher = SecretCipher(
        active_key_version="v2",
        keys_by_version={
            "v1": os.urandom(32),
            "v2": os.urandom(32),
        },
    )
    plaintext = "tok_live_secret"

    encrypted = cipher.encrypt(plaintext)
    assert encrypted.startswith("aeadv1:v2:")
    assert plaintext not in encrypted
    assert cipher.decrypt(encrypted) == plaintext


def test_secret_cipher_decrypts_previous_key_version() -> None:
    key_v1 = os.urandom(32)
    key_v2 = os.urandom(32)
    previous_key_cipher = SecretCipher(
        active_key_version="v1",
        keys_by_version={"v1": key_v1},
    )
    token = "tok_previous_key"
    ciphertext = previous_key_cipher.encrypt(token)

    rotated_cipher = SecretCipher(
        active_key_version="v2",
        keys_by_version={"v1": key_v1, "v2": key_v2},
    )
    assert rotated_cipher.decrypt(ciphertext) == token


def test_secret_cipher_allows_single_secret_dev_key_version_relabel() -> None:
    v1_cipher = SecretCipher.from_config(
        active_key_version="v1",
        configured_keys=None,
        single_secret="shared-dev-secret",
    )
    ciphertext = v1_cipher.encrypt("tok_single_secret")

    v2_cipher = SecretCipher.from_config(
        active_key_version="v2",
        configured_keys=None,
        single_secret="shared-dev-secret",
    )
    assert v2_cipher.decrypt(ciphertext) == "tok_single_secret"


@pytest.mark.parametrize("raw_key", ["not@base64", "\u00e5"])
def test_secret_cipher_config_rejects_invalid_base64url_keys(raw_key: str) -> None:
    with pytest.raises(RuntimeError, match="connector encryption key must be base64url encoded"):
        SecretCipher.from_config(
            active_key_version="v1",
            configured_keys=json.dumps({"v1": raw_key}),
            single_secret="shared-dev-secret",
        )


def test_secret_cipher_config_rejects_invalid_key_lengths() -> None:
    with pytest.raises(RuntimeError, match="connector encryption key length"):
        SecretCipher.from_config(
            active_key_version="v1",
            configured_keys=json.dumps({"v1": "AA"}),
            single_secret="shared-dev-secret",
        )


def test_secret_cipher_rejects_non_aead_ciphertext() -> None:
    cipher = SecretCipher.from_config(
        active_key_version="v1",
        configured_keys=None,
        single_secret="shared-dev-secret",
    )

    with pytest.raises(SecretDecryptionFailure) as raised:
        cipher.decrypt("not-aead:v1")
    assert raised.value.code == "malformed_envelope"


def test_secret_cipher_decrypts_failures_are_typed() -> None:
    key_v1 = os.urandom(32)
    cipher = SecretCipher(active_key_version="v1", keys_by_version={"v1": key_v1})
    ciphertext = cipher.encrypt("tok_secret")
    _, version, nonce, payload = ciphertext.split(":", maxsplit=3)
    tampered_payload = f"{'A' if payload[0] != 'A' else 'B'}{payload[1:]}"
    cases = [
        ("aeadv1:v1", "malformed_envelope"),
        (f"aeadv1:v2:{nonce}:{payload}", "unknown_key_version"),
        (f"aeadv1:{version}:short:{payload}", "invalid_nonce"),
        (f"aeadv1:{version}:{nonce}:not@base64", "invalid_payload"),
        (f"aeadv1:{version}:{nonce}:{tampered_payload}", "integrity_check_failed"),
    ]

    for invalid_ciphertext, expected_code in cases:
        with pytest.raises(SecretDecryptionFailure) as raised:
            cipher.decrypt(invalid_ciphertext)
        assert raised.value.code == expected_code
