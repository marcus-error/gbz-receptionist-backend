import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.whatsapp_config import WhatsAppConfig
from src.models.appointment import Appointment, AppointmentStatus

router = APIRouter(prefix="/whatsapp", tags=["WhatsApp"])


class WhatsAppConfigSchema(BaseModel):
    provider: Optional[str] = "Meta WhatsApp Cloud API"
    phone_number: Optional[str] = None
    phone_number_id: Optional[str] = None
    waba_id: Optional[str] = None
    access_token: Optional[str] = None
    template: Optional[str] = None
    webhook_url: Optional[str] = None
    webhook_verify_token: Optional[str] = None
    is_configured: Optional[bool] = False
    has_token: Optional[bool] = False


class TemplateSchema(BaseModel):
    name: str
    language: str = "en_US"
    category: str = "UTILITY"
    components: List[dict] = Field(default_factory=list)


class SendMessageSchema(BaseModel):
    to: str
    text: Optional[str] = None
    template_name: Optional[str] = None
    language: str = "en_US"
    parameters: List[str] = Field(default_factory=list)


class AppointmentMessageSchema(BaseModel):
    appointment_id: int
    template_name: Optional[str] = None
    language: str = "en_US"


def _config(db, organization_id: int = 1):
    org_id = organization_id if (organization_id and organization_id != "None") else 1
    # 1. Prefer configured row with valid phone_number_id
    config = db.query(WhatsAppConfig).filter(
        WhatsAppConfig.organization_id == org_id,
        WhatsAppConfig.phone_number_id.isnot(None),
        WhatsAppConfig.phone_number_id != ""
    ).order_by(WhatsAppConfig.updated_at.desc(), WhatsAppConfig.id.desc()).first()

    # 2. Fall back to any row for this org
    if not config:
        config = db.query(WhatsAppConfig).filter(
            WhatsAppConfig.organization_id == org_id
        ).order_by(WhatsAppConfig.updated_at.desc(), WhatsAppConfig.id.desc()).first()

    if not config:
        default_tpl = (
            "Hello {{customer_name}},\n\n"
            "This is a confirmation for your appointment with {{staff_name}} on {{date}} at {{time}}.\n\n"
            "Department: {{department}}\n"
            "Thank you for choosing CallZenza AI Receptionist."
        )
        config = WhatsAppConfig(
            organization_id=org_id,
            provider="Meta WhatsApp Cloud API",
            phone_number=None,
            phone_number_id=None,
            waba_id=None,
            access_token=None,
            template=default_tpl
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _token(config):
    token = config.access_token
    phone_id = config.phone_number_id
    if not token or not phone_id:
        raise HTTPException(
            400,
            "WhatsApp is not configured for your organization. Please enter your Meta API credentials.",
        )
    return token, phone_id



def _graph_url(phone_id):
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
    return f"https://graph.facebook.com/{version}/{phone_id}/messages"


def _send(db, payload):
    config = _config(db)
    token, phone_id = _token(config)

    # Sanitize recipient phone number for Meta Graph API (digits only)
    if isinstance(payload, dict) and "to" in payload and isinstance(payload["to"], str):
        payload["to"] = "".join(c for c in payload["to"] if c.isdigit())

    response = requests.post(
        _graph_url(phone_id),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    data = response.json() if response.content else {}
    if not response.ok:
        err_msg = None
        if isinstance(data, dict):
            err_info = data.get("error", {})
            if isinstance(err_info, dict):
                err_msg = err_info.get("message")
        if not err_msg:
            err_msg = str(data) if data else f"WhatsApp API HTTP {response.status_code}"
        if response.status_code == 401:
            raise HTTPException(400, f"Meta WhatsApp Access Token Invalid (HTTP 401): {err_msg}")
        raise HTTPException(response.status_code, err_msg)
    return data


def _validate_template_payload(template):
    if not template.name or not template.name.replace("_", "").isalnum():
        raise HTTPException(400, "Template name must contain letters, numbers and underscores")
    if not template.language:
        raise HTTPException(400, "Template language is required")
    if template.category not in {"UTILITY", "MARKETING", "AUTHENTICATION"}:
        raise HTTPException(400, "Invalid WhatsApp template category")
    return True


from src.middleware.auth_middleware import get_current_user
from src.models.user import User

@router.get("/config", response_model=WhatsAppConfigSchema)
def get_whatsapp_config(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = (current_user.organization_id if (current_user and current_user.organization_id) else 1)
    config = _config(db, organization_id=org_id)
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "receptionist_verify_token_2026")
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/api/v1/whatsapp/webhook"
    
    default_template = (
        "Hello {{customer_name}},\n\n"
        "This is a confirmation for your appointment with {{staff_name}} on {{date}} at {{time}}.\n\n"
        "Department: {{department}}\n"
        "Thank you for choosing CallZenza AI Receptionist."
    )

    return WhatsAppConfigSchema(
        provider=config.provider or "Meta WhatsApp Cloud API",
        phone_number=config.phone_number,
        phone_number_id=config.phone_number_id,
        waba_id=config.waba_id,
        access_token="••••••••••••••••" if config.access_token else None,
        template=config.template or default_template,
        webhook_url=webhook_url,
        webhook_verify_token=verify_token,
        is_configured=bool(config.phone_number_id and config.access_token),
        has_token=bool(config.access_token),
    )


@router.post("/config", response_model=WhatsAppConfigSchema)
def update_whatsapp_config(
    request: Request,
    payload: WhatsAppConfigSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = (current_user.organization_id if (current_user and current_user.organization_id) else 1)
    config = _config(db, organization_id=org_id)
    config.provider = payload.provider or "Meta WhatsApp Cloud API"
    config.phone_number = payload.phone_number
    config.phone_number_id = payload.phone_number_id
    config.waba_id = payload.waba_id
    if payload.access_token and payload.access_token != "••••••••••••••••":
        config.access_token = payload.access_token
    if payload.template is not None:
        config.template = payload.template
    db.commit()
    db.refresh(config)
    return get_whatsapp_config(request, db, current_user=current_user)


@router.get("/validate")
def validate_credentials(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = (current_user.organization_id if (current_user and current_user.organization_id) else 1)
    config = _config(db, organization_id=org_id)
    if not config.access_token or not config.phone_number_id:
        return {
            "valid": False,
            "error": "WhatsApp credentials not configured. Please enter your Phone Number ID and Access Token."
        }
    token, phone_id = config.access_token, config.phone_number_id
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
    try:
        response = requests.get(
            f"https://graph.facebook.com/{version}/{phone_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,display_phone_number,verified_name,code_verification_status,quality_rating"},
            timeout=15,
        )
        data = response.json() if response.content else {}
        if not response.ok:
            err_info = data.get("error", {}) if isinstance(data, dict) else {}
            err_msg = err_info.get("message") or str(data)
            error_code = err_info.get("code")
            error_subcode = err_info.get("error_subcode")
            is_expired = bool(error_code == 190 or "expired" in str(err_msg).lower())
            return {
                "valid": False,
                "error": err_msg,
                "is_expired": is_expired,
                "code": error_code,
                "subcode": error_subcode,
            }
        return {
            "valid": True,
            "phone_number_id": phone_id,
            "display_phone_number": data.get("display_phone_number"),
            "verified_name": data.get("verified_name"),
            "quality_rating": data.get("quality_rating", "GREEN"),
            "details": data
        }
    except Exception as exc:
        return {"valid": False, "error": f"Network error contacting Meta Graph API: {str(exc)}"}


@router.get("/templates")
def list_templates(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    config = _config(db, organization_id=org_id)
    token, _ = _token(config)
    waba_id = config.waba_id
    if not waba_id:
        raise HTTPException(400, "WhatsApp WABA ID is not configured for your organization")
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
    response = requests.get(
        f"https://graph.facebook.com/{version}/{waba_id}/message_templates",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    data = response.json() if response.content else {}
    if not response.ok:
        raise HTTPException(response.status_code, data)
    return data.get("data", [])


@router.post("/templates")
def validate_and_register_template(
    payload: TemplateSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    _validate_template_payload(payload)
    org_id = current_user.organization_id if current_user else 1
    config = _config(db, organization_id=org_id)
    token, _ = _token(config)
    waba_id = config.waba_id
    if not waba_id:
        raise HTTPException(400, "WhatsApp WABA ID is not configured for your organization")
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v23.0")
    response = requests.post(
        f"https://graph.facebook.com/{version}/{waba_id}/message_templates",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "name": payload.name,
            "language": payload.language,
            "category": payload.category,
            "components": payload.components,
        },
        timeout=20,
    )
    data = response.json() if response.content else {}
    if not response.ok:
        raise HTTPException(response.status_code, data)
    return data



@router.post("/messages")
def send_message(payload: SendMessageSchema, db: Session = Depends(get_db)):
    if payload.template_name:
        body = {
            "messaging_product": "whatsapp",
            "to": payload.to,
            "type": "template",
            "template": {
                "name": payload.template_name,
                "language": {"code": payload.language},
            },
        }
        if payload.parameters:
            body["template"]["components"] = [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": p} for p in payload.parameters
                ],
            }]
    elif payload.text:
        body = {
            "messaging_product": "whatsapp",
            "to": payload.to,
            "type": "text",
            "text": {"preview_url": False, "body": payload.text},
        }
    else:
        raise HTTPException(400, "Provide text or template_name")

    return _send(db, body)


def _appointment(db, appointment_id):
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(404, "Appointment not found")
    if not appt.phone:
        raise HTTPException(400, "Appointment has no customer phone number")
    return appt


def _appointment_params(appt):
    return [
        appt.customer,
        appt.department,
        appt.staff,
        appt.appointment_date,
        appt.appointment_time,
    ]


@router.post("/appointments/confirmation")
def send_appointment_confirmation(
    payload: AppointmentMessageSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    template = payload.template_name or "appointment_confirmation"
    return _send(db, {
        "messaging_product": "whatsapp",
        "to": appt.phone,
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": payload.language},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": str(x)}
                    for x in _appointment_params(appt)
                ],
            }],
        },
    })


