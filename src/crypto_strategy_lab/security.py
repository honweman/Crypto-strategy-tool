from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 310_000


def hash_password(password: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    if not password:
        raise ValueError("password cannot be empty")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    encoded = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"{PASSWORD_ALGORITHM}${iterations}${salt}${encoded}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt, encoded = password_hash.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iterations_raw)
        expected = base64.urlsafe_b64decode(encoded.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:4]}...{value[-4:]}"


@dataclass(frozen=True)
class SecretBox:
    secret_key: str

    @classmethod
    def from_env(cls) -> "SecretBox":
        secret = os.environ.get("CRYPTO_LAB_SECRET_KEY", "dev-secret-change-before-deploy")
        return cls(secret)

    def _fernet(self) -> Fernet:
        digest = hashlib.sha256(self.secret_key.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def encrypt(self, value: str | None) -> str:
        if value is None:
            value = ""
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted: str | None) -> str:
        if not encrypted:
            return ""
        try:
            return self._fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("unable to decrypt value with current secret key") from exc

