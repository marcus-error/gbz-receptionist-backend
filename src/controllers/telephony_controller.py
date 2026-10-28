from typing import Optional
from fastapi import Depends
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user, get_optional_current_user
from src.models.user import User
from src.services.telephony_service import TelephonyService
from src.validators.telephony_validator import (
    TelephonySettingsOut,
    TelephonySettingsUpdate,
    TestConnectionRequest,
    TestConnectionResponse,
    ValidateSipRequest,
    ValidateSipResponse,
    SipConfigIn,
    SipConfigOut,
    SipTestRequest,
    SipTestResponse,
    SipE2ETestRequest,
    SipE2ETestResponse,
    SipRegisterRequest,
    SipRegistrationStatusResponse,
    SipUnregisterResponse,
    SipOutboundCallRequest,
    SipOutboundCallResponse,
)


def _get_ids(current_user: Optional[User]):
    user_id = current_user.id if current_user else 1
    org_id = current_user.organization_id if current_user else 1
    return user_id, org_id


def get_settings(
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db)
) -> TelephonySettingsOut:
    user_id, org_id = _get_ids(current_user)
    return TelephonyService(db, user_id=user_id, org_id=org_id).get_settings()


def update_settings(
    payload: TelephonySettingsUpdate,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db)
) -> TelephonySettingsOut:
    user_id, org_id = _get_ids(current_user)
    return TelephonyService(db, user_id=user_id, org_id=org_id).update_settings(payload)


async def test_connection(
    payload: TestConnectionRequest,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db)
) -> TestConnectionResponse:
    user_id, org_id = _get_ids(current_user)
    result = await TelephonyService(db, user_id=user_id, org_id=org_id).test_connection(payload)
    return TestConnectionResponse(**result)


async def validate_sip(
    payload: ValidateSipRequest,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: Session = Depends(get_db)
) -> ValidateSipResponse:
    user_id, org_id = _get_ids(current_user)
    result = await TelephonyService(db, user_id=user_id, org_id=org_id).validate_sip(payload)
    return ValidateSipResponse(**result)


def get_sip_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipConfigOut:
    user_id, org_id = _get_ids(current_user)
    return TelephonyService(db, user_id=user_id, org_id=org_id).get_sip_config()


def update_sip_config(
    payload: SipConfigIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipConfigOut:
    user_id, org_id = _get_ids(current_user)
    return TelephonyService(db, user_id=user_id, org_id=org_id).update_sip_config(payload)


async def test_sip_connection(
    payload: SipTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipTestResponse:
    user_id, org_id = _get_ids(current_user)
    result = await TelephonyService(db, user_id=user_id, org_id=org_id).test_sip_connection(payload)
    return SipTestResponse(**result)


async def run_e2e_test(
    payload: Optional[SipE2ETestRequest] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipE2ETestResponse:
    user_id, org_id = _get_ids(current_user)
    if payload is None:
        payload = SipE2ETestRequest()
    result = await TelephonyService(db, user_id=user_id, org_id=org_id).run_e2e_test(payload)
    return SipE2ETestResponse(**result)


def register_sip_trunk(
    payload: Optional[SipRegisterRequest] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    """Trigger SIP REGISTER with FreeSWITCH / Asterisk."""
    user_id, org_id = _get_ids(current_user)
    if payload is None:
        payload = SipRegisterRequest()
    return TelephonyService(db, user_id=user_id, org_id=org_id).register_sip_trunk(payload)


def get_registration_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipRegistrationStatusResponse:
    """Return live SIP UA registration state."""
    user_id, org_id = _get_ids(current_user)
    result = TelephonyService(db, user_id=user_id, org_id=org_id).get_registration_status()
    return SipRegistrationStatusResponse(
        status=result.get("status", "unregistered"),
        detail=result.get("detail", ""),
        sip_host=result.get("sip_host"),
        sip_username=result.get("sip_username"),
        sip_port=result.get("sip_port", 5060),
        did_number=result.get("did_number"),
        registered_at=result.get("registered_at"),
        last_attempt_at=result.get("last_attempt_at"),
        error_message=result.get("error_message"),
        active_calls=result.get("active_calls", 0),
    )


def unregister_sip_trunk(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipUnregisterResponse:
    """Stop the SIP UA and unregister."""
    user_id, org_id = _get_ids(current_user)
    result = TelephonyService(db, user_id=user_id, org_id=org_id).unregister_sip_trunk()
    return SipUnregisterResponse(**result)


def make_outbound_call(
    payload: SipOutboundCallRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> SipOutboundCallResponse:
    user_id, org_id = _get_ids(current_user)
    result = TelephonyService(db, user_id=user_id, org_id=org_id).make_outbound_call(payload)
    return SipOutboundCallResponse(**result)
