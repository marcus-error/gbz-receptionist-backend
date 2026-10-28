from fastapi import APIRouter, Depends
from src.controllers import auth_controller
from src.middleware.auth_middleware import get_current_user
from src.validators.auth_validator import TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["Auth"])

router.add_api_route("/register", auth_controller.register, methods=["POST"], response_model=TokenResponse, status_code=201)
router.add_api_route("/login", auth_controller.login, methods=["POST"], response_model=TokenResponse)

@router.get("/me", response_model=UserOut)
def get_me(current_user=Depends(get_current_user)):
    return current_user
