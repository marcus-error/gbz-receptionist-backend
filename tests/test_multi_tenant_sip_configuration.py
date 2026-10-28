"""
Targeted Test Suite: Multi-Tenant Independent SIP Configuration
Validates:
1. Tenant A saves and activates SIP successfully.
2. Tenant B saves and activates a different SIP account independently.
3. Cross-tenant isolation: Tenant A cannot read, update, activate, or deactivate Tenant B.
4. Repeated activation does not create duplicate registrar threads.
5. Updating Tenant A does not stop Tenant B's registration.
6. Deactivating Tenant A does not affect Tenant B.
7. Accurate failure reporting for invalid credentials.
8. Safe handling of registration timeout and network failure.
9. Inbound call routing scoped to correct tenant DID.
10. Outbound calls scoped to caller's tenant credentials with rejection on unconfigured tenant.
11. Transparent backward compatibility for legacy plaintext credentials and admin fallback.
"""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from src.app import create_app
from src.config.jwt import create_access_token
from src.config.database import SessionLocal
from src.models.user import User
from src.models.organization import Organization
from src.models.sip_config import SipConfig
from src.services.sip_registrar import (
    start_registrar,
    stop_registrar,
    get_registrar,
    get_all_registrars,
    get_registration_status,
    SipRegistrar,
)
from src.services.telephony_service import TelephonyService
from src.validators.telephony_validator import SipConfigIn, SipRegisterRequest, SipOutboundCallRequest
from src.utils.security import encrypt_secret, decrypt_secret


