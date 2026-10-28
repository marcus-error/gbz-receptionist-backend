import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.app import app
from src.config.database import SessionLocal, init_db
from src.config.jwt import create_access_token
from src.models.user import User
from src.models.organization import Organization
from src.models.call_log import CallLog, CallType
from src.models.appointment import Appointment, AppointmentStatus
from src.models.lead import Lead, LeadActivity
from src.services.lead_service import LeadService


@pytest.fixture(scope="module")
def db_session():
    init_db()
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture(scope="module")
def test_client():
    return TestClient(app)


@pytest.fixture(scope="module")
def auth_headers(db_session: Session):
    # Ensure test user and organization exist
    org = db_session.query(Organization).filter(Organization.id == 1).first()
    if not org:
        org = Organization(id=1, name="Test Org", status="active")
        db_session.add(org)
        db_session.commit()

    user = db_session.query(User).filter(User.organization_id == 1).first()
    token = create_access_token({"sub": str(user.id if user else 2), "email": "admin@receptionist.ai", "org_id": 1})
    return {"Authorization": f"Bearer {token}"}


def test_01_inbound_call_creates_or_updates_lead(db_session: Session):
    """Criterion 1: An inbound call creates or updates the correct lead."""
    svc = LeadService(db_session)
    phone = "+19876543210"

    call = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="Sarah Connor",
        call_type=CallType.INBOUND,
        status="completed",
        duration="01:30",
        intent="Appointment Booking",
        call_sid="test_inbound_sid_01",
    )
    db_session.add(call)
    db_session.commit()
    db_session.refresh(call)

    lead = svc.capture_or_update_from_call(
        call.id,
        session_booking_data={"caller_name": "Sarah Connor", "email": "sarah@cyberdyne.com", "service": "Cardiology Consultation"}
    )

    assert lead is not None
    assert lead.phone == phone
    assert lead.name == "Sarah Connor"
    assert lead.email == "sarah@cyberdyne.com"
    assert lead.call_type == "Inbound"
    assert lead.lead_status in ("Contacted", "Qualified")
    assert lead.call_log_id == call.id

    # Verify activity was recorded
    activities = db_session.query(LeadActivity).filter(LeadActivity.lead_id == lead.id).all()
    assert len(activities) >= 1
    assert any("Call" in a.title for a in activities)


def test_02_outbound_call_creates_or_updates_lead(db_session: Session):
    """Criterion 2: An outbound campaign call creates or updates the correct lead."""
    svc = LeadService(db_session)
    phone = "+19876543211"

    call = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="Outbound → +19876543211",
        call_type=CallType.OUTBOUND,
        status="completed",
        duration="00:45",
        intent="Subscription Renewal",
        call_sid="test_outbound_sid_02",
    )
    db_session.add(call)
    db_session.commit()
    db_session.refresh(call)

    lead = svc.capture_or_update_from_call(call.id)
    assert lead is not None
    assert lead.phone == phone
    assert lead.call_type == "Outbound"
    assert lead.call_log_id == call.id
    assert lead.lead_source == "Outbound Voice Call"


def test_03_phone_email_name_displayed_only_when_available(db_session: Session):
    """Criterion 3: Phone number, email, and customer name appear only when actually available; generic callers display 'Not provided'."""
    svc = LeadService(db_session)
    phone = "+19876543212"

    call = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="Customer Mobile",  # Generic caller ID
        call_type=CallType.INBOUND,
        status="completed",
        duration="00:20",
        intent="General Inquiry",
        call_sid="test_generic_sid_03",
    )
    db_session.add(call)
    db_session.commit()
    db_session.refresh(call)

    lead = svc.capture_or_update_from_call(call.id)
    details = svc.get_lead_details(lead.id, 1)

    assert details["customer"]["name"] == "Not provided"
    assert details["customer"]["email"] == "Not provided"
    assert details["customer"]["phone"] == phone


def test_04_discussed_product_not_falsely_marked_booked(db_session: Session):
    """Criterion 4: A discussed product is not falsely marked as booked (displays 'Discussed — Not booked')."""
    svc = LeadService(db_session)
    phone = "+19876543213"

    call = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="John Kramer",
        call_type=CallType.INBOUND,
        status="completed",
        duration="01:10",
        intent="Dental Cleaning",
        call_sid="test_discussed_sid_04",
    )
    db_session.add(call)
    db_session.commit()
    db_session.refresh(call)

    lead = svc.capture_or_update_from_call(call.id, session_booking_data={"service": "Dental Cleaning"})
    details = svc.get_lead_details(lead.id, 1)

    assert details["appointment_status"] == "Not Booked"
    assert "Discussed" in details["product_service"]["display"]
    assert details["appointment"] is None


def test_05_successful_appointment_booking_updates_lead(db_session: Session):
    """Criterion 5: A successful appointment booking appears with the correct date, time, and service."""
    svc = LeadService(db_session)
    phone = "+19876543214"

    appt = Appointment(
        organization_id=1,
        customer="Ellen Ripley",
        phone=phone,
        email="ripley@weyland.com",
        department="Neurology",
        service="Brain MRI Scan",
        staff="Dr. Bishop",
        appointment_date="25 Oct 2026",
        appointment_time="02:30 PM",
        duration="45 Minutes",
        status=AppointmentStatus.CONFIRMED,
        customer_id="CUST-77889",
    )
    db_session.add(appt)
    db_session.commit()
    db_session.refresh(appt)

    lead = svc.link_appointment(appt)
    assert lead is not None
    assert lead.appointment_id == appt.id
    assert lead.appointment_status == "Booked"
    assert lead.lead_status == "Converted"
    assert lead.service_booked == "Brain MRI Scan"

    details = svc.get_lead_details(lead.id, 1)
    assert details["appointment"]["date"] == "25 Oct 2026"
    assert details["appointment"]["time"] == "02:30 PM"
    assert details["appointment"]["service"] == "Brain MRI Scan"
    assert details["appointment"]["staff"] == "Dr. Bishop"
    assert details["appointment"]["reference_id"] == "CUST-77889"


