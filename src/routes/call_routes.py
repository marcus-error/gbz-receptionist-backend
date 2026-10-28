from typing import List, Optional
from fastapi import APIRouter

from src.controllers import call_controller
from src.validators.call_validator import CallLogOut

router = APIRouter(prefix="/calls", tags=["Calls"])

router.add_api_route("", call_controller.list_calls, methods=["GET"], response_model=List[CallLogOut])
router.add_api_route("/latest", call_controller.get_latest_call, methods=["GET"], response_model=Optional[CallLogOut])
router.add_api_route("", call_controller.create_call, methods=["POST"], response_model=CallLogOut, status_code=201)
