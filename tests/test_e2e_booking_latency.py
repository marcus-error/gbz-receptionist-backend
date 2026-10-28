import time
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

from src.services.booking_state_service import (
    BookingStateService,
    BookingState,
    BookingSession,
)
from src.config.database import SessionLocal, engine, Base
from src.models.staff import Staff
from src.models.appointment import Appointment
from src.models.organization import Organization


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.id == 1).first()
        if not org:
            org = Organization(id=1, name="Apollo Test Clinic", is_active=True)
            db.add(org)
            db.commit()

        # Ensure Dr Arun Kumar is in staff directory
        from src.models.staff import StaffStatus
        doc = db.query(Staff).filter(Staff.name == "Dr Arun Kumar", Staff.organization_id == 1).first()
        if not doc:
            doc = Staff(
                name="Dr Arun Kumar",
                organization_id=1,
                department="General Medicine",
                status=StaffStatus.AVAILABLE,
                working_hours="Monday-Saturday: 09:00 - 17:00",
            )
            db.add(doc)
            db.commit()
    finally:
        db.close()
    yield


def test_e2e_multi_turn_booking_flow_and_latency():
    """
    Simulates a full multi-turn caller booking conversation turn-by-turn:
      Turn 1: Doctor selection ("I would like to see Dr Arun Kumar")
      Turn 2: Date selection ("Tomorrow")
      Turn 3: Time selection ("10:00 AM")
      Turn 4: Patient Name ("Sure, Rajesh Kumar") - verifying no false doctor match or surname swallowing
      Turn 5: Phone Number ("9876543210")
      Turn 6: Email & Confirmation ("rajesh dot kumar at gmail dot com") - verifying real DB booking in <= 2.0s
    """
    service = BookingStateService()
    session = service.get_session("e2e_latency_test_session_001")
    session.reset()
    session.organization_id = 1

    turns = [
        ("I would like to see Dr Arun Kumar", BookingState.WAITING_FOR_DATE, "doctor"),
        ("Tomorrow", BookingState.WAITING_FOR_TIME, "date"),
        ("10:00 AM", BookingState.COLLECTING_PATIENT_NAME, "time"),
        ("Sure, Rajesh Kumar", BookingState.COLLECTING_PHONE, "patient_name"),
        ("9876543210", BookingState.COLLECTING_EMAIL, "phone"),
        ("rajesh dot kumar at gmail dot com", BookingState.BOOKED, "email_and_confirmation"),
    ]

    latencies = {}

    # Mock external calendar sync & notification send to avoid outbound network dependencies during unit tests
    with patch("src.services.appointment_service.AppointmentService._sync_appointment_to_calendar_async") as mock_cal_sync, \
         patch("src.services.notification_service.send_realtime_appointment_notification_async") as mock_notif_sync:

        for text, expected_state, turn_name in turns:
            t0 = time.perf_counter()
            reply = service.process_booking_turn(text, session)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            latencies[turn_name] = elapsed_ms

            # Log turn latency
            print(f"\n[TURN: {turn_name}] Elapsed: {elapsed_ms:.2f} ms | Reply: {reply[:60]}... | State: {session.state}")

            # Latency SLA requirement: Fractional-second to <= 2.0s per turn
            assert elapsed_ms < 2000.0, f"Turn '{turn_name}' exceeded 2.0s latency SLA: {elapsed_ms:.2f} ms"

        # Verify final state
        assert session.state == BookingState.BOOKED
        assert session.booking_confirmed is True
        assert session.patient_name == "Rajesh Kumar"
        assert "Arun Kumar" in session.doctor
        assert session.contact_number == "9876543210"
        assert session.email == "rajesh.kumar@gmail.com"
        assert session.appointment_id is not None

        # Verify DB appointment record
        db = SessionLocal()
        try:
            appt = db.query(Appointment).filter(Appointment.id == session.appointment_id).first()
            assert appt is not None
            assert appt.customer == "Rajesh Kumar"
            assert "Arun Kumar" in appt.staff
            assert appt.email == "rajesh.kumar@gmail.com"
            assert appt.phone == "9876543210"
            assert appt.appointment_time == "10:00 AM"
        finally:
            db.close()

    print("\n=== LATENCY RESULTS SUMMARY ===")
    for turn, lat in latencies.items():
        print(f"  - {turn:25}: {lat:7.2f} ms")
    assert all(lat < 2000.0 for lat in latencies.values())
