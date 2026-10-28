from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.services.webhook_registry import get_available_events
from src.services.webhook_service import WebhookService
from src.validators.webhook_validator import (
    WebhookCreate,
    WebhookDeliveryOut,
    WebhookOut,
    WebhookStatsOut,
    WebhookTestRequest,
    WebhookTestResponse,
    WebhookUpdate,
    WebhookWithSecretOut,
)


def _get_org_id(user: Optional[User]) -> Optional[int]:
    if not user:
        return 1
    if user.role == "super_admin" and user.organization_id is None:
        return None
    return user.organization_id or 1


def list_webhooks(
    search: Optional[str] = None,
    type: Optional[str] = None,
    is_active: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _get_org_id(current_user)
    return WebhookService(db).list_webhooks(
        organization_id=org_id,
        search=search,
        type_filter=type,
        is_active=is_active,
        page=page,
        page_size=page_size,
    )


def get_webhook(
    webhook_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).get_webhook(webhook_id, organization_id=org_id)


def create_webhook(
    payload: WebhookCreate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookWithSecretOut:
    org_id = _get_org_id(current_user) or 1
    return WebhookService(db).create_webhook(payload, organization_id=org_id)


def update_webhook(
    webhook_id: int,
    payload: WebhookUpdate,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).update_webhook(webhook_id, payload, organization_id=org_id)


def delete_webhook(
    webhook_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _get_org_id(current_user)
    return WebhookService(db).delete_webhook(webhook_id, organization_id=org_id)


def toggle_webhook(
    webhook_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).toggle_webhook(webhook_id, organization_id=org_id)


def enable_webhook(
    webhook_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).enable_webhook(webhook_id, organization_id=org_id)


def disable_webhook(
    webhook_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).disable_webhook(webhook_id, organization_id=org_id)


def rotate_secret(
    webhook_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookWithSecretOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).rotate_secret(webhook_id, organization_id=org_id)


def get_webhook_events() -> List[Dict[str, Any]]:
    """Return list of extensible application events available for webhooks."""
    return get_available_events()


def get_webhook_stats(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookStatsOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).get_stats(organization_id=org_id)


async def send_test(
    webhook_id: int,
    payload: Optional[WebhookTestRequest] = None,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _get_org_id(current_user)
    event_name = payload.event if payload else None
    custom_data = payload.custom_data if payload else None
    return await WebhookService(db).send_test(
        webhook_id=webhook_id,
        organization_id=org_id,
        event_name=event_name,
        custom_data=custom_data,
    )


def list_deliveries(
    webhook_id: Optional[int] = None,
    status: Optional[str] = None,
    event: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _get_org_id(current_user)
    return WebhookService(db).list_deliveries(
        organization_id=org_id,
        webhook_id=webhook_id,
        status_filter=status,
        event_filter=event,
        page=page,
        page_size=page_size,
    )


def get_delivery(
    delivery_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WebhookDeliveryOut:
    org_id = _get_org_id(current_user)
    return WebhookService(db).get_delivery(delivery_id, organization_id=org_id)


async def retry_delivery(
    delivery_id: int,
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    org_id = _get_org_id(current_user)
    return await WebhookService(db).retry_delivery(delivery_id, organization_id=org_id)


async def handle_incoming_webhook(
    identifier: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Public incoming webhook receiver endpoint.
    Accepts external POST, validates HMAC signature & timestamp, and processes event.
    """
    raw_body = await request.body()
    if len(raw_body) > 2 * 1024 * 1024:  # Max 2MB payload protection
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Webhook payload exceeds maximum size of 2MB",
        )

    headers_dict = dict(request.headers)
    payload_dict = {}
    if raw_body:
        try:
            import json
            payload_dict = json.loads(raw_body.decode("utf-8"))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON request body",
            )

    return WebhookService(db).process_incoming_webhook(
        identifier=identifier,
        raw_body=raw_body,
        headers=headers_dict,
        payload=payload_dict,
    )