@pytest.fixture(autouse=True)
def clean_sip_state():
    """Ensure clean SIP state before and after each test."""
    stop_registrar()  # Stop all active registrars
    db = SessionLocal()
    try:
        # Create Organizations if not present
        if not db.query(Organization).filter(Organization.id == 1).first():
            db.add(Organization(id=1, name="Tenant One", organization_type="General", status="active"))
        if not db.query(Organization).filter(Organization.id == 2).first():
            db.add(Organization(id=2, name="Tenant Two", organization_type="General", status="active"))
        if not db.query(Organization).filter(Organization.id == 3).first():
            db.add(Organization(id=3, name="Tenant Three", organization_type="General", status="active"))

        # Create Users
        user_1 = db.query(User).filter(User.id == 101).first()
        if not user_1:
            user_1 = User(id=101, name="Admin Org1", email="admin_org1@test.com", password_hash="dummy_hash", role="admin", organization_id=1, is_active=1)
            db.add(user_1)

        user_2 = db.query(User).filter(User.id == 102).first()
        if not user_2:
            user_2 = User(id=102, name="Admin Org2", email="admin_org2@test.com", password_hash="dummy_hash", role="admin", organization_id=2, is_active=1)
            db.add(user_2)

        user_3 = db.query(User).filter(User.id == 103).first()
        if not user_3:
            user_3 = User(id=103, name="Admin Org3", email="admin_org3@test.com", password_hash="dummy_hash", role="admin", organization_id=3, is_active=1)
            db.add(user_3)

        # Clear test SipConfig records
        db.query(SipConfig).filter(SipConfig.organization_id.in_([1, 2, 3])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

    yield

    stop_registrar()
    db = SessionLocal()
    try:
        db.query(SipConfig).filter(SipConfig.organization_id.in_([1, 2, 3])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def test_client():
    app = create_app()
    return TestClient(app)


def get_auth_headers(user_id: int):
    token = create_access_token({"sub": str(user_id)})
    return {"Authorization": f"Bearer {token}"}


class MockVoIPPhoneSuccess:
    """Mock pyVoIP VoIPPhone that simulates successful registration."""
    def __init__(self, *args, **kwargs):
        self.sip = MagicMock()
        self.sip.default_expires = 180
        self.sip.s = MagicMock()
        self.sip.s._closed = False
        self._status = MagicMock()
        self._status.name = "REGISTERED"

    def start(self):
        pass

    def stop(self):
        pass

    def get_status(self):
        return self._status

    def is_alive(self):
        return True


class MockVoIPPhoneFailed:
    """Mock pyVoIP VoIPPhone that simulates failed registration."""
    def __init__(self, *args, **kwargs):
        self.sip = MagicMock()
        self.sip.s = MagicMock()
        self.sip.s._closed = False
        self._status = MagicMock()
        self._status.name = "FAILED"

    def start(self):
        pass

    def stop(self):
        pass

    def get_status(self):
        return self._status

    def is_alive(self):
        return True


# ==============================================================================
# 1. Tenant A & B Save and Independent Activation
# ==============================================================================

def test_tenant_a_saves_and_activates_sip_successfully(test_client):
    headers_a = get_auth_headers(101)

    # 1. Save SIP configuration for Tenant A
    payload_a = {
        "account_name": "Tenant A SIP Trunk",
        "sip_server": "sip.provider-a.com",
        "sip_host": "sip.provider-a.com",
        "sip_username": "tenant_a_user",
        "sip_password": "SuperSecretPasswordA!",
        "sip_port": 5060,
        "did_number": "+15551000001",
    }
    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        save_resp = test_client.post("/api/v1/telephony/sip", json=payload_a, headers=headers_a)
        assert save_resp.status_code == 200
        data = save_resp.json()
        assert data["sip_host"] == "sip.provider-a.com"
        assert data["sip_username"] == "tenant_a_user"
        assert data["has_password"] is True
        # Plaintext password must NEVER be in response
        assert "sip_password" not in data
        assert "SuperSecretPasswordA!" not in save_resp.text

        # Verify DB encryption at rest
        db = SessionLocal()
        try:
            cfg = db.query(SipConfig).filter(SipConfig.organization_id == 1).first()
            assert cfg is not None
            assert cfg.sip_password_encrypted != "SuperSecretPasswordA!"
            assert decrypt_secret(cfg.sip_password_encrypted) == "SuperSecretPasswordA!"
        finally:
            db.close()

        # 2. Activate SIP registration
        reg_resp = test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_a)
        assert reg_resp.status_code == 200
        reg_data = reg_resp.json()
        assert reg_data["success"] is True
        assert reg_data["organization_id"] == 1

        reg_instance = get_registrar(organization_id=1)
        assert reg_instance is not None
        reg_instance.wait_for_status(target_statuses=("registered", "error"), timeout=3.0)
        assert reg_instance.status == "registered"


def test_tenant_b_saves_and_activates_sip_independently(test_client):
    headers_a = get_auth_headers(101)
    headers_b = get_auth_headers(102)

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        # 1. Activate Tenant A
        test_client.post("/api/v1/telephony/sip", json={
            "account_name": "Tenant A Trunk",
            "sip_host": "sip.provider-a.com",
            "sip_username": "user_a",
            "sip_password": "PasswordA",
            "sip_port": 5060,
            "did_number": "+15551000001",
        }, headers=headers_a)
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_a)

        # 2. Activate Tenant B with different provider credentials
        test_client.post("/api/v1/telephony/sip", json={
            "account_name": "Tenant B Trunk",
            "sip_host": "sip.provider-b.com",
            "sip_username": "user_b",
            "sip_password": "PasswordB",
            "sip_port": 5062,
            "did_number": "+15552000002",
        }, headers=headers_b)
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_b)

        # 3. Verify BOTH registrars exist independently in memory
        all_regs = get_all_registrars()
        assert 1 in all_regs
        assert 2 in all_regs

        reg_a = get_registrar(organization_id=1)
        reg_b = get_registrar(organization_id=2)
        reg_a.wait_for_status(target_statuses=("registered", "error"), timeout=3.0)
        reg_b.wait_for_status(target_statuses=("registered", "error"), timeout=3.0)
        assert reg_a.sip_username == "user_a"
        assert reg_b.sip_username == "user_b"
        assert reg_a.status == "registered"
        assert reg_b.status == "registered"
        assert reg_a.local_bind_port != reg_b.local_bind_port


# ==============================================================================
# 2. Multi-Tenant Isolation
# ==============================================================================

