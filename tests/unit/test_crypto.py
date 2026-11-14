"""Encryption, token, and password unit tests."""

import base64

import pytest

from app.errors import CryptoError
from app.security.crypto import (
    derive_key,
    decrypt_secret,
    encrypt_secret,
    generate_export_account,
    generate_export_password,
    generate_token,
    hash_password,
    hash_token,
    load_master_key,
    mask_hmac,
    native_auth_string,
    token_prefix,
    verify_password,
)

MASTER = b"m" * 32


def test_load_master_key_roundtrip() -> None:
    encoded = base64.b64encode(MASTER).decode("ascii")
    assert load_master_key(encoded) == MASTER


@pytest.mark.parametrize("value", ["not-base64!!", base64.b64encode(b"short").decode("ascii")])
def test_load_master_key_invalid(value: str) -> None:
    with pytest.raises(CryptoError):
        load_master_key(value)


def test_encrypt_decrypt_roundtrip_and_random_nonce() -> None:
    first = encrypt_secret("p@ss word", MASTER)
    second = encrypt_secret("p@ss word", MASTER)
    assert first != second
    assert decrypt_secret(first, MASTER) == "p@ss word"
    assert decrypt_secret(second, MASTER) == "p@ss word"


def test_decrypt_with_wrong_key_fails() -> None:
    ciphertext = encrypt_secret("secret", MASTER)
    with pytest.raises(CryptoError):
        decrypt_secret(ciphertext, b"x" * 32)


def test_decrypt_invalid_payload_fails() -> None:
    with pytest.raises(CryptoError):
        decrypt_secret("not-a-cipher", MASTER)
    with pytest.raises(CryptoError):
        decrypt_secret(base64.b64encode(b"short").decode("ascii"), MASTER)


def test_derive_key_is_stable_and_purpose_separated() -> None:
    mask_key = derive_key(MASTER, b"dbgw-mask")
    session_key = derive_key(MASTER, b"dbgw-session")
    assert mask_key == derive_key(MASTER, b"dbgw-mask")
    assert mask_key != session_key
    assert len(mask_key) == 32


def test_token_generation_and_hash() -> None:
    token = generate_token()
    assert token.startswith("gw_")
    assert len(token) > 20
    assert hash_token(token) == hash_token(token)
    assert len(hash_token(token)) == 64
    assert token_prefix(token) == token[:10]


def test_password_hash_and_verify() -> None:
    password_hash = hash_password("correct horse")
    assert password_hash != "correct horse"
    assert verify_password("correct horse", password_hash)
    assert not verify_password("wrong", password_hash)
    assert not verify_password("correct horse", "not-a-bcrypt-hash")


def test_mask_hmac_is_stable() -> None:
    key = derive_key(MASTER, b"dbgw-mask")
    assert mask_hmac("13812341234", key) == mask_hmac("13812341234", key)
    assert mask_hmac("13812341234", key) != mask_hmac("13812341235", key)


def test_native_auth_string_matches_mysql_mimic() -> None:
    from mysql_mimic.auth import NativePasswordAuthPlugin

    for password in ("short", "p@ss:w/ord", generate_export_password()):
        assert native_auth_string(password) == NativePasswordAuthPlugin.create_auth_string(password)
        assert len(native_auth_string(password)) == 40
        assert native_auth_string(password) == native_auth_string(password).lower()


def test_export_password_shape() -> None:
    first = generate_export_password()
    second = generate_export_password()
    assert first != second
    assert len(first) == 32
    assert set(first) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def test_export_account_shape() -> None:
    account = generate_export_account()
    assert account.startswith("exp_")
    assert len(account) == 16
    assert account != generate_export_account()

