"""Small helpers for consistent JSON response shapes across controllers."""
from typing import Any


def success(data: Any = None, message: str = "OK") -> dict:
    return {"message": message, "data": data}


def error(message: str) -> dict:
    return {"message": message}
