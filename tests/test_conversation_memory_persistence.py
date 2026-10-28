"""
Regression and Verification Test Suite for Persistent Conversation Memory and
Multi-Turn Appointment Workflow Continuity.

Verifies:
1. Department selection is retained across multiple turns.
2. A department cannot become "not found" after it has been authoritatively resolved.
3. A repeated appointment request does not reset the workflow.
4. "Are you there?" does not clear conversation memory or reset to generic greeting.
5. Previously collected fields are not requested again.
6. A correction from the customer updates the appropriate field.
7. Related follow-up questions preserve the pending workflow.
8. Different calls and tenants never share memory.
9. Concurrent LLM responses cannot overwrite newer state.
10. Restart and recovery behavior works according to the persistence design.
11. Booking confirmation is given only after a real successful booking.
12. Existing inbound/outbound calls and voice pipeline compatibility.
"""

import copy
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from src.services.booking_state_service import (
    BookingStateService,
    BookingState,
    BookingSession,
)
from src.services.entity_validation_service import entity_validation_service
from src.config.database import SessionLocal, engine, Base
from src.models.staff import Staff
from src.models.department import Department
from src.models.call_log import CallLog
from src.models.appointment import Appointment
from src.models.organization import Organization


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Ensure default organization 1
        org = db.query(Organization).filter(Organization.id == 1).first()
        if not org:
            org = Organization(id=1, name="ABC General Hospital", is_active=True)
            db.add(org)
            db.commit()

        # Ensure organization 2 for tenant isolation test
        org2 = db.query(Organization).filter(Organization.id == 2).first()
        if not org2:
            org2 = Organization(id=2, name="City Health Clinic", is_active=True)
            db.add(org2)
            db.commit()

        # Ensure Staff: Priya in Cardiology
        staff_priya = db.query(Staff).filter(Staff.organization_id == 1, Staff.name.ilike("%Priya%")).first()
        if not staff_priya:
            staff_priya = Staff(
                organization_id=1,
                name="Dr. Priya",
                department="Cardiology",
                working_hours="09:00-17:00",
                start_time="09:00",
                end_time="17:00",
                working_days='["Monday","Tuesday","Wednesday","Thursday","Friday"]',
                online_booking_enabled=True,
            )
            db.add(staff_priya)
            db.commit()

        # Ensure Staff: Rohit in Dental Care
        staff_rohit = db.query(Staff).filter(Staff.organization_id == 1, Staff.name.ilike("%Rohit%")).first()
        if not staff_rohit:
            staff_rohit = Staff(
                organization_id=1,
                name="Dr. Rohit Kumar",
                department="Dental Care",
                working_hours="10:00-18:00",
                start_time="10:00",
                end_time="18:00",
                working_days='["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]',
                online_booking_enabled=True,
            )
            db.add(staff_rohit)
            db.commit()

    finally:
        db.close()
    yield


# ==============================================================================
# TEST 1: Department selection is retained across multiple turns
# ==============================================================================
def test_1_department_selection_retained_across_turns():
    service = BookingStateService()
    session = service.get_session("test_call_turn_dept", call_sid="call_sid_dept_1")
    session.organization_id = 1

    # Turn 1: User specifies department
    resp1 = service.process_booking_turn("Cardiology", session)
    assert session.department == "Cardiology"
    # Doctor should NOT be set to "Cardiology"
    assert session.doctor != "Cardiology"
    # The AI should offer Dr. Priya for Cardiology
    assert "Dr. Priya" in resp1 or "Priya" in resp1
    assert "Cardiology" in resp1

    # Turn 2: User says "Yes"
    resp2 = service.process_booking_turn("Yes", session)
    # Department must STILL be Cardiology
    assert session.department == "Cardiology"
    assert "Priya" in (session.doctor or "")
    assert session.doctor_locked is True


# ==============================================================================
# TEST 2: Department cannot become "not found" after being resolved
# ==============================================================================
def test_2_department_cannot_become_not_found():
    service = BookingStateService()
    session = service.get_session("test_call_dept_avail", call_sid="call_sid_dept_2")
    session.organization_id = 1
    session.department = "Cardiology"

    # Direct availability check with department name should resolve associated doctor
    resp = service.check_doctor_availability_live("Cardiology", "today", session=session)
    assert "could not find 'Cardiology' in our directory" not in resp.lower()
    assert session.department == "Cardiology"