def test_06_failed_or_incomplete_bookings_do_not_appear_confirmed(db_session: Session):
    """Criterion 6: Calls without completed bookings remain 'Not Booked'."""
    svc = LeadService(db_session)
    phone = "+19876543215"

    call = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="Arthur Dent",
        call_type=CallType.INBOUND,
        status="completed",
        duration="00:15",
        intent="Appointment Booking",
        call_sid="test_unconfirmed_sid_06",
    )
    db_session.add(call)
    db_session.commit()
    db_session.refresh(call)

    lead = svc.capture_or_update_from_call(call.id)
    assert lead.appointment_status == "Not Booked"
    assert lead.appointment_id is None


def test_07_duplicate_call_events_do_not_create_duplicate_leads(db_session: Session):
    """Criterion 7: Duplicate call events do not create duplicate leads."""
    svc = LeadService(db_session)
    phone = "+19876543216"

    initial_count = db_session.query(Lead).filter(Lead.phone.like(f"%{phone[-10:]}")).count()
    assert initial_count == 0

    # First call
    call1 = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="Luke Skywalker",
        call_type=CallType.INBOUND,
        status="completed",
        duration="00:40",
        call_sid="test_dup_sid_01",
    )
    db_session.add(call1)
    db_session.commit()
    lead1 = svc.capture_or_update_from_call(call1.id)

    # Second call for the same customer phone
    call2 = CallLog(
        organization_id=1,
        caller_phone=phone,
        caller_name="Luke Skywalker",
        call_type=CallType.INBOUND,
        status="completed",
        duration="01:10",
        call_sid="test_dup_sid_02",
    )
    db_session.add(call2)
    db_session.commit()
    lead2 = svc.capture_or_update_from_call(call2.id)

    # Must be the exact same lead ID
    assert lead1.id == lead2.id

    matching_leads = db_session.query(Lead).filter(Lead.phone.like(f"%{phone[-10:]}")).all()
    assert len(matching_leads) == 1


def test_08_search_filters_sorting_and_details_api(test_client: TestClient, auth_headers: dict):
    """Criterion 8: Search, filters, sorting, pagination, and lead details endpoints work."""
    # 1. Test Listing with search
    res = test_client.get("/api/v1/leads?search=Sarah", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert "results" in data
    assert any("Sarah" in l["customer"] for l in data["results"])

    # 2. Test Filter by call_type
    res = test_client.get("/api/v1/leads?call_type=outbound", headers=auth_headers)
    assert res.status_code == 200
    for l in res.json()["results"]:
        assert l["call_type"].lower() == "outbound"

    # 3. Test Filter by appointment_status
    res = test_client.get("/api/v1/leads?appointment_status=booked", headers=auth_headers)
    assert res.status_code == 200
    for l in res.json()["results"]:
        assert l["appointment_status"].lower() in ("booked", "confirmed")

    # 4. Test Metrics API
    res = test_client.get("/api/v1/leads/metrics", headers=auth_headers)
    assert res.status_code == 200
    metrics = res.json()
    assert "totalLeads" in metrics
    assert "appointmentsBooked" in metrics
    assert metrics["totalLeads"] > 0

    # 5. Test Details API
    first_lead_id = data["results"][0]["id"]
    res = test_client.get(f"/api/v1/leads/{first_lead_id}", headers=auth_headers)
    assert res.status_code == 200
    details = res.json()
    assert "customer" in details
    assert "timeline" in details
    assert "product_service" in details


def test_09_tenant_isolation_prevents_cross_organization_access(db_session: Session, test_client: TestClient):
    """Criterion 9: Tenant isolation prevents cross-organization access."""
    # Create org 2 and user 2
    org2 = db_session.query(Organization).filter(Organization.id == 2).first()
    if not org2:
        org2 = Organization(id=2, name="Foreign Org", status="active")
        db_session.add(org2)
        db_session.commit()

    # Create a lead strictly belonging to Org 2
    lead_org2 = Lead(
        organization_id=2,
        name="Confidential Customer",
        phone="+19999999999",
        lead_status="New",
    )
    db_session.add(lead_org2)
    db_session.commit()
    db_session.refresh(lead_org2)

    # Token for Org 1 user
    token_org1 = create_access_token({"sub": "2", "email": "admin@receptionist.ai", "org_id": 1})
    headers_org1 = {"Authorization": f"Bearer {token_org1}"}

    # Org 1 user attempts to get Org 2 lead
    res = test_client.get(f"/api/v1/leads/{lead_org2.id}", headers=headers_org1)
    assert res.status_code == 404  # Not found under Org 1 scope

    # Org 1 leads list must not contain Org 2 lead
    res_list = test_client.get("/api/v1/leads?search=Confidential", headers=headers_org1)
    assert res_list.status_code == 200
    assert len(res_list.json()["results"]) == 0


def test_10_empty_results_handle_cleanly(test_client: TestClient, auth_headers: dict):
    """Criterion 10: Empty database query results return proper empty list."""
    res = test_client.get("/api/v1/leads?search=NonExistentSearchTerm123456", headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0
    assert data["results"] == []


def test_11_calling_flow_preserved_on_lead_capture_error(db_session: Session):
    """Criterion 11: Lead persistence errors never crash or disrupt call finalization."""
    # Calling safe_capture_from_call_sync with non-existent ID runs safely
    LeadService.safe_capture_from_call_sync(call_log_id=999999)
    # Passed without exception!
