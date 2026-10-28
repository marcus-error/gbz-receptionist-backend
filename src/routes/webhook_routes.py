from fastapi import APIRouter

from src.controllers import webhook_controller
from src.validators.webhook_validator import (
    WebhookDeliveryOut,
    WebhookOut,
    WebhookStatsOut,
    WebhookWithSecretOut,
)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# ── General Webhook Utilities & Stats ──────────────────────────────────
router.add_api_route("/events", webhook_controller.get_webhook_events, methods=["GET"])
router.add_api_route("/stats", webhook_controller.get_webhook_stats, methods=["GET"], response_model=WebhookStatsOut)

# ── Global Deliveries Endpoints ────────────────────────────────────────
router.add_api_route("/deliveries", webhook_controller.list_deliveries, methods=["GET"])
router.add_api_route("/deliveries/{delivery_id}", webhook_controller.get_delivery, methods=["GET"], response_model=WebhookDeliveryOut)
router.add_api_route("/deliveries/{delivery_id}/retry", webhook_controller.retry_delivery, methods=["POST"])

# ── Public Incoming Webhook Endpoint ──────────────────────────────────
router.add_api_route("/incoming/{identifier}", webhook_controller.handle_incoming_webhook, methods=["POST"])

# ── Webhook CRUD Endpoints ────────────────────────────────────────────
router.add_api_route("", webhook_controller.list_webhooks, methods=["GET"])
router.add_api_route("", webhook_controller.create_webhook, methods=["POST"], response_model=WebhookWithSecretOut, status_code=201)

router.add_api_route("/{webhook_id}", webhook_controller.get_webhook, methods=["GET"], response_model=WebhookOut)
router.add_api_route("/{webhook_id}", webhook_controller.update_webhook, methods=["PUT", "PATCH"], response_model=WebhookOut)
router.add_api_route("/{webhook_id}", webhook_controller.delete_webhook, methods=["DELETE"])

# ── Webhook Actions & Per-Webhook Deliveries ───────────────────────────
router.add_api_route("/{webhook_id}/toggle", webhook_controller.toggle_webhook, methods=["POST"], response_model=WebhookOut)
router.add_api_route("/{webhook_id}/enable", webhook_controller.enable_webhook, methods=["POST"], response_model=WebhookOut)
router.add_api_route("/{webhook_id}/disable", webhook_controller.disable_webhook, methods=["POST"], response_model=WebhookOut)
router.add_api_route("/{webhook_id}/rotate-secret", webhook_controller.rotate_secret, methods=["POST"], response_model=WebhookWithSecretOut)
router.add_api_route("/{webhook_id}/test", webhook_controller.send_test, methods=["POST"])
router.add_api_route("/{webhook_id}/deliveries", webhook_controller.list_deliveries, methods=["GET"])
router.add_api_route("/{webhook_id}/deliveries/{delivery_id}", webhook_controller.get_delivery, methods=["GET"], response_model=WebhookDeliveryOut)
router.add_api_route("/{webhook_id}/deliveries/{delivery_id}/retry", webhook_controller.retry_delivery, methods=["POST"])