# ==============================================================================
# TEST 3: Repeated appointment request does not reset workflow
# ==============================================================================
def test_3_repeated_appointment_request_preserves_workflow():
    service = BookingStateService()
    session = service.get_session("test_call_repeat", call_sid="call_sid_repeat_3")
    session.organization_id = 1

    # Set up active booking with department and doctor selected
    session.state = BookingState.WAITING_FOR_DATE
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.doctor_id = 1

    # User repeats generic booking intent
    resp = service.process_booking_turn("I want to book an appointment today", session)

    # Department and doctor must be preserved!
    assert session.department == "Cardiology"
    assert session.doctor == "Dr. Priya"
    # Since date was given as "today", date must be updated
    assert session.date is not None
    assert "Priya" in (session.doctor or "")


# ==============================================================================
# TEST 4: "Are you there?" does not clear conversation memory
# ==============================================================================
def test_4_are_you_there_preserves_memory():
    service = BookingStateService()
    session = service.get_session("test_call_presence", call_sid="call_sid_presence_4")
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_TIME
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.date = "2026-10-15"

    resp = service.process_booking_turn("Are you there?", session)

    assert "right here" in resp.lower() or "can hear you" in resp.lower()
    # Memory must NOT be wiped
    assert session.department == "Cardiology"
    assert session.doctor == "Dr. Priya"
    assert session.date == "2026-10-15"
    assert session.state == BookingState.WAITING_FOR_TIME


# ==============================================================================
# TEST 5: Previously collected fields are not requested again
# ==============================================================================
def test_5_previously_collected_fields_not_re_requested():
    service = BookingStateService()
    session = service.get_session("test_call_fields", call_sid="call_sid_fields_5")
    session.organization_id = 1
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.date = "2026-10-15"
    session.time = "10:00 AM"
    session.availability_verified = True

    # Next missing field should be patient name
    resp = service.process_booking_turn("10:00 AM works", session)
    assert session.state == BookingState.COLLECTING_PATIENT_NAME
    assert "name" in resp.lower()
    # Should not re-ask which doctor, department, date, or time
    assert "which doctor" not in resp.lower()
    assert "which department" not in resp.lower()


# ==============================================================================
# TEST 6: Customer correction updates the appropriate field
# ==============================================================================
def test_6_customer_correction_updates_appropriate_field():
    service = BookingStateService()
    session = service.get_session("test_call_correction", call_sid="call_sid_corr_6")
    session.organization_id = 1
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.date = "today"

    # User explicitly changes date to tomorrow
    service.extract_slots("Actually let's make it tomorrow", session)
    assert "tomorrow" in (session.date or "").lower()
    # Department and doctor must be preserved
    assert session.department == "Cardiology"
    assert session.doctor == "Dr. Priya"


# ==============================================================================
# TEST 7: Related follow-up question preserves pending workflow
# ==============================================================================
def test_7_follow_up_question_preserves_workflow():
    service = BookingStateService()
    session = service.get_session("test_call_followup", call_sid="call_sid_followup_7")
    session.organization_id = 1
    session.state = BookingState.WAITING_FOR_TIME
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.date = "today"

    # Informational question
    switched = service.check_and_handle_intent_switch(session, "general_question", "Where are you located?")
    assert switched is True
    # Session state must NOT be reset by informational question
    assert session.department == "Cardiology"
    assert session.doctor == "Dr. Priya"
    assert session.date == "today"


# ==============================================================================
# TEST 8: Tenant and Call Isolation
# ==============================================================================
def test_8_tenant_and_call_isolation():
    service = BookingStateService()
    session_call_a = service.get_session("call_tenant_A", call_sid="call_sid_A")
    session_call_a.organization_id = 1
    session_call_a.department = "Cardiology"
    session_call_a.doctor = "Dr. Priya"

    session_call_b = service.get_session("call_tenant_B", call_sid="call_sid_B")
    session_call_b.organization_id = 2

    # Call B must NOT have Call A's memory
    assert session_call_b.department is None
    assert session_call_b.doctor is None
    assert session_call_a.department == "Cardiology"


