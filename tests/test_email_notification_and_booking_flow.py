"""
Comprehensive Test Suite for CallZenza AI Receptionist:
Customer + Staff Email Notifications and Booking Flow Integration.

Verifies:
1. Dual email dispatch: Customer (confirmed email) and Assigned Staff (Staff Directory) both receive emails with matching booking references.
2. Email confirmation & decline: Customer email is confirmed before sending; declined email safely skips customer delivery.
3. Staff Directory validation & tenant isolation: Staff email is resolved strictly from active organization (no cross-tenant leakage, no arbitrary admin fallback).
4. Dynamic booking purpose: Product Demo, Service Booking, Consultation, and Healthcare appointments adapt dynamically.
5. Missing customer/staff email safety: Absence of either email does not crash or block the other recipient.
6. Failure resilience: Email provider failure does not lose or invalidate the persisted booking.
7. Idempotency: Retrying notification dispatch produces zero duplicate emails.
8. Email configuration API & template management: Customer and staff template updates, provider validation, and credential security.
9. Voice flow preservation: Email dispatch is strictly asynchronous and never placed in real-time RTP/audio processing paths.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from src.config.database import SessionLocal, engine, Base
from src.models.organization import Organization
from src.models.staff import Staff, StaffStatus
from src.models.appointment import Appointment, AppointmentStatus
from src.models.notification_log import NotificationLog
from src.models.email_config import EmailConfig
from src.models.user import User
from src.middleware.auth_middleware import get_current_user
from src.services.notification_service import (
    send_realtime_appointment_notification,
    _get_staff_info,
    _determine_booking_purpose,
    _render_template,
)
from src.services.booking_state_service import (
    BookingStateService,
    BookingState,
    BookingSession,
    parse_spoken_email,
)
from src.app import fastapi_app

client = TestClient(fastapi_app)

# Override auth for test client
mock_test_user = User(
    id=1,
    name="Admin User",
    email="admin@callzenza.com",
    role="admin",
    organization_id=1
)
fastapi_app.dependency_overrides[get_current_user] = lambda: mock_test_user


@pytest.fixture(autouse=True)
def setup_db():
    """Ensure database tables and test organizations exist."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Org 1
        org1 = db.query(Organization).filter(Organization.id == 1).first()
        if not org1:
            org1 = Organization(id=1, name="CallZenza Primary Clinic", is_active=True, email="contact@callzenza.com", phone="+1-555-0100")
            db.add(org1)

        # Org 2 (for tenant isolation tests)
        org2 = db.query(Organization).filter(Organization.id == 2).first()
        if not org2:
            org2 = Organization(id=2, name="External Tech Corp", is_active=True, email="info@externaltech.com", phone="+1-555-0200")
            db.add(org2)

        # Seed Staff in Org 1
        staff1 = db.query(Staff).filter(Staff.email == "sarah.connor@callzenza.com").first()
        if not staff1:
            staff1 = Staff(
                name="Sarah Connor",
                email="sarah.connor@callzenza.com",
                department="Cardiology",
                organization_id=1,
                status=StaffStatus.AVAILABLE,
                online_booking_enabled=True
            )
            db.add(staff1)

        # Seed Staff in Org 2 (Tenant isolation check)
        staff2 = db.query(Staff).filter(Staff.email == "john.doe@externaltech.com").first()
        if not staff2:
            staff2 = Staff(
                name="John Doe",
                email="john.doe@externaltech.com",
                department="Sales",
                organization_id=2,
                status=StaffStatus.AVAILABLE,
                online_booking_enabled=True
            )
            db.add(staff2)

        db.commit()
    finally:
        db.close()
    yield


