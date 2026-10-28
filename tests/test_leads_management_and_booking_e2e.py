import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
from sqlalchemy import or_

from src.config.database import SessionLocal
from src.models.lead import Lead
from src.models.appointment import Appointment
from src.models.staff import Staff
from src.services.booking_state_service import booking_state_service, BookingState
from src.services.lead_service import LeadService


TEST_PHONES = ["9876543219", "+919876543219", "9123456789", "9998887776", "9443322110", "9555111222"]
TEST_NAMES = ["Sanjay Kumar", "Rahul Sharma", "Priya Raman", "Tenant One Customer", "Tenant Two Customer", "Robert Bruce", "Vijay Anand"]
TEST_DOCTOR = "Dr. TestBookingDoc"


@pytest.fixture(autouse=True)
def clean_test_data():
    """Ensure isolated database state before and after each test."""
    db = SessionLocal()
    try:
        # Pre-cleanup test data
        db.query(Appointment).filter(or_(Appointment.phone.in_(TEST_PHONES), Appointment.staff == TEST_DOCTOR)).delete(synchronize_session=False)
        db.query(Lead).filter(or_(Lead.phone.in_(TEST_PHONES), Lead.name.in_(TEST_NAMES))).delete(synchronize_session=False)
        db.query(Staff).filter(Staff.name == TEST_DOCTOR).delete(synchronize_session=False)
        db.commit()

        # Ensure a test staff doctor exists
        doctor = Staff(
            name=TEST_DOCTOR,
            department="General Medicine",
            organization_id=1,
            email="dr.testdoc@example.com",
            phone="9876500000"
        )
        db.add(doctor)
        db.commit()

        yield db
    finally:
        # Post-cleanup test data
        db.query(Appointment).filter(or_(Appointment.phone.in_(TEST_PHONES), Appointment.staff == TEST_DOCTOR)).delete(synchronize_session=False)
        db.query(Lead).filter(or_(Lead.phone.in_(TEST_PHONES), Lead.name.in_(TEST_NAMES))).delete(synchronize_session=False)
        db.query(Staff).filter(Staff.name == TEST_DOCTOR).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_e2e_conversational_booking_flow_and_lead_created_before_booking():
    """
    Acceptance Criteria 1-7:
    - AI collects customer details conversationally (Name, Phone, Email)
    - Suggests and confirms caller ID
    - Lead is automatically created in DB before appointment finishes
    - Lead appears in Leads Management table and is updated as conversation progresses
    - Books calendar only after confirmation and updates lead with calendar_event_id and status Booked/Converted
    """
    session_id = f"test_e2e_{int(datetime.now().timestamp())}"
    session = booking_state_service.get_session(
        session_id=session_id,
        conversation_id=session_id,
        call_sid=f"CALL_{session_id}"
    )
    session.organization_id = 1
    session.caller_phone = "+919876543219"
    session.caller_name = "Caller"
    session.conversation_language = "en"

    # Turn 1: Caller states intent to book
    resp1 = booking_state_service.process_booking_turn(
        f"I want to book an appointment with {TEST_DOCTOR} on Monday at 10 AM", session
    )
    assert "May I" in resp1 and "full name" in resp1.lower()
    assert session.state == BookingState.COLLECTING_PATIENT_NAME

    # Turn 2: Caller provides full name
    resp2 = booking_state_service.process_booking_turn(
        "My name is Sanjay Kumar", session
    )
    assert session.patient_name == "Sanjay Kumar"
    # AI must suggest caller ID and ask for confirmation
    assert "+919876543219" in resp2
    assert "use this number" in resp2.lower()

    # CRITICAL CHECK: Lead MUST be saved in database BEFORE appointment is booked
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(
            Lead.organization_id == 1,
            Lead.name == "Sanjay Kumar"
        ).first()
        assert lead is not None, "Lead was not saved to database before appointment booking completed!"
        assert lead.lead_status in ("New", "Contacted")
        assert lead.appointment_status in ("Pending", "Not Booked")
        assert lead.follow_up_required is True
        assert lead.lead_source == "AI Receptionist / Inbound Call"
    finally:
        db.close()

    # Turn 3: Caller confirms caller ID
    resp3 = booking_state_service.process_booking_turn("Yes, use that number", session)
    assert session.contact_number == "+919876543219"
    # AI must ask for email address
    assert "email" in resp3.lower()
    assert session.state == BookingState.COLLECTING_EMAIL

    # Turn 4: Caller provides email
    resp4 = booking_state_service.process_booking_turn("sanjay.kumar@gmail.com", session)
    assert session.email == "sanjay.kumar@gmail.com"
    # AI reads back details for confirmation
    assert "confirm" in resp4.lower() or "correct" in resp4.lower()
    assert "Sanjay Kumar" in resp4
    assert "+919876543219" in resp4
    assert "sanjay.kumar@gmail.com" in resp4
    assert session.state == BookingState.WAITING_FOR_CONFIRMATION

    # Turn 5: Caller confirms details -> Triggers booking
    mock_cal_cfg = MagicMock()
    mock_cal_cfg.timezone = "UTC"
    with patch("src.services.calendar_service.CalendarService.get_connected_config", return_value=mock_cal_cfg), \
         patch("src.services.calendar_service.CalendarService.create_event", return_value={"id": "gcal_evt_998877"}):
        resp5 = booking_state_service.process_booking_turn("Yes, that is correct", session)

    assert "confirmed" in resp5.lower() or "Appointment ID" in resp5
    assert session.is_completed is True
    assert session.appointment_id is not None

    # Verify final Lead status in database
    db = SessionLocal()
    try:
        lead_final = db.query(Lead).filter(
            Lead.organization_id == 1,
            Lead.phone.like("%9876543219")
        ).first()
        assert lead_final is not None
        assert lead_final.email == "sanjay.kumar@gmail.com"
        assert lead_final.lead_status == "Converted"
        assert lead_final.appointment_status == "Booked"
        assert lead_final.follow_up_required is False
        assert lead_final.appointment_id == session.appointment_id
        assert lead_final.calendar_event_id == "gcal_evt_998877"

        # Check Leads Management listing API returns this lead
        lead_svc = LeadService(db)
        listing = lead_svc.get_leads(organization_id=1, search="Sanjay")
        assert listing["total"] >= 1
        found = any(r["customer"] == "Sanjay Kumar" for r in listing["results"])
        assert found is True
    finally:
        db.close()
        booking_state_service.remove_session(session_id)