@router.post("/appointments/details")
def send_appointment_details(
    payload: AppointmentMessageSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    return _send(db, {
        "messaging_product": "whatsapp",
        "to": appt.phone,
        "type": "text",
        "text": {"body": (
            f"Appointment #{appt.id}\n"
            f"Customer: {appt.customer}\n"
            f"Department: {appt.department}\n"
            f"Staff: {appt.staff}\n"
            f"Date: {appt.appointment_date}\n"
            f"Time: {appt.appointment_time}\n"
            f"Duration: {appt.duration}\n"
            f"Status: {appt.status.value if hasattr(appt.status, 'value') else appt.status}"
        )},
    })


@router.post("/appointments/cancellation")
def send_cancellation_message(
    payload: AppointmentMessageSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    return _send(db, {
        "messaging_product": "whatsapp",
        "to": appt.phone,
        "type": "text",
        "text": {"body": (
            f"Your appointment #{appt.id} on {appt.appointment_date} "
            f"at {appt.appointment_time} has been cancelled."
        )},
    })


@router.post("/appointments/reschedule")
def send_reschedule_message(
    payload: AppointmentMessageSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    return _send(db, {
        "messaging_product": "whatsapp",
        "to": appt.phone,
        "type": "text",
        "text": {"body": (
            f"Your appointment #{appt.id} has been rescheduled to "
            f"{appt.appointment_date} at {appt.appointment_time} "
            f"with {appt.staff}."
        )},
    })


@router.post("/reminders/{hours_before}")
def send_reminder(
    hours_before: int,
    template_name: str = "appointment_reminder",
    language: str = "en_US",
    db: Session = Depends(get_db),
):
    if hours_before not in (24, 1):
        raise HTTPException(400, "Only 24-hour and 1-hour reminders are supported")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target_start = now + timedelta(hours=hours_before)
    target_end = target_start + timedelta(minutes=5)

    sent = []
    failed = []

    # Appointment dates are stored as display strings in the existing schema.
    for appt in db.query(Appointment).filter(
        Appointment.status == AppointmentStatus.CONFIRMED
    ).all():
        try:
            start = datetime.strptime(
                f"{appt.appointment_date} {appt.appointment_time}",
                "%d %b %Y %I:%M %p",
            )
        except ValueError:
            continue

        if not (target_start <= start <= target_end) or not appt.phone:
            continue

        try:
            result = _send(db, {
                "messaging_product": "whatsapp",
                "to": appt.phone,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(x)}
                            for x in _appointment_params(appt)
                        ],
                    }],
                },
            })
            sent.append({"appointment_id": appt.id, "result": result})
        except Exception as exc:
            failed.append({"appointment_id": appt.id, "error": str(exc)})

    return {
        "hours_before": hours_before,
        "template": template_name,
        "delivery_status": {
            "sent": len(sent),
            "failed": len(failed),
        },
        "sent": sent,
        "failed": failed,
    }


