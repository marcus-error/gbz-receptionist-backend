import hmac
import hashlib
import json
import time
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.app import fastapi_app
from src.config.database import Base, get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.models.organization import Organization
from src.models.webhook import Webhook, WebhookDelivery
from src.services.webhook_delivery_service import compute_signature, verify_signature
from src.services.webhook_dispatcher import dispatch_event

# Create SQLite database for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_webhooks.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base.metadata.create_all(bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


current_test_user = User(id=1, name="Admin User", email="admin@example.com", password_hash="hash", role="admin", organization_id=1)


def override_get_current_user():
    return current_test_user


fastapi_app.dependency_overrides[get_db] = override_get_db
fastapi_app.dependency_overrides[get_current_user] = override_get_current_user
client = TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    db.query(WebhookDelivery).delete()
    db.query(Webhook).delete()
    db.query(User).delete()
    db.query(Organization).delete()
    org1 = Organization(id=1, name="Org 1")
    org2 = Organization(id=2, name="Org 2")
    db.add(org1)
    db.add(org2)
    user1 = User(id=1, name="Admin User", email="admin@example.com", password_hash="hash", role="admin", organization_id=1)
    user2 = User(id=2, name="Org2 User", email="org2@example.com", password_hash="hash", role="admin", organization_id=2)
    db.add(user1)
    db.add(user2)
    db.commit()
    db.close()


def test_get_webhook_events():
    resp = client.get("/api/v1/webhooks/events")
    assert resp.status_code == 200
    events = resp.json()
    assert isinstance(events, list)
    assert any(e["event"] == "appointment.created" for e in events)
    assert any(e["event"] == "call.completed" for e in events)


def test_create_and_list_outgoing_webhook():
    payload = {
        "name": "CRM Sync Webhook",
        "type": "outgoing",
        "url": "https://httpbin.org/post",
        "events": ["appointment.created", "appointment.updated"],
        "is_active": True,
        "description": "Sync appointments to external CRM"
    }
    create_resp = client.post("/api/v1/webhooks", json=payload)
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["name"] == "CRM Sync Webhook"
    assert created["type"] == "outgoing"
    assert created["secret"].startswith("whsec_")
    wh_id = created["id"]

    # List webhooks
    list_resp = client.get("/api/v1/webhooks")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["total"] == 1
    wh_item = data["items"][0]
    assert wh_item["id"] == wh_id
    assert "••••" in wh_item["secret_masked"]


def test_create_incoming_webhook():
    payload = {
        "name": "Zapier Inbound Webhook",
        "type": "incoming",
        "events": ["lead.created"],
        "is_active": True
    }
    create_resp = client.post("/api/v1/webhooks", json=payload)
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["type"] == "incoming"
    assert created["incoming_url"] is not None
    assert "/api/v1/webhooks/incoming/" in created["incoming_url"]
    wh_uuid = created["webhook_uuid"]
    secret = created["secret"]

    # Test sending valid incoming payload to the endpoint
    body = {"event": "lead.created", "event_id": "zap_12345", "name": "Alice Smith", "phone": "+15554443333"}
    raw_body = json.dumps(body)
    sig = hmac.new(secret.encode(), raw_body.encode(), hashlib.sha256).hexdigest()

    in_resp = client.post(
        f"/api/v1/webhooks/incoming/{wh_uuid}",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={sig}",
            "X-Webhook-Event": "lead.created",
            "X-Webhook-Event-ID": "zap_12345"
        }
    )
    assert in_resp.status_code == 200
    res_json = in_resp.json()
    assert res_json["success"] is True
    assert res_json["status"] == "accepted"

    # Test idempotency on duplicate event_id
    dup_resp = client.post(
        f"/api/v1/webhooks/incoming/{wh_uuid}",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={sig}",
            "X-Webhook-Event": "lead.created",
            "X-Webhook-Event-ID": "zap_12345"
        }
    )
    assert dup_resp.status_code == 200
    assert dup_resp.json()["status"] == "duplicate_acknowledged"


def test_incoming_webhook_invalid_signature():
    payload = {
        "name": "Secure Inbound",
        "type": "incoming",
        "events": ["payment.success"],
        "is_active": True
    }
    create_resp = client.post("/api/v1/webhooks", json=payload)
    created = create_resp.json()
    wh_uuid = created["webhook_uuid"]

    body = {"event": "payment.success", "amount": 100}
    raw_body = json.dumps(body)

    # Invalid signature should be rejected with 401
    bad_resp = client.post(
        f"/api/v1/webhooks/incoming/{wh_uuid}",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": "sha256=invalid_signature_hex_123456"
        }
    )
    assert bad_resp.status_code == 401