def test_caller_id_different_number_and_validation():
    """
    Acceptance Criteria:
    - Caller ID suggested, but caller chooses a different phone number
    - Validation of phone number (rejects incomplete fragments, prompts clearly)
    """
    session_id = f"test_diff_num_{int(datetime.now().timestamp())}"
    session = booking_state_service.get_session(
        session_id=session_id,
        conversation_id=session_id
    )
    session.organization_id = 1
    session.caller_phone = "+919876543219"
    session.doctor = TEST_DOCTOR
    session.date = "tomorrow"
    session.time = "11:00 AM"
    session.availability_verified = True
    session.patient_name = "Rahul Sharma"
    session.state = BookingState.COLLECTING_PHONE

    # First turn: AI suggests caller phone
    resp1 = booking_state_service.process_booking_turn("Hello", session)
    assert "+919876543219" in resp1

    # Caller provides a completely different valid phone number
    resp2 = booking_state_service.process_booking_turn("No, please use 9123456789", session)
    assert session.contact_number == "9123456789"
    assert "email" in resp2.lower()
    booking_state_service.remove_session(session_id)


def test_lead_deduplication_by_phone_and_email():
    """
    Acceptance Criteria 5 & 9:
    - Prevent duplicate leads using tenant ID, normalized phone number, and email.
    - If lead already exists, update the existing record instead of creating duplicates.
    """
    session_id_1 = f"test_dedup_1_{int(datetime.now().timestamp())}"
    session_id_2 = f"test_dedup_2_{int(datetime.now().timestamp())}"

    session1 = booking_state_service.get_session(session_id_1)
    session1.organization_id = 1
    session1.contact_number = "9555111222"
    session1.caller_phone = "9555111222"
    session1.patient_name = "Priya Raman"
    session1.email = None
    session1.doctor = TEST_DOCTOR
    session1.state = BookingState.COLLECTING_EMAIL

    db = SessionLocal()
    try:
        lead_svc = LeadService(db)

        # 1. First interaction: Save lead with phone
        lead1 = lead_svc.upsert_lead_from_session(session1)
        db.commit()
        lead1_id = lead1.id
        assert lead1_id is not None

        # 2. Second interaction: Same phone, now providing email
        session2 = booking_state_service.get_session(session_id_2)
        session2.organization_id = 1
        session2.contact_number = "+919555111222"  # Variations with country code
        session2.caller_phone = "9555111222"
        session2.patient_name = "Priya Raman"
        session2.email = "priya.raman@example.com"
        session2.doctor = TEST_DOCTOR
        session2.state = BookingState.WAITING_FOR_CONFIRMATION

        lead2 = lead_svc.upsert_lead_from_session(session2)
        db.commit()

        # Must be the SAME lead, not a duplicate!
        assert lead2.id == lead1_id
        assert lead2.email == "priya.raman@example.com"

        # Check count in DB for this phone
        count = db.query(Lead).filter(
            Lead.organization_id == 1,
            Lead.phone.like("%9555111222")
        ).count()
        assert count == 1, f"Expected exactly 1 lead record, found {count}!"
    finally:
        db.close()
        booking_state_service.remove_session(session_id_1)
        booking_state_service.remove_session(session_id_2)


