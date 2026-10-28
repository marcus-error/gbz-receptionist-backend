"""JWT helpers, kept isolated so auth logic never touches raw secrets directly."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt

from src.config.env import settings

JWT_SECRET_KEY = getattr(settings, "JWT_SECRET_KEY", None) or getattr(settings, "JWT_SECRET", "super_secret_jwt_key_12345") or "super_secret_jwt_key_12345"
EXPIRE_MINUTES = getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", None) or getattr(settings, "JWT_EXPIRE_MINUTES", 60) or 60
ALGORITHM = getattr(settings, "JWT_ALGORITHM", "HS256")


def create_access_token(payload: Dict[str, Any], expires_minutes: Optional[int] = None) -> str:
    to_encode = payload.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or EXPIRE_MINUTES
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
