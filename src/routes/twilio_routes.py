from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.models.twilio_config import TwilioConfig

router = APIRouter(prefix="/twilio/config", tags=["Twilio"])

class TwilioConfigSchema(BaseModel):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    twilio_number: Optional[str] = None
    webhook_url: Optional[str] = None
    call_status_url: Optional[str] = None
    recording_status_url: Optional[str] = None

@router.get("", response_model=TwilioConfigSchema)
def get_twilio_config(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    config = db.query(TwilioConfig).filter(TwilioConfig.organization_id == org_id).first()
    if not config:
        return TwilioConfigSchema()
    return TwilioConfigSchema(
        account_sid=config.account_sid,
        auth_token=config.auth_token,
        twilio_number=config.twilio_number,
        webhook_url=config.webhook_url,
        call_status_url=config.call_status_url,
        recording_status_url=config.recording_status_url
    )

@router.post("", response_model=TwilioConfigSchema)
def update_twilio_config(
    payload: TwilioConfigSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    config = db.query(TwilioConfig).filter(TwilioConfig.organization_id == org_id).first()
    if not config:
        config = TwilioConfig(organization_id=org_id)
        db.add(config)
    
    config.account_sid = payload.account_sid
    config.auth_token = payload.auth_token
    config.twilio_number = payload.twilio_number
    config.webhook_url = payload.webhook_url
    config.call_status_url = payload.call_status_url
    config.recording_status_url = payload.recording_status_url

    db.commit()
    db.refresh(config)
    
    return TwilioConfigSchema(
        account_sid=config.account_sid,
        auth_token=config.auth_token,
        twilio_number=config.twilio_number,
        webhook_url=config.webhook_url,
        call_status_url=config.call_status_url,
        recording_status_url=config.recording_status_url
    )
