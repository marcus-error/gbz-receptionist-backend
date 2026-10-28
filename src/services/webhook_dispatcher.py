"""
Application Webhook Event Dispatcher.
Reusable service to dispatch application events to all matching active outgoing webhooks.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.config.database import SessionLocal
from src.models.webhook import Webhook, WebhookDelivery
from src.services.webhook_delivery_service import WebhookDeliveryService

logger = logging.getLogger(__name__)


def dispatch_event(
    event_name: str,
    organization_id: int,
    data: Dict[str, Any],
    db: Optional[Session] = None
) -> List[str]:
    """
    Dispatch an application event to all active outgoing webhooks registered for this organization.
    Creates delivery records and spawns non-blocking async delivery tasks.
    Returns list of generated event IDs.
    """
    should_close_db = False
    if db is None:
        db = SessionLocal()
        should_close_db = True

    event_ids: List[str] = []
    try:
        # Query active outgoing webhooks for this organization
        webhooks = db.query(Webhook).filter(
            Webhook.organization_id == organization_id,
            Webhook.type == "outgoing",
            Webhook.is_active == True,
        ).all()

        if not webhooks:
            return []

        deliveries_to_start = []
        for wh in webhooks:
            # Check if webhook is subscribed to this event or wildcard
            sub_events = wh.events or []
            if isinstance(sub_events, str):
                import json
                try:
                    sub_events = json.loads(sub_events)
                except Exception:
                    sub_events = [sub_events]

            if ("*" in sub_events) or ("all" in sub_events) or (event_name in sub_events):
                event_id = f"evt_{uuid.uuid4().hex[:16]}"
                event_ids.append(event_id)
                iso_now = datetime.now(timezone.utc).isoformat()

                payload = {
                    "event_id": event_id,
                    "event": event_name,
                    "webhook_id": str(wh.webhook_uuid or wh.id),
                    "timestamp": iso_now,
                    "organization_id": organization_id,
                    "data": data or {}
                }

                delivery = WebhookDelivery(
                    webhook_id=wh.id,
                    organization_id=organization_id,
                    event=event_name,
                    event_id=event_id,
                    direction="outgoing",
                    request_url=wh.url,
                    request_payload=payload,
                    attempt=1,
                    max_attempts=5,
                    status="pending"
                )
                db.add(delivery)
                deliveries_to_start.append(delivery)

        if deliveries_to_start:
            db.commit()
            for d in deliveries_to_start:
                db.refresh(d)
                # Launch async delivery in background
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(WebhookDeliveryService.deliver_webhook_async(d.id))
                except RuntimeError:
                    # No running loop in synchronous thread - run in background thread
                    import threading
                    def _run_in_thread(deliv_id: int):
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(WebhookDeliveryService.deliver_webhook_async(deliv_id))
                        new_loop.close()
                    threading.Thread(target=_run_in_thread, args=(d.id,), daemon=True).start()

                logger.info(f"[WEBHOOK DISPATCH] Dispatched '{event_name}' (event_id={d.event_id}) to Webhook ID {d.webhook_id}")

    except Exception as exc:
        logger.error(f"[WEBHOOK DISPATCH ERROR] Failed to dispatch '{event_name}': {exc}")
    finally:
        if should_close_db:
            db.close()

    return event_ids
