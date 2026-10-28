from fastapi import Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.services.auth_service import AuthService
from src.validators.auth_validator import LoginRequest, RegisterRequest, TokenResponse


def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    return AuthService(db).register(payload)


def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    return AuthService(db).login(payload)
