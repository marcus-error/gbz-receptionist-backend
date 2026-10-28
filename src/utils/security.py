"""
Secure credential encryption and decryption at rest for multi-tenant SIP and secrets.
Uses AES-128-CBC + HMAC-SHA256 authenticated encryption via Fernet.
Supports backward-compatible transparent fallback for unencrypted legacy credentials.
"""
import base64
import hashlib
import logging
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

from src.config.jwt import JWT_SECRET_KEY

logger = logging.getLogger(__name__)

_fernet_instance: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is None:
        derived_key = base64.urlsafe_b64encode(
            hashlib.sha256(JWT_SECRET_KEY.encode("utf-8")).digest()
        )
        _fernet_instance = Fernet(derived_key)
    return _fernet_instance


def encrypt_secret(plain_text: Optional[str]) -> Optional[str]:
    """
    Encrypt sensitive credential string at rest.
    Returns URL-safe base64-encoded encrypted token string, or None if input is empty.
    Never logs the credential.
    """
    if plain_text is None:
        return None
    cleaned = str(plain_text).strip()
    if not cleaned:
        return None
    try:
        f = _get_fernet()
        return f.encrypt(cleaned.encode("utf-8")).decode("utf-8")
    except Exception as exc:
        logger.error(f"[Security] Failed to encrypt secret: {type(exc).__name__}")
        return cleaned


def decrypt_secret(cipher_text: Optional[str]) -> Optional[str]:
    """
    Decrypt sensitive credential string.
    If the text is an encrypted Fernet token, decrypts and returns plaintext.
    If the text is legacy plaintext (from prior to encryption-at-rest), returns as-is.
    Never logs the credential.
    """
    if cipher_text is None:
        return None
    cleaned = str(cipher_text).strip()
    if not cleaned:
        return None
    try:
        f = _get_fernet()
        return f.decrypt(cleaned.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        # Backward compatibility: legacy plaintext value in existing database
        return cleaned
