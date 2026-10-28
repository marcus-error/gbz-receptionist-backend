"""
Webhook Delivery Service.
Handles HTTP POST delivery, HMAC-SHA256 signature generation, exponential backoff retries,
and delivery state persistence.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from src.config.database import SessionLocal
from src.models.webhook import Webhook, WebhookDelivery, WebhookDeliveryStatus
from src.services.webhook_registry import get_event_sample

logger = logging.getLogger(__name__)

# Sensitive header names to sanitize before storing in database
SENSITIVE_HEADER_KEYS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
    "x-webhook-secret",
    "secret",
    "token",
}


def compute_signature(secret: str, raw_body: str) -> str:
    """Generate canonical HMAC-SHA256 hex signature from secret and raw JSON body."""
    if not secret:
        return ""
    key_bytes = secret.encode("utf-8")
    data_bytes = raw_body.encode("utf-8")
    return hmac.new(key_bytes, data_bytes, hashlib.sha256).hexdigest()


def verify_signature(secret: str, raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Verify incoming HMAC-SHA256 signature using constant-time comparison."""
    if not secret or not signature_header:
        return False
    
    clean_sig = signature_header.strip()
    if clean_sig.startswith("sha256="):
        clean_sig = clean_sig[7:]
    
    expected_sig = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig.lower(), clean_sig.lower())


def mask_secret(secret: Optional[str]) -> str:
    """Return safely masked representation of a secret for UI display and logs."""
    if not secret:
        return "••••••••••••••••"
    if len(secret) <= 8:
        return "••••••••"
    prefix = secret[:4]
    suffix = secret[-4:]
    return f"{prefix}••••••••{suffix}"