@router.get("/delivery-status")
def delivery_status(status: Optional[str] = Query(None)):
    """
    Delivery statuses are received asynchronously through the webhook.
    The endpoint is kept as a health/query contract; production persistence
    can be backed by a dedicated message-status table.
    """
    return {
        "status_filter": status,
        "note": "WhatsApp delivery/read/failed events are handled by /webhook",
    }


@router.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
):
    expected = os.getenv("WHATSAPP_VERIFY_TOKEN", "receptionist_verify_token_2026")
    if hub_mode == "subscribe" and expected and hmac.compare_digest(
        hub_verify_token, expected
    ):
        return int(hub_challenge)
    raise HTTPException(403, "Invalid webhook verification")


@router.post("/webhook")
async def whatsapp_webhook(request: Request):
    body = await request.json()

    # Meta sends message statuses under entry[].changes[].value.statuses[].
    # Failed messages are explicitly preserved in the response for callers
    # and can be persisted by adding a WhatsAppMessage model later.
    statuses = []
    messages = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for s in value.get("statuses", []):
                statuses.append({
                    "id": s.get("id"),
                    "status": s.get("status"),
                    "timestamp": s.get("timestamp"),
                    "recipient_id": s.get("recipient_id"),
                    "errors": s.get("errors", []),
                })
            messages.extend(value.get("messages", []))

    failed = [s for s in statuses if s.get("status") == "failed"]

    return {
        "received": True,
        "delivery_statuses": statuses,
        "failed_messages": failed,
        "messages": messages,
    }