def test_toggle_and_rotate_secret():
    payload = {
        "name": "Test Rotation",
        "type": "outgoing",
        "url": "https://example.com/wh",
        "events": ["appointment.created"]
    }
    create_resp = client.post("/api/v1/webhooks", json=payload)
    created = create_resp.json()
    wh_id = created["id"]
    old_secret = created["secret"]

    # Toggle
    toggle_resp = client.post(f"/api/v1/webhooks/{wh_id}/toggle")
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["is_active"] is False

    # Rotate secret
    rot_resp = client.post(f"/api/v1/webhooks/{wh_id}/rotate-secret")
    assert rot_resp.status_code == 200
    new_secret = rot_resp.json()["secret"]
    assert new_secret != old_secret
    assert new_secret.startswith("whsec_")


def test_webhook_stats():
    client.post("/api/v1/webhooks", json={"name": "Out1", "type": "outgoing", "url": "https://example.com", "events": ["*"]})
    client.post("/api/v1/webhooks", json={"name": "In1", "type": "incoming", "events": ["*"]})

    stats_resp = client.get("/api/v1/webhooks/stats")
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["total_webhooks"] == 2
    assert stats["outgoing_webhooks"] == 1
    assert stats["incoming_webhooks"] == 1
    assert stats["active_webhooks"] == 2


def test_multi_tenant_isolation():
    global current_test_user
    # 1. User from Org 1 creates webhook
    current_test_user = User(id=1, name="Admin Org1", email="org1@test.com", password_hash="h", role="admin", organization_id=1)
    res1 = client.post("/api/v1/webhooks", json={"name": "Org1 Webhook", "type": "outgoing", "url": "https://example.com", "events": ["*"]})
    assert res1.status_code == 201
    wh1_id = res1.json()["id"]

    # 2. Switch context to Org 2
    current_test_user = User(id=2, name="Admin Org2", email="org2@test.com", password_hash="h", role="admin", organization_id=2)

    # Org 2 list should be empty
    list_res = client.get("/api/v1/webhooks")
    assert list_res.status_code == 200
    assert list_res.json()["total"] == 0

    # Org 2 cannot read Org 1 webhook
    get_res = client.get(f"/api/v1/webhooks/{wh1_id}")
    assert get_res.status_code == 404

    # Org 2 cannot delete Org 1 webhook
    del_res = client.delete(f"/api/v1/webhooks/{wh1_id}")
    assert del_res.status_code == 404

    # Switch back to Org 1
    current_test_user = User(id=1, name="Admin Org1", email="org1@test.com", password_hash="h", role="admin", organization_id=1)


def test_send_test_webhook():
    payload = {
        "name": "Test Outgoing",
        "type": "outgoing",
        "url": "https://httpbin.org/post",
        "events": ["appointment.created"]
    }
    create_resp = client.post("/api/v1/webhooks", json=payload)
    wh_id = create_resp.json()["id"]

    # Send test
    test_resp = client.post(f"/api/v1/webhooks/{wh_id}/test", json={"event_name": "appointment.created", "payload": {"test": True}})
    assert test_resp.status_code == 200
    test_data = test_resp.json()
    assert "success" in test_data
    assert "duration_ms" in test_data


def test_event_dispatcher():
    db = TestingSessionLocal()
    # Create outgoing webhook subscribed to appointment.created
    wh = Webhook(
        organization_id=1,
        name="Dispatcher Test",
        type="outgoing",
        url="https://httpbin.org/post",
        secret="whsec_dispatcher_test",
        events=["appointment.created"],
        is_active=True
    )
    db.add(wh)
    db.commit()
    db.close()

    # Dispatch event
    delivery_ids = dispatch_event("appointment.created", 1, {"customer": "John Doe", "doctor": "Dr. Smith"}, db=db)
    assert len(delivery_ids) >= 1

    # Check delivery log created in database
    deliv = db.query(WebhookDelivery).filter(WebhookDelivery.event_id == delivery_ids[0]).first()
    assert deliv is not None
    assert deliv.event == "appointment.created"
    assert deliv.direction == "outgoing"
    assert "John Doe" in str(deliv.request_payload)
    db.close()
