from fastapi import APIRouter

from src.controllers import settings_controller
from src.validators.settings_validator import SettingsOut

router = APIRouter(prefix="/settings", tags=["Settings"])

router.add_api_route("", settings_controller.get_settings, methods=["GET"], response_model=SettingsOut)
router.add_api_route("", settings_controller.update_settings, methods=["PUT"], response_model=SettingsOut)