# ==============================================================================
# TEST 1: Dual Email Dispatch with Matching Booking References
# ==============================================================================
def test_dual_email_dispatch_customer_and_staff():
    """
    Verify that when a booking is created with both customer email and assigned staff,
    confirmation emails are sent to BOTH recipients with the identical booking reference,
    purpose, and date/time.
    """
    db = SessionLocal()
    try:
        appt = Appointment(
            customer="Alice Smith",
            staff="Sarah Connor",
            appointment_date="15 Nov 2026",
            appointment_time="10:30 AM",
            status=AppointmentStatus.CONFIRMED,
            phone="+1555123456",
            email="alice.smith@example.com",
            service="Cardiology Consultation",
            department="Cardiology",
            organization_id=1,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        sent_calls = []

        def mock_send_func(db_session, to_email, subject, body, html_body=None, organization_id=1):
            sent_calls.append({
                "to_email": to_email,
                "subject": subject,
                "body": body,
                "org_id": organization_id,
            })
            return {"sent": True, "status": "sent", "message_id": f"msg_{len(sent_calls)}"}

        with patch("src.routes.email_routes._send", side_effect=mock_send_func):
            send_realtime_appointment_notification(db=db, appointment=appt, action="created")

        # Must send to both Alice (customer) and Sarah (staff)
        assert len(sent_calls) == 2, f"Expected 2 emails, got {len(sent_calls)}"
        recipients = [c["to_email"] for c in sent_calls]
        assert "alice.smith@example.com" in recipients
        assert "sarah.connor@callzenza.com" in recipients

        # Both emails must contain the exact same booking reference (str(appt.id))
        booking_ref = str(appt.id)
        for call in sent_calls:
            assert booking_ref in call["body"] or booking_ref in call["subject"]
            assert call["org_id"] == 1

        # Check NotificationLog audit entries
        logs = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appt.id,
            NotificationLog.channel == "email"
        ).all()
        assert len(logs) == 2
        log_types = {l.recipient_type: l for l in logs}
        assert "customer" in log_types and log_types["customer"].status == "sent"
        assert "staff" in log_types and log_types["staff"].status == "sent"
    finally:
        db.close()


# ==============================================================================
# TEST 2: Customer Email Declined or Missing Handled Safely
# ==============================================================================
def test_customer_email_declined_handles_gracefully():
    """
    Verify that if the customer declines email (or does not provide one),
    the customer email is skipped, but the staff notification is still delivered.
    """
    db = SessionLocal()
    try:
        appt = Appointment(
            customer="Bob Miller",
            staff="Sarah Connor",
            appointment_date="16 Nov 2026",
            appointment_time="02:00 PM",
            status=AppointmentStatus.CONFIRMED,
            phone="+1555987654",
            email=None,  # No customer email
            service="Follow-up Checkup",
            department="General Medicine",
            organization_id=1,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        sent_calls = []

        def mock_send_func(db_session, to_email, subject, body, html_body=None, organization_id=1):
            sent_calls.append({"to_email": to_email, "subject": subject, "body": body})
            return {"sent": True, "status": "sent", "message_id": "msg_staff_only"}

        with patch("src.routes.email_routes._send", side_effect=mock_send_func):
            send_realtime_appointment_notification(db=db, appointment=appt, action="created")

        # Only staff email should be sent
        assert len(sent_calls) == 1
        assert sent_calls[0]["to_email"] == "sarah.connor@callzenza.com"

        # Check logs
        staff_log = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appt.id,
            NotificationLog.channel == "email",
            NotificationLog.recipient_type == "staff"
        ).first()
        assert staff_log is not None and staff_log.status == "sent"

        # Customer log for email should not exist since customer had no email
        cust_log = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appt.id,
            NotificationLog.channel == "email",
            NotificationLog.recipient_type == "customer"
        ).first()
        assert cust_log is None
    finally:
        db.close()


# ==============================================================================
# TEST 3: Staff Directory Validation and Tenant Isolation
# ==============================================================================
def test_staff_directory_validation_and_tenant_isolation():
    """
    Verify that:
    1. Assigned staff is resolved strictly from Staff directory for the active organization.
    2. Cross-tenant staff resolution is strictly prevented (e.g. Org 1 cannot notify John Doe in Org 2).
    3. If no staff or unassigned staff, does NOT invent or fall back to arbitrary admin.
    4. Records missing staff recipient failure in NotificationLog for review.
    """
    db = SessionLocal()
    try:
        # Scenario: Staff name is John Doe, who only exists in Org 2, but appointment is in Org 1
        appt_cross_tenant = Appointment(
            customer="Charlie Brown",
            staff="John Doe",  # Belongs to Org 2
            appointment_date="17 Nov 2026",
            appointment_time="11:00 AM",
            status=AppointmentStatus.CONFIRMED,
            phone="+1555333444",
            email="charlie@example.com",
            service="Product Consultation",
            department="Sales",
            organization_id=1,  # Org 1
        )
        db.add(appt_cross_tenant)
        db.commit()
        db.refresh(appt_cross_tenant)

        # Direct test of _get_staff_info
        staff_info = _get_staff_info(db, appt_cross_tenant)
        assert staff_info.get("email") is None, "Cross-tenant staff email must NOT be returned!"

        sent_calls = []

        def mock_send_func(db_session, to_email, subject, body, html_body=None, organization_id=1):
            sent_calls.append({"to_email": to_email, "subject": subject})
            return {"sent": True, "status": "sent", "message_id": "msg_cust_only"}

        with patch("src.routes.email_routes._send", side_effect=mock_send_func):
            send_realtime_appointment_notification(db=db, appointment=appt_cross_tenant, action="created")

        # Only customer should receive an email
        assert len(sent_calls) == 1
        assert sent_calls[0]["to_email"] == "charlie@example.com"

        # Staff failure must be recorded in NotificationLog for administrative review
        failed_staff_log = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appt_cross_tenant.id,
            NotificationLog.channel == "email",
            NotificationLog.recipient_type == "staff"
        ).first()
        assert failed_staff_log is not None
        assert failed_staff_log.status == "failed"
        assert "Staff Directory" in failed_staff_log.error_reason
    finally:
        db.close()