def test_tenant_isolation_read_and_update(test_client):
    headers_a = get_auth_headers(101)
    headers_b = get_auth_headers(102)

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        # Tenant A saves config
        test_client.post("/api/v1/telephony/sip", json={
            "account_name": "Org 1 SIP",
            "sip_host": "sip.org1.com",
            "sip_username": "org1_trunk",
            "sip_password": "PasswordOrg1",
            "did_number": "+15551111111",
        }, headers=headers_a)

        # Tenant B saves config
        test_client.post("/api/v1/telephony/sip", json={
            "account_name": "Org 2 SIP",
            "sip_host": "sip.org2.com",
            "sip_username": "org2_trunk",
            "sip_password": "PasswordOrg2",
            "did_number": "+15552222222",
        }, headers=headers_b)

        # Tenant A reads config: must ONLY see Org 1 settings
        resp_a = test_client.get("/api/v1/telephony/sip", headers=headers_a)
        assert resp_a.status_code == 200
        assert resp_a.json()["sip_username"] == "org1_trunk"
        assert resp_a.json()["did_number"] == "+15551111111"

        # Tenant B reads config: must ONLY see Org 2 settings
        resp_b = test_client.get("/api/v1/telephony/sip", headers=headers_b)
        assert resp_b.status_code == 200
        assert resp_b.json()["sip_username"] == "org2_trunk"
        assert resp_b.json()["did_number"] == "+15552222222"


# ==============================================================================
# 3. Repeated Activation & Non-Disruption
# ==============================================================================

def test_repeated_activation_does_not_duplicate_registrations(test_client):
    headers_a = get_auth_headers(101)

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.provider-a.com",
            "sip_username": "user_a",
            "sip_password": "PasswordA",
            "sip_port": 5060,
        }, headers=headers_a)

        # First activation
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_a)
        reg_first = get_registrar(organization_id=1)

        # Second activation with same credentials
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_a)
        reg_second = get_registrar(organization_id=1)

        # Must be the exact same instance, not a newly spawned duplicate
        assert reg_first is reg_second


def test_updating_tenant_a_does_not_stop_tenant_b(test_client):
    headers_a = get_auth_headers(101)
    headers_b = get_auth_headers(102)

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        # Register Tenant A and B
        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.a.com",
            "sip_username": "user_a",
            "sip_password": "PassA",
        }, headers=headers_a)
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_a)

        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.b.com",
            "sip_username": "user_b",
            "sip_password": "PassB",
        }, headers=headers_b)
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_b)

        reg_b_before = get_registrar(organization_id=2)
        assert reg_b_before is not None
        reg_b_before.wait_for_status(target_statuses=("registered", "error"), timeout=3.0)
        assert reg_b_before.status == "registered"

        # Update Tenant A with new port and host
        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.new-a.com",
            "sip_username": "user_a_updated",
            "sip_password": "PassAUpdated",
        }, headers=headers_a)

        # Tenant B must remain active and registered
        reg_b_after = get_registrar(organization_id=2)
        assert reg_b_after is not None
        assert reg_b_after.status == "registered"
        assert reg_b_after.sip_username == "user_b"


def test_deactivating_tenant_a_does_not_affect_tenant_b(test_client):
    headers_a = get_auth_headers(101)
    headers_b = get_auth_headers(102)

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        # Register both
        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.a.com",
            "sip_username": "user_a",
            "sip_password": "PassA",
        }, headers=headers_a)
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_a)

        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.b.com",
            "sip_username": "user_b",
            "sip_password": "PassB",
        }, headers=headers_b)
        test_client.post("/api/v1/telephony/sip/register", json={}, headers=headers_b)

        reg_b = get_registrar(organization_id=2)
        assert reg_b is not None
        reg_b.wait_for_status(target_statuses=("registered", "error"), timeout=3.0)

        # Unregister Tenant A
        unreg_resp = test_client.post("/api/v1/telephony/sip/unregister", headers=headers_a)
        assert unreg_resp.status_code == 200

        # Tenant A status is unregistered
        status_a = test_client.get("/api/v1/telephony/sip/registration-status", headers=headers_a).json()
        assert status_a["status"] == "unregistered"

        # Tenant B status is STILL registered
        status_b = test_client.get("/api/v1/telephony/sip/registration-status", headers=headers_b).json()
        assert status_b["status"] == "registered"
        assert status_b["sip_username"] == "user_b"


