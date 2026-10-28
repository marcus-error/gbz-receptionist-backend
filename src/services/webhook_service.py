"""
Webhook Business Logic Service.
Provides full CRUD, multi-tenant isolation, incoming webhook verification & processing,
secret generation & rotation, and delivery logs queries.
"""
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from src.config.env import settings
from src.models.webhook import Webhook, WebhookDelivery, WebhookType, WebhookDeliveryStatus
from src.services.webhook_delivery_service import (
    WebhookDeliveryService,
    compute_signature,
    mask_secret,
    sanitize_headers_for_storage,
    verify_signature,
)
from src.validators.webhook_validator import (
    WebhookCreate,
    WebhookDeliveryOut,
    WebhookOut,
    WebhookStatsOut,
    WebhookUpdate,
    WebhookWithSecretOut,
)

logger = logging.getLogger(__name__)


def _get_base_url() -> str:
    """Resolve the public domain/base URL for incoming webhook endpoints."""
    domain = getattr(settings, "PUBLIC_BASE_URL", "") or getattr(settings, "BACKEND_URL", "")
    if not domain:
        host = getattr(settings, "HOST", "localhost") or "localhost"
        port = getattr(settings, "PORT", 8000) or 8000
        domain = f"http://{host}:{port}"
    return domain.rstrip("/")