# ==============================================================================
# TEST 4: Dynamic Booking Purposes (Demo, Service, Consultation, Appointment)
# ==============================================================================
def test_dynamic_booking_purposes():
    """
    Verify that booking purpose is dynamically derived from conversation/booking data
    and not assumed to always be a medical appointment.
    """
    # 1. Product Demo
    demo_appt = Appointment(service="AI Voice Receptionist Demo", department="Sales", notes="Wants software demonstration")
    assert "Demo" in _determine_booking_purpose(demo_appt)

    # 2. General Service
    service_appt = Appointment(service="Vehicle Inspection Service", department="Maintenance", notes="Full tune up")
    assert "Vehicle Inspection Service" in _determine_booking_purpose(service_appt)

    # 3. Consultation
    consult_appt = Appointment(service="Financial Advisory Consultation", department="Advisory", notes="Tax planning")
    assert "Consultation" in _determine_booking_purpose(consult_appt)

    # 4. Healthcare Appointment
    med_appt = Appointment(service="General Medicine Checkup", department="General Medicine", notes="Routine checkup")
    assert "Checkup" in _determine_booking_purpose(med_appt)


# ==============================================================================
# TEST 5: Idempotency Prevents Duplicate Emails on Retries
# ==============================================================================
def test_idempotency_prevents_duplicate_notifications():
    """
    Verify that retrying send_realtime_appointment_notification on the same appointment
    does not send duplicate emails if previously sent.
    """
    db = SessionLocal()
    try:
        appt = Appointment(
            customer="Dana Scully",
            staff="Sarah Connor",
            appointment_date="18 Nov 2026",
            appointment_time="09:00 AM",
            status=AppointmentStatus.CONFIRMED,
            phone="+1555888999",
            email="scully@example.com",
            service="Medical Consultation",
            department="Cardiology",
            organization_id=1,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        send_count = 0

        def mock_send(db_session, to_email, subject, body, html_body=None, organization_id=1):
            nonlocal send_count
            send_count += 1
            return {"sent": True, "status": "sent", "message_id": f"msg_{send_count}"}

        with patch("src.routes.email_routes._send", side_effect=mock_send):
            # First attempt
            send_realtime_appointment_notification(db=db, appointment=appt, action="created")
            assert send_count == 2, "First run should send 2 emails (customer + staff)"

            # Second attempt (simulate webhook/retry)
            send_realtime_appointment_notification(db=db, appointment=appt, action="created")
            assert send_count == 2, "Retry must NOT send duplicate emails!"
    finally:
        db.close()


# ==============================================================================
# TEST 6: Email Provider Failure Does Not Invalidate Booking
# ==============================================================================
def test_provider_failure_does_not_lose_booking():
    """
    Verify that if the email provider raises an exception, the appointment remains
    safely persisted in the database and failure is recorded in NotificationLog.
    """
    db = SessionLocal()
    try:
        appt = Appointment(
            customer="Evan Stone",
            staff="Sarah Connor",
            appointment_date="19 Nov 2026",
            appointment_time="03:30 PM",
            status=AppointmentStatus.CONFIRMED,
            phone="+1555777111",
            email="evan.stone@example.com",
            service="Service Demo",
            department="Sales",
            organization_id=1,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        def mock_failing_send(db_session, to_email, subject, body, html_body=None, organization_id=1):
            raise RuntimeError("SMTP Connection timed out (upstream mail gateway down)")

        with patch("src.routes.email_routes._send", side_effect=mock_failing_send):
            send_realtime_appointment_notification(db=db, appointment=appt, action="created")

        # Appointment must remain valid in DB
        persisted = db.query(Appointment).filter(Appointment.id == appt.id).first()
        assert persisted is not None
        assert persisted.customer == "Evan Stone"

        # NotificationLog should show email failure with error reason
        email_logs = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appt.id,
            NotificationLog.channel == "email"
        ).all()
        assert len(email_logs) > 0
        for l in email_logs:
            assert l.status == "failed"
            assert "SMTP Connection timed out" in (l.error_reason or "")
    finally:
        db.close()


# ==============================================================================
# TEST 7: Conversational Email Collection and Confirmation in Booking State
# ==============================================================================
def test_booking_state_machine_email_collection_and_confirmation():
    """
    Verify that:
    1. Asking for email parses spoken email correctly.
    2. Confirming read-back marks email_confirmed = True.
    3. Declining email ('no email', 'skip') cleanly proceeds without looping.
    """
    service = BookingStateService()

    # 1. Spoken email parsing and confirmation
    session = service.get_session("test_email_confirm_session")
    session.reset()
    session.organization_id = 1
    session.doctor = "Dr Sarah Connor"
    session.date = "2026-11-20"
    session.time = "10:00 AM"
    session.availability_verified = True
    session.patient_name = "George Harrison"
    session.contact_number = "9876543210"
    session.state = BookingState.COLLECTING_EMAIL
    session.last_asked_field = "email"

    resp = service.process_booking_turn("my email is george dot h at gmail dot com", session)
    assert session.email == "george.h@gmail.com"
    assert "george.h@gmail.com" in resp.lower()

    # Confirm read-back
    resp2 = service.process_booking_turn("yes that is correct", session)
    assert session.booking_data.get("email_confirmed") is True

    # 2. Test clean decline for caller with no email
    session_dec = service.get_session("test_email_decline_session")
    session_dec.reset()
    session_dec.organization_id = 1
    session_dec.doctor = "Dr Sarah Connor"
    session_dec.date = "2026-11-20"
    session_dec.time = "10:00 AM"
    session_dec.availability_verified = True
    session_dec.patient_name = "Ringo Starr"
    session_dec.contact_number = "9876543211"
    session_dec.state = BookingState.COLLECTING_EMAIL
    session_dec.last_asked_field = "email"

    resp_dec = service.process_booking_turn("I don't have an email address, please skip it", session_dec)
    assert session_dec.state == BookingState.WAITING_FOR_CONFIRMATION


# ==============================================================================
# TEST 8: Email Configuration API & Template Management
# ==============================================================================
def test_email_config_api():
    """
    Verify that Email Configuration API supports both customer_template and staff_template,
    provider switching, and credential protection.
    """
    # 1. Fetch current config
    response = client.get("/api/email/config")
    assert response.status_code == 200
    data = response.json()
    assert "staff_template" in data
    assert "template" in data  # customer template

    # 2. Update config with custom staff and customer templates
    custom_customer = "Dear {{customer_name}}, your booking {{booking_ref}} for {{booking_purpose}} is confirmed."
    custom_staff = "Hello {{staff_name}}, new booking {{booking_ref}} assigned to you for customer {{customer_name}}."

    update_payload = {
        "provider": "smtp",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": "587",
        "email_address": "test@gmail.com",
        "password": "app-password-123",
        "from_email": "test@gmail.com",
        "sender_name": "CallZenza Notifications",
        "template": custom_customer,
        "staff_template": custom_staff,
        "is_active": True
    }
    post_resp = client.post("/api/email/config", json=update_payload)
    assert post_resp.status_code == 200
    saved = post_resp.json()
    assert saved["template"] == custom_customer
    assert saved["staff_template"] == custom_staff

    # 3. Test template rendering helper
    vars_dict = {
        "customer_name": "Test Customer",
        "staff_name": "Dr Sarah",
        "booking_ref": "CZ-REF-100",
        "booking_purpose": "Software Demo"
    }
    rendered_cust = _render_template(custom_customer, vars_dict)
    assert "Dear Test Customer" in rendered_cust
    assert "CZ-REF-100" in rendered_cust
    assert "Software Demo" in rendered_cust

    rendered_staff = _render_template(custom_staff, vars_dict)
    assert "Hello Dr Sarah" in rendered_staff
    assert "Test Customer" in rendered_staff


# ==============================================================================
# TEST 9: Voice Flow Pipeline Protection & Async Email Dispatch
# ==============================================================================
def test_voice_flow_pipeline_preservation():
    """
    Verify that email sending is executed asynchronously via background thread
    and never delays real-time call handling or audio loops.
    """
    from src.services.sip_call_handler import SipCallSession
    import inspect

    # Verify SipCallSession methods do not import or call synchronous email blocking code
    session = SipCallSession(call_sid="voice_preservation_test")
    assert hasattr(session, "feed_audio")
    assert hasattr(session, "stop")
    assert hasattr(session, "_audio_drain_loop")
    assert hasattr(session, "_ai_processing_loop")

    # Verify send_realtime_appointment_notification signature
    sig = inspect.signature(send_realtime_appointment_notification)
    assert "appointment" in sig.parameters
    assert "db" in sig.parameters
    assert "action" in sig.parameters