def test_multi_tenant_isolation():
    """
    Acceptance Criteria 6 & 11:
    - Multi-tenant security: Leads belong strictly to the correct tenant (organization_id).
    - Businesses never see leads belonging to other tenants.
    """
    session_id_t1 = f"test_t1_{int(datetime.now().timestamp())}"
    session_id_t2 = f"test_t2_{int(datetime.now().timestamp())}"

    session_t1 = booking_state_service.get_session(session_id_t1)
    session_t1.organization_id = 1
    session_t1.contact_number = "9998887776"
    session_t1.patient_name = "Tenant One Customer"
    session_t1.email = "cust@tenant1.com"
    session_t1.doctor = TEST_DOCTOR
    session_t1.state = BookingState.COLLECTING_EMAIL

    session_t2 = booking_state_service.get_session(session_id_t2)
    session_t2.organization_id = 2
    session_t2.contact_number = "9998887776"
    session_t2.patient_name = "Tenant Two Customer"
    session_t2.email = "cust@tenant2.com"
    session_t2.doctor = "Dr. Jane"
    session_t2.state = BookingState.COLLECTING_EMAIL

    db = SessionLocal()
    try:
        lead_svc = LeadService(db)

        # Create lead for Tenant 1
        lead_t1 = lead_svc.upsert_lead_from_session(session_t1)
        db.commit()

        # Create lead for Tenant 2 with same phone
        lead_t2 = lead_svc.upsert_lead_from_session(session_t2)
        db.commit()

        assert lead_t1.id != lead_t2.id, "Tenant leads collided!"
        assert lead_t1.organization_id == 1
        assert lead_t2.organization_id == 2

        # Verify query isolation: Tenant 1 list only returns Tenant 1 leads
        t1_leads = lead_svc.get_leads(organization_id=1, search="9998887776")
        assert len(t1_leads["results"]) == 1
        assert t1_leads["results"][0]["customer"] == "Tenant One Customer"

        t2_leads = lead_svc.get_leads(organization_id=2, search="9998887776")
        assert len(t2_leads["results"]) == 1
        assert t2_leads["results"][0]["customer"] == "Tenant Two Customer"
    finally:
        db.query(Lead).filter(Lead.phone.like("%9998887776")).delete(synchronize_session=False)
        db.commit()
        db.close()
        booking_state_service.remove_session(session_id_t1)
        booking_state_service.remove_session(session_id_t2)


def test_lead_preserved_on_mid_conversation_disconnect():
    """
    Acceptance Criteria 3 & 4:
    - Customer provides details and disconnects mid-conversation before booking.
    - Information is preserved, marked as requiring follow-up.
    """
    session_id = f"test_disc_{int(datetime.now().timestamp())}"
    session = booking_state_service.get_session(
        session_id=session_id,
        conversation_id=session_id
    )
    session.organization_id = 1
    session.caller_phone = "9443322110"

    # Turn 1: Customer gives name and phone in compound response
    booking_state_service.process_booking_turn(
        "I'm Robert Bruce and my phone number is 9443322110", session
    )

    # Customer abruptly disconnects without completing appointment!
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(
            Lead.organization_id == 1,
            Lead.phone.like("%9443322110")
        ).first()
        assert lead is not None, "Lead was not preserved on mid-conversation disconnect!"
        assert lead.name == "Robert Bruce"
        assert lead.phone == "9443322110"
        assert lead.lead_status in ("New", "Contacted")
        assert lead.follow_up_required is True
    finally:
        db.close()
        booking_state_service.remove_session(session_id)


def test_calendar_sync_failure_preserves_lead_without_false_confirmation():
    """
    Acceptance Criteria 7, 9, 10:
    - Never claim success if calendar operation fails.
    - Failed calendar booking preserves lead with follow-up required.
    """
    session_id = f"test_cal_fail_{int(datetime.now().timestamp())}"
    session = booking_state_service.get_session(
        session_id=session_id,
        conversation_id=session_id
    )
    session.organization_id = 1
    session.doctor = TEST_DOCTOR
    session.date = "tomorrow"
    session.time = "10:00 AM"
    session.availability_verified = True
    session.patient_name = "Vijay Anand"
    session.contact_number = "9876543219"
    session.email = "vijay@example.com"
    session.state = BookingState.WAITING_FOR_CONFIRMATION

    # Mock Google Calendar sync failure (e.g. 409 conflict or API exception)
    with patch(
        "src.services.appointment_service.AppointmentService._sync_appointment_to_calendar",
        side_effect=HTTPException(status_code=409, detail="Requested slot on calendar is already booked")
    ):
        resp = booking_state_service.process_booking_turn("Yes, confirm the booking", session)

    # AI must NOT claim appointment was booked or confirmed
    assert "has been successfully confirmed" not in resp
    assert "Appointment ID #" not in resp
    assert "taken" in resp.lower() or "issue" in resp.lower() or "follow up" in resp.lower()

    # Lead must be preserved with follow-up required
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(
            Lead.organization_id == 1,
            Lead.name == "Vijay Anand"
        ).first()
        assert lead is not None, "Lead was lost on calendar failure!"
        assert lead.follow_up_required is True
        assert lead.appointment_status in ("Failed", "Pending", "Not Booked")
    finally:
        db.close()
        booking_state_service.remove_session(session_id)