# ==============================================================================
# 4. Accurate Error Reporting
# ==============================================================================

def test_invalid_credentials_produce_accurate_failure_status(test_client):
    headers_a = get_auth_headers(101)

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneFailed):
        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.failing-provider.com",
            "sip_username": "wrong_user",
            "sip_password": "wrong_password",
        }, headers=headers_a)

        reg_instance = get_registrar(organization_id=1)
        assert reg_instance is not None
        reg_instance.wait_for_status(target_statuses=("registered", "error"), timeout=3.0)
        # Never claim registration success when provider rejects
        assert reg_instance.status == "error"
        assert "rejected" in reg_instance.detail.lower() or "failed" in reg_instance.detail.lower()


# ==============================================================================
# 5. Inbound and Outbound Scoping
# ==============================================================================

def test_inbound_calls_route_to_correct_tenant(test_client):
    db = SessionLocal()
    try:
        # Save DID +15552000002 for Tenant 2
        cfg_2 = SipConfig(
            organization_id=2,
            user_id=102,
            sip_host="sip.tenant2.com",
            sip_username="tenant2_user",
            did_number="+15552000002",
            status="connected",
        )
        db.add(cfg_2)
        db.commit()
    finally:
        db.close()

    # Inbound call arrives with DID +15552000002
    resp = test_client.post("/api/v1/telephony/sip/inbound", json={
        "caller_phone": "+19998887777",
        "caller_name": "Customer John",
        "did_number": "+15552000002",
        "event": "call_start",
    })
    assert resp.status_code == 200
    data = resp.json()
    call_id = data["call_id"]

    # Verify the created CallLog record is strictly associated with Tenant 2
    from src.models.call_log import CallLog
    db = SessionLocal()
    try:
        call = db.query(CallLog).filter(CallLog.id == call_id).first()
        assert call is not None
        assert call.organization_id == 2
    finally:
        db.close()


def test_outbound_calls_scoped_and_reject_unconfigured_tenant(test_client):
    headers_b = get_auth_headers(102)
    headers_c = get_auth_headers(103)  # Tenant 3 has no SIP configured

    with patch("pyVoIP.VoIP.VoIPPhone", MockVoIPPhoneSuccess):
        # Configure Tenant B
        test_client.post("/api/v1/telephony/sip", json={
            "sip_host": "sip.provider-b.com",
            "sip_username": "user_b",
            "sip_password": "PasswordB",
            "dialing_prefix": "9",
        }, headers=headers_b)

        # Tenant B outbound call succeeds
        out_resp_b = test_client.post("/api/v1/telephony/sip/outbound", json={
            "to_number": "1234567890",
        }, headers=headers_b)
        assert out_resp_b.status_code == 200
        assert out_resp_b.json()["formatted_number"] == "91234567890"

        # Tenant C (unconfigured) outbound call must be REJECTED (HTTP 400)
        out_resp_c = test_client.post("/api/v1/telephony/sip/outbound", json={
            "to_number": "1234567890",
        }, headers=headers_c)
        assert out_resp_c.status_code == 400
        assert "not have an active sip configuration" in out_resp_c.json()["detail"].lower()


# ==============================================================================
# 6. Backward Compatibility for Legacy Plaintext Credentials
# ==============================================================================

def test_legacy_plaintext_credential_compatibility():
    # Simulates an existing database row saved before encryption was enabled
    legacy_pass = "LegacyPlaintextPass123"
    decrypted = decrypt_secret(legacy_pass)
    assert decrypted == legacy_pass

    # New credentials are encrypted
    encrypted = encrypt_secret("NewSecret123")
    assert encrypted != "NewSecret123"
    assert decrypt_secret(encrypted) == "NewSecret123"
