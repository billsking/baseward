"""Encryption and hashing for keys, passwords, and tokens.

No function logs plaintext; decryption failures raise CryptoError.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

import bcrypt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.errors import CryptoError

NONCE_BYTES = 12
KEY_BYTES = 32
TOKEN_PREFIX_LENGTH = 10
BCRYPT_ROUNDS = 12
EXPORT_PASSWORD_BYTES = 24
EXPORT_ACCOUNT_RANDOM_BYTES = 6


def load_master_key(value: str) -> bytes:
    """base64-decode the master key; its length must be 32 bytes."""
    try:
        key = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("主密钥不是合法的 base64") from exc
    if len(key) != KEY_BYTES:
        raise CryptoError("主密钥解码后必须为 32 字节")
    return key


def derive_key(master: bytes, info: bytes) -> bytes:
    """Derive a purpose-separated subkey with HKDF-SHA256."""
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info).derive(master)


def encrypt_secret(plaintext: str, key: bytes) -> str:
    """AES-GCM encryption, returning base64(nonce || ciphertext+tag)."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(token: str, key: bytes) -> str:
    """Decrypt the output of encrypt_secret; raise CryptoError on failure."""
    try:
        payload = base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CryptoError("密文格式错误") from exc
    if len(payload) <= NONCE_BYTES:
        raise CryptoError("密文长度错误")
    nonce, ciphertext = payload[:NONCE_BYTES], payload[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        raise CryptoError("密文解密失败") from exc


def generate_token() -> str:
    """Generate a plaintext user token with the gw_ prefix."""
    return "gw_" + secrets.token_urlsafe(32)


def generate_short_credential() -> str:
    """Generate a plaintext short-lived MCP credential with the sc_ prefix."""
    return "sc_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are stored as SHA-256 hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """First 10 characters of the plaintext, used to locate the token in audits."""
    return token[:TOKEN_PREFIX_LENGTH]


def mask_hmac(value: str, key: bytes) -> str:
    """HMAC-SHA256 masking; identical plaintext yields a stable output."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    """bcrypt-hash an admin password."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password; return False on an invalid hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def generate_export_password() -> str:
    """Generate an export credential password: 192 bits of entropy, URL-safe, ready for a connection string."""
    return secrets.token_urlsafe(EXPORT_PASSWORD_BYTES)


def native_auth_string(password: str) -> str:
    """auth_string for mysql_native_password: hex of SHA1(SHA1(password))."""
    inner = hashlib.sha1(password.encode("utf-8")).digest()
    return hashlib.sha1(inner).hexdigest()


def generate_export_account() -> str:
    """Generate an exp_-prefixed export account name, 16 characters total."""
    return "exp_" + secrets.token_hex(EXPORT_ACCOUNT_RANDOM_BYTES)