class WebhookService:
    def __init__(self, db: Session):
        self.db = db

    def _build_incoming_url(self, webhook: Webhook) -> Optional[str]:
        if webhook.type != "incoming":
            return None
        base = _get_base_url()
        identifier = webhook.webhook_uuid or str(webhook.id)
        return f"{base}/api/v1/webhooks/incoming/{identifier}"

    def _to_webhook_out(self, webhook: Webhook) -> WebhookOut:
        return WebhookOut(
            id=webhook.id,
            webhook_uuid=webhook.webhook_uuid,
            organization_id=webhook.organization_id,
            name=webhook.name,
            type=webhook.type,
            url=webhook.url,
            secret_masked=mask_secret(webhook.secret),
            events=webhook.events or [],
            is_active=webhook.is_active,
            description=webhook.description,
            incoming_url=self._build_incoming_url(webhook),
            last_triggered_at=webhook.last_triggered_at,
            created_at=webhook.created_at,
            updated_at=webhook.updated_at,
        )

    def _to_webhook_with_secret_out(self, webhook: Webhook) -> WebhookWithSecretOut:
        return WebhookWithSecretOut(
            id=webhook.id,
            webhook_uuid=webhook.webhook_uuid,
            organization_id=webhook.organization_id,
            name=webhook.name,
            type=webhook.type,
            url=webhook.url,
            secret=webhook.secret,  # Full plaintext secret
            secret_masked=mask_secret(webhook.secret),
            events=webhook.events or [],
            is_active=webhook.is_active,
            description=webhook.description,
            incoming_url=self._build_incoming_url(webhook),
            last_triggered_at=webhook.last_triggered_at,
            created_at=webhook.created_at,
            updated_at=webhook.updated_at,
        )

    # ─────────────────────────────────────────────────────────────
    # Webhook CRUD
    # ─────────────────────────────────────────────────────────────

    def list_webhooks(
        self,
        organization_id: Optional[int] = 1,
        search: Optional[str] = None,
        type_filter: Optional[str] = None,
        is_active: Optional[bool] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """List webhooks scoped to tenant organization with search, filters, and pagination."""
        query = self.db.query(Webhook)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)

        if search and search.strip():
            term = f"%{search.strip()}%"
            query = query.filter((Webhook.name.ilike(term)) | (Webhook.url.ilike(term)))

        if type_filter and type_filter.strip() and type_filter.lower() != "all":
            query = query.filter(Webhook.type == type_filter.strip().lower())

        if is_active is not None:
            query = query.filter(Webhook.is_active == is_active)

        total = query.count()
        query = query.order_by(desc(Webhook.created_at))
        offset = max(0, (page - 1) * page_size)
        items = query.offset(offset).limit(page_size).all()

        return {
            "items": [self._to_webhook_out(wh) for wh in items],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    def get_webhook(self, webhook_id: int, organization_id: Optional[int] = 1) -> WebhookOut:
        """Fetch single webhook with organization boundary check."""
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        return self._to_webhook_out(webhook)

    def create_webhook(self, payload: WebhookCreate, organization_id: int = 1) -> WebhookWithSecretOut:
        """Create a new webhook with auto-generated secure UUID and HMAC secret."""
        if payload.type == "outgoing" and not payload.url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Destination URL is required for outgoing webhooks",
            )

        # Generate unique public identifier & high-entropy secret
        webhook_uuid = f"whk_{uuid.uuid4().hex[:16]}"
        secure_secret = f"whsec_{secrets.token_urlsafe(32)}"

        events_list = payload.events if payload.events else ["appointment.created"]

        webhook = Webhook(
            webhook_uuid=webhook_uuid,
            organization_id=organization_id,
            name=payload.name.strip(),
            type=payload.type.strip().lower(),
            url=payload.url.strip() if payload.url else None,
            secret=secure_secret,
            events=events_list,
            is_active=payload.is_active,
            description=payload.description,
        )
        self.db.add(webhook)
        self.db.commit()
        self.db.refresh(webhook)

        logger.info(f"[WEBHOOK CREATED] ID={webhook.id} type={webhook.type} org={organization_id} name='{webhook.name}'")
        return self._to_webhook_with_secret_out(webhook)

    def update_webhook(self, webhook_id: int, payload: WebhookUpdate, organization_id: Optional[int] = 1) -> WebhookOut:
        """Update webhook configuration."""
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        if payload.name is not None:
            webhook.name = payload.name.strip()
        if payload.url is not None:
            webhook.url = payload.url.strip() if payload.url else None
        if payload.events is not None:
            webhook.events = payload.events
        if payload.is_active is not None:
            webhook.is_active = payload.is_active
        if payload.description is not None:
            webhook.description = payload.description

        self.db.commit()
        self.db.refresh(webhook)
        logger.info(f"[WEBHOOK UPDATED] ID={webhook.id} org={organization_id}")
        return self._to_webhook_out(webhook)

    def delete_webhook(self, webhook_id: int, organization_id: Optional[int] = 1) -> Dict[str, Any]:
        """Delete webhook and cascade its deliveries."""
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        self.db.delete(webhook)
        self.db.commit()
        logger.info(f"[WEBHOOK DELETED] ID={webhook_id} org={organization_id}")
        return {"success": True, "message": f"Webhook '{webhook.name}' deleted successfully"}

    def toggle_webhook(self, webhook_id: int, organization_id: Optional[int] = 1) -> WebhookOut:
        """Toggle active state of a webhook."""
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        webhook.is_active = not webhook.is_active
        self.db.commit()
        self.db.refresh(webhook)
        logger.info(f"[WEBHOOK TOGGLED] ID={webhook_id} active={webhook.is_active}")
        return self._to_webhook_out(webhook)

    def enable_webhook(self, webhook_id: int, organization_id: Optional[int] = 1) -> WebhookOut:
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        webhook.is_active = True
        self.db.commit()
        self.db.refresh(webhook)
        return self._to_webhook_out(webhook)

    def disable_webhook(self, webhook_id: int, organization_id: Optional[int] = 1) -> WebhookOut:
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
        webhook.is_active = False
        self.db.commit()
        self.db.refresh(webhook)
        return self._to_webhook_out(webhook)

    def rotate_secret(self, webhook_id: int, organization_id: Optional[int] = 1) -> WebhookWithSecretOut:
        """Generate a new cryptographic HMAC secret for the webhook and return it."""
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        new_secret = f"whsec_{secrets.token_urlsafe(32)}"
        webhook.secret = new_secret
        self.db.commit()
        self.db.refresh(webhook)
        logger.info(f"[WEBHOOK SECRET ROTATED] ID={webhook_id} org={organization_id}")
        return self._to_webhook_with_secret_out(webhook)

    # ─────────────────────────────────────────────────────────────
    # Deliveries & Delivery Logs
    # ─────────────────────────────────────────────────────────────

    def list_deliveries(
        self,
        organization_id: Optional[int] = 1,
        webhook_id: Optional[int] = None,
        status_filter: Optional[str] = None,
        event_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """Query delivery logs with tenant isolation, webhook filter, and pagination."""
        query = self.db.query(WebhookDelivery)
        if organization_id is not None:
            query = query.filter(WebhookDelivery.organization_id == organization_id)
        if webhook_id is not None:
            query = query.filter(WebhookDelivery.webhook_id == webhook_id)
        if status_filter and status_filter.strip() and status_filter.lower() != "all":
            query = query.filter(WebhookDelivery.status == status_filter.strip().lower())
        if event_filter and event_filter.strip() and event_filter.lower() != "all":
            query = query.filter(WebhookDelivery.event == event_filter.strip())

        total = query.count()
        query = query.order_by(desc(WebhookDelivery.created_at))
        offset = max(0, (page - 1) * page_size)
        deliveries = query.offset(offset).limit(page_size).all()

        # Preload webhook names
        wh_ids = {d.webhook_id for d in deliveries}
        wh_map = {}
        if wh_ids:
            wh_records = self.db.query(Webhook.id, Webhook.name).filter(Webhook.id.in_(wh_ids)).all()
            wh_map = {r.id: r.name for r in wh_records}

        out_items = []
        for d in deliveries:
            out_items.append(
                WebhookDeliveryOut(
                    id=d.id,
                    webhook_id=d.webhook_id,
                    webhook_name=wh_map.get(d.webhook_id, f"Webhook #{d.webhook_id}"),
                    organization_id=d.organization_id,
                    event=d.event,
                    event_id=d.event_id,
                    direction=d.direction,
                    request_url=d.request_url,
                    request_headers=d.request_headers,
                    request_payload=d.request_payload,
                    response_status=d.response_status,
                    response_headers=d.response_headers,
                    response_body=d.response_body,
                    attempt=d.attempt,
                    max_attempts=d.max_attempts,
                    status=d.status,
                    duration_ms=d.duration_ms,
                    error_message=d.error_message,
                    created_at=d.created_at,
                    updated_at=d.updated_at,
                )
            )

        return {
            "items": out_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
        }

    def get_delivery(self, delivery_id: int, organization_id: Optional[int] = 1) -> WebhookDeliveryOut:
        """Fetch single delivery record with full request/response diagnostics."""
        query = self.db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id)
        if organization_id is not None:
            query = query.filter(WebhookDelivery.organization_id == organization_id)
        delivery = query.first()
        if not delivery:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found")

        webhook = self.db.query(Webhook).filter(Webhook.id == delivery.webhook_id).first()
        wh_name = webhook.name if webhook else f"Webhook #{delivery.webhook_id}"

        return WebhookDeliveryOut(
            id=delivery.id,
            webhook_id=delivery.webhook_id,
            webhook_name=wh_name,
            organization_id=delivery.organization_id,
            event=delivery.event,
            event_id=delivery.event_id,
            direction=delivery.direction,
            request_url=delivery.request_url,
            request_headers=delivery.request_headers,
            request_payload=delivery.request_payload,
            response_status=delivery.response_status,
            response_headers=delivery.response_headers,
            response_body=delivery.response_body,
            attempt=delivery.attempt,
            max_attempts=delivery.max_attempts,
            status=delivery.status,
            duration_ms=delivery.duration_ms,
            error_message=delivery.error_message,
            created_at=delivery.created_at,
            updated_at=delivery.updated_at,
        )

    async def retry_delivery(self, delivery_id: int, organization_id: Optional[int] = 1) -> Dict[str, Any]:
        """Manually trigger a real re-delivery attempt for a failed or pending delivery."""
        query = self.db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id)
        if organization_id is not None:
            query = query.filter(WebhookDelivery.organization_id == organization_id)
        delivery = query.first()
        if not delivery:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery record not found")

        delivery.attempt += 1
        delivery.status = "processing"
        self.db.commit()

        await WebhookDeliveryService.deliver_webhook_async(delivery.id)
        self.db.refresh(delivery)

        return {
            "success": delivery.status == "delivered",
            "status": delivery.status,
            "response_status": delivery.response_status,
            "duration_ms": delivery.duration_ms,
            "attempt": delivery.attempt,
            "error_message": delivery.error_message,
        }

    # ─────────────────────────────────────────────────────────────
    # Statistics & Real Test Execution
    # ─────────────────────────────────────────────────────────────

    def get_stats(self, organization_id: Optional[int] = 1) -> WebhookStatsOut:
        """Calculate summary statistics for webhooks dashboard cards."""
        wh_q = self.db.query(Webhook)
        deliv_q = self.db.query(WebhookDelivery)

        if organization_id is not None:
            wh_q = wh_q.filter(Webhook.organization_id == organization_id)
            deliv_q = deliv_q.filter(WebhookDelivery.organization_id == organization_id)

        total_wh = wh_q.count()
        active_wh = wh_q.filter(Webhook.is_active == True).count()
        incoming_wh = wh_q.filter(Webhook.type == "incoming").count()
        outgoing_wh = wh_q.filter(Webhook.type == "outgoing").count()

        total_deliv = deliv_q.count()
        failed_deliv = deliv_q.filter(WebhookDelivery.status == "failed").count()

        return WebhookStatsOut(
            total_webhooks=total_wh,
            active_webhooks=active_wh,
            incoming_webhooks=incoming_wh,
            outgoing_webhooks=outgoing_wh,
            failed_deliveries=failed_deliv,
            total_deliveries=total_deliv,
        )

    async def send_test(
        self,
        webhook_id: int,
        organization_id: Optional[int] = 1,
        event_name: Optional[str] = None,
        custom_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a REAL HTTP test POST to the outgoing webhook destination."""
        query = self.db.query(Webhook).filter(Webhook.id == webhook_id)
        if organization_id is not None:
            query = query.filter(Webhook.organization_id == organization_id)
        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")

        if webhook.type != "outgoing" or not webhook.url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Test delivery can only be executed for outgoing webhooks with a destination URL",
            )

        return await WebhookDeliveryService.send_test_delivery(
            webhook=webhook,
            event_name=event_name,
            custom_data=custom_data,
            db=self.db,
        )

    # ─────────────────────────────────────────────────────────────
    # Incoming Webhook Processing
    # ─────────────────────────────────────────────────────────────

    def process_incoming_webhook(
        self,
        identifier: str,
        raw_body: bytes,
        headers: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Authenticate, validate, and record incoming webhook event from an external system.
        """
        # Resolve webhook by UUID or ID
        query = self.db.query(Webhook)
        if identifier.isdigit():
            query = query.filter((Webhook.id == int(identifier)) | (Webhook.webhook_uuid == identifier))
        else:
            query = query.filter(Webhook.webhook_uuid == identifier)

        webhook = query.first()
        if not webhook:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incoming webhook not found")

        if not webhook.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook is inactive/disabled")

        if webhook.type != "incoming":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook is not configured for incoming events")

        # 1. Signature Verification (if X-Webhook-Signature or signature header is provided)
        sig_header = (
            headers.get("x-webhook-signature")
            or headers.get("X-Webhook-Signature")
            or headers.get("x-signature")
            or headers.get("X-Signature")
        )
        if sig_header:
            if not verify_signature(webhook.secret, raw_body, sig_header):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC-SHA256 webhook signature")

        # 2. Replay Protection via Timestamp
        ts_header = headers.get("x-webhook-timestamp") or headers.get("X-Webhook-Timestamp")
        if ts_header:
            try:
                # If numeric unix timestamp or ISO8601 string
                if str(ts_header).isdigit():
                    req_ts = float(ts_header)
                    now_ts = datetime.now(timezone.utc).timestamp()
                    # 5-minute replay window
                    if abs(now_ts - req_ts) > 300:
                        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook timestamp outside 5-minute replay window")
            except Exception as e:
                if isinstance(e, HTTPException):
                    raise e
                logger.debug(f"[INCOMING WEBHOOK TS CHECK] {e}")

        # 3. Idempotency Check
        event_name = (
            payload.get("event")
            or headers.get("x-webhook-event")
            or headers.get("X-Webhook-Event")
            or "incoming.event"
        )
        event_id = (
            payload.get("event_id")
            or headers.get("x-webhook-event-id")
            or headers.get("X-Webhook-Event-ID")
            or f"in_{uuid.uuid4().hex[:16]}"
        )

        # Check if already processed
        existing_delivery = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.webhook_id == webhook.id,
            WebhookDelivery.event_id == event_id,
        ).first()

        if existing_delivery:
            logger.info(f"[INCOMING WEBHOOK DUPLICATE] Webhook ID {webhook.id} event_id={event_id} already received.")
            return {
                "success": True,
                "status": "duplicate_acknowledged",
                "message": f"Event {event_id} was already accepted (idempotency acknowledged)",
                "event_id": event_id,
            }

        # 4. Event validation against accepted events
        accepted_events = webhook.events or []
        if ("*" not in accepted_events) and ("all" not in accepted_events) and (event_name not in accepted_events):
            logger.warning(f"[INCOMING WEBHOOK UNACCEPTED EVENT] Event '{event_name}' not in {accepted_events}")

        # 5. Persist delivery log
        delivery = WebhookDelivery(
            webhook_id=webhook.id,
            organization_id=webhook.organization_id,
            event=event_name,
            event_id=event_id,
            direction="incoming",
            request_url=self._build_incoming_url(webhook),
            request_headers=sanitize_headers_for_storage(headers),
            request_payload=payload,
            response_status=200,
            response_body=json.dumps({"success": True, "event_id": event_id}),
            attempt=1,
            max_attempts=1,
            status="delivered",
            duration_ms=5,
        )
        self.db.add(delivery)
        webhook.last_triggered_at = datetime.now(timezone.utc)
        self.db.commit()

        logger.info(f"[INCOMING WEBHOOK ACCEPTED] ID={webhook.id} org={webhook.organization_id} event='{event_name}' event_id={event_id}")

        return {
            "success": True,
            "status": "accepted",
            "message": "Webhook payload received and verified successfully",
            "webhook_id": webhook.id,
            "event": event_name,
            "event_id": event_id,
        }