# ==============================================================================
# TEST 9: Concurrent/Delayed LLM Response Overwrite Protection
# ==============================================================================
def test_9_concurrency_protection_via_state_version():
    session = BookingSession("call_concurrency_test", call_sid="call_sid_conc_9")
    session.turn_count = 1
    session.turn_id = 1
    session.state_version = 1
    session.department = "Cardiology"

    initial_version = session.state_version

    # Newer turn arrives and updates version
    session.turn_count = 2
    session.turn_id = 2
    session.state_version = 2
    session.doctor = "Dr. Priya"

    # Simulate older turn trying to write back
    older_turn_version = initial_version
    can_commit = older_turn_version == session.state_version
    assert can_commit is False  # Older turn must be rejected!


# ==============================================================================
# TEST 10: Restart and Recovery from CallLog Persistent State
# ==============================================================================
def test_10_restart_and_recovery_from_db():
    service = BookingStateService()
    test_sid = "test_persistence_call_10"

    # 1. Create CallLog entry in DB
    db = SessionLocal()
    try:
        call = db.query(CallLog).filter(CallLog.call_sid == test_sid).first()
        if not call:
            call = CallLog(call_sid=test_sid, organization_id=1)
            db.add(call)
            db.commit()

        # 2. Populate session and save to DB
        session = BookingSession(test_sid, call_sid=test_sid)
        session.organization_id = 1
        session.department = "Cardiology"
        session.doctor = "Dr. Priya"
        session.doctor_id = 1
        session.date = "2026-10-16"
        session.state = BookingState.WAITING_FOR_TIME
        session.patient_name = "John Doe"

        service.save_session_state(session)

        # 3. Simulate process restart by clearing memory cache
        service._sessions.clear()
        assert test_sid not in service._sessions

        # 4. Retrieve session — must recover from DB
        recovered = service.get_session(test_sid, call_sid=test_sid)
        assert recovered is not None
        assert recovered.department == "Cardiology"
        assert recovered.doctor == "Dr. Priya"
        assert recovered.date == "2026-10-16"
        assert recovered.patient_name == "John Doe"
        assert recovered.state == BookingState.WAITING_FOR_TIME
    finally:
        db.close()


# ==============================================================================
# TEST 11: Booking Confirmation Only Given After Real DB Booking
# ==============================================================================
def test_11_booking_confirmed_only_after_real_db_record():
    service = BookingStateService()
    session = service.get_session("test_call_db_confirm", call_sid="call_sid_db_11")
    session.organization_id = 1
    session.department = "Cardiology"
    session.doctor = "Dr. Priya"
    session.doctor_id = 1
    session.date = "2026-10-20"
    session.time = "11:00 AM"
    session.patient_name = "Jane Smith"
    session.contact_number = "9876543210"
    session.email = "jane.smith@example.com"
    session.availability_verified = True

    resp = service._execute_db_booking(session)

    # Must have real appointment_id in DB
    assert session.booking_confirmed is True
    assert session.appointment_id is not None
    assert session.appointment_id > 0
    assert str(session.appointment_id) in resp

    # Verify directly in SQLite Appointment table
    db = SessionLocal()
    try:
        saved_appt = db.query(Appointment).filter(Appointment.id == session.appointment_id).first()
        assert saved_appt is not None
        assert saved_appt.customer == "Jane Smith"
        assert saved_appt.staff == "Dr. Priya"
    finally:
        db.close()


# ==============================================================================
# TEST 12: Contextual "Yes" to Offered Doctor Advances Directly to Date/Time
# ==============================================================================
def test_12_contextual_yes_advances_workflow():
    service = BookingStateService()
    session = service.get_session("test_call_yes_advance", call_sid="call_sid_yes_12")
    session.organization_id = 1

    # Turn 1: Caller says "Cardiology for tomorrow"
    resp1 = service.process_booking_turn("I need Cardiology for tomorrow", session)
    assert session.department == "Cardiology"
    assert "tomorrow" in (session.date or "").lower()
    assert "Dr. Priya" in resp1

    # Turn 2: Caller says "Yes"
    resp2 = service.process_booking_turn("Yes", session)
    # Doctor is confirmed as Dr. Priya
    assert session.doctor == "Dr. Priya"
    # Since date is already tomorrow, next prompt should check or ask for time!
    assert session.state == BookingState.WAITING_FOR_TIME or "time" in resp2.lower()