def sanitize_headers_for_storage(headers: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mask secret/auth headers before persisting to delivery logs."""
    if not headers:
        return {}
    sanitized = {}
    for k, v in headers.items():
        k_lower = str(k).lower()
        if any(sens in k_lower for sens in SENSITIVE_HEADER_KEYS):
            sanitized[k] = "••••••••"
        else:
            sanitized[k] = str(v)
    return sanitized


class WebhookDeliveryService:

    @staticmethod
    async def deliver_webhook_async(delivery_id: int):
        """Asynchronous worker to deliver a single webhook delivery record."""
        db: Session = SessionLocal()
        try:
            delivery = db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first()
            if not delivery:
                logger.warning(f"[WEBHOOK DELIVERY] Delivery ID {delivery_id} not found")
                return

            webhook = db.query(Webhook).filter(Webhook.id == delivery.webhook_id).first()
            if not webhook:
                delivery.status = "failed"
                delivery.error_message = "Associated webhook configuration was deleted."
                db.commit()
                return

            if not webhook.is_active:
                delivery.status = "failed"
                delivery.error_message = "Webhook is disabled/inactive."
                db.commit()
                return

            if not webhook.url:
                delivery.status = "failed"
                delivery.error_message = "Outgoing webhook has no destination URL configured."
                db.commit()
                return

            delivery.status = "processing"
            db.commit()

            # Canonical payload formatting
            payload_dict = delivery.request_payload or {}
            raw_body = json.dumps(payload_dict, separators=(",", ":"), ensure_ascii=False)
            iso_now = datetime.now(timezone.utc).isoformat()
            signature = compute_signature(webhook.secret, raw_body)

            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Callzenza-Webhook-Delivery/1.0",
                "X-Webhook-ID": str(webhook.webhook_uuid or webhook.id),
                "X-Webhook-Event": delivery.event,
                "X-Webhook-Timestamp": iso_now,
                "X-Webhook-Signature": f"sha256={signature}",
                "X-Webhook-Event-ID": delivery.event_id,
            }

            delivery.request_url = webhook.url
            delivery.request_headers = sanitize_headers_for_storage(headers)

            t0 = time.monotonic()
            resp_status = None
            resp_body_text = None
            resp_headers_dict = None
            err_msg = None

            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                    response = await client.post(
                        webhook.url,
                        content=raw_body.encode("utf-8"),
                        headers=headers
                    )
                    t1 = time.monotonic()
                    duration_ms = round((t1 - t0) * 1000)
                    resp_status = response.status_code
                    resp_headers_dict = dict(response.headers)
                    # Limit response body to first 16KB for safe DB storage
                    resp_body_text = response.text[:16384]

            except httpx.TimeoutException:
                t1 = time.monotonic()
                duration_ms = round((t1 - t0) * 1000)
                err_msg = "Delivery timed out after 15.0 seconds."
            except httpx.ConnectError as conn_err:
                t1 = time.monotonic()
                duration_ms = round((t1 - t0) * 1000)
                err_msg = f"Connection failed to {webhook.url}: {conn_err}"
            except Exception as exc:
                t1 = time.monotonic()
                duration_ms = round((t1 - t0) * 1000)
                err_msg = f"HTTP dispatch error: {exc}"

            delivery.duration_ms = duration_ms
            delivery.response_status = resp_status
            delivery.response_headers = sanitize_headers_for_storage(resp_headers_dict)
            delivery.response_body = resp_body_text
            delivery.error_message = err_msg

            if resp_status and 200 <= resp_status < 300:
                delivery.status = "delivered"
                delivery.error_message = None
                webhook.last_triggered_at = datetime.now(timezone.utc)
                logger.info(f"[WEBHOOK SUCCESS] ID={webhook.id} event={delivery.event} -> {webhook.url} ({resp_status} in {duration_ms}ms)")
            else:
                is_retryable = (resp_status is None) or (resp_status >= 500) or (resp_status == 429)
                if is_retryable and delivery.attempt < delivery.max_attempts:
                    delivery.status = "retrying"
                    logger.warning(f"[WEBHOOK RETRYING] ID={webhook.id} attempt {delivery.attempt}/{delivery.max_attempts} -> {webhook.url} (status={resp_status}, err={err_msg})")
                    # Schedule backoff retry in background
                    backoff_delay = min(300, (2 ** (delivery.attempt - 1)) * 5)  # 5s, 10s, 20s, 40s
                    asyncio.create_task(WebhookDeliveryService._schedule_retry(delivery.id, backoff_delay))
                else:
                    delivery.status = "failed"
                    if not err_msg:
                        delivery.error_message = f"HTTP response returned non-2xx status: {resp_status}"
                    logger.warning(f"[WEBHOOK FAILED] ID={webhook.id} attempt {delivery.attempt}/{delivery.max_attempts} -> {webhook.url} ({delivery.error_message})")

            db.commit()

        except Exception as e:
            logger.error(f"[WEBHOOK DELIVERY CRITICAL ERROR] {e}")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    @staticmethod
    async def _schedule_retry(delivery_id: int, delay_seconds: int):
        """Wait for backoff delay and increment attempt counter before redelivering."""
        await asyncio.sleep(delay_seconds)
        db: Session = SessionLocal()
        try:
            delivery = db.query(WebhookDelivery).filter(WebhookDelivery.id == delivery_id).first()
            if delivery and delivery.status == "retrying":
                delivery.attempt += 1
                db.commit()
                await WebhookDeliveryService.deliver_webhook_async(delivery.id)
        except Exception as exc:
            logger.error(f"[WEBHOOK RETRY ERROR] {exc}")
        finally:
            db.close()

    @staticmethod
    async def send_test_delivery(
        webhook: Webhook,
        event_name: Optional[str] = None,
        custom_data: Optional[Dict[str, Any]] = None,
        db: Optional[Session] = None
    ) -> Dict[str, Any]:
        """
        Perform a REAL test HTTP delivery to the destination URL.
        Returns detailed results including HTTP status, duration, request headers/payload, and response body.
        """
        import uuid
        event = event_name or (webhook.events[0] if webhook.events else "appointment.created")
        sample_data = custom_data or get_event_sample(event)
        event_id = f"test_{uuid.uuid4().hex[:16]}"
        iso_now = datetime.now(timezone.utc).isoformat()

        test_payload = {
            "event_id": event_id,
            "event": event,
            "webhook_id": str(webhook.webhook_uuid or webhook.id),
            "timestamp": iso_now,
            "organization_id": webhook.organization_id,
            "is_test": True,
            "data": sample_data
        }

        raw_body = json.dumps(test_payload, separators=(",", ":"), ensure_ascii=False)
        signature = compute_signature(webhook.secret, raw_body)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Callzenza-Webhook-Delivery/1.0 (Test Mode)",
            "X-Webhook-ID": str(webhook.webhook_uuid or webhook.id),
            "X-Webhook-Event": event,
            "X-Webhook-Timestamp": iso_now,
            "X-Webhook-Signature": f"sha256={signature}",
            "X-Webhook-Event-ID": event_id,
            "X-Webhook-Test": "true"
        }

        # Create delivery record if db session is provided
        delivery = None
        should_close_db = False
        if db is None:
            db = SessionLocal()
            should_close_db = True

        try:
            delivery = WebhookDelivery(
                webhook_id=webhook.id,
                organization_id=webhook.organization_id,
                event=event,
                event_id=event_id,
                direction="outgoing",
                request_url=webhook.url,
                request_headers=sanitize_headers_for_storage(headers),
                request_payload=test_payload,
                attempt=1,
                max_attempts=1,
                status="processing"
            )
            db.add(delivery)
            db.commit()
            db.refresh(delivery)

            t0 = time.monotonic()
            resp_status = None
            resp_body = None
            resp_headers = None
            err_msg = None
            success = False

            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    response = await client.post(
                        webhook.url,
                        content=raw_body.encode("utf-8"),
                        headers=headers
                    )
                    t1 = time.monotonic()
                    duration_ms = round((t1 - t0) * 1000)
                    resp_status = response.status_code
                    resp_headers = dict(response.headers)
                    resp_body = response.text[:8192]
                    success = (200 <= resp_status < 300)
            except httpx.TimeoutException:
                t1 = time.monotonic()
                duration_ms = round((t1 - t0) * 1000)
                err_msg = "Connection timed out after 10.0 seconds."
            except Exception as exc:
                t1 = time.monotonic()
                duration_ms = round((t1 - t0) * 1000)
                err_msg = f"HTTP POST error: {exc}"

            delivery.duration_ms = duration_ms
            delivery.response_status = resp_status
            delivery.response_headers = sanitize_headers_for_storage(resp_headers)
            delivery.response_body = resp_body
            delivery.error_message = err_msg
            delivery.status = "delivered" if success else "failed"
            if success:
                webhook.last_triggered_at = datetime.now(timezone.utc)
            db.commit()

            return {
                "success": success,
                "status_code": resp_status,
                "duration_ms": duration_ms,
                "request_headers": sanitize_headers_for_storage(headers),
                "request_payload": test_payload,
                "response_body": resp_body,
                "error_message": err_msg
            }
        finally:
            if should_close_db:
                db.close()
