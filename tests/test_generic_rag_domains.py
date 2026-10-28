"""
Comprehensive Multi-Domain Tests for Generic RAG-Driven AI Receptionist.

Verifies that "Schedule an appointment" or "Book an appointment":
1. Does NOT assume Doctor/Hospital entities by default.
2. Dynamically derives entity terminology (Staff, Service, Department) from the active RAG.
3. Works across Medical, Salon, School, and Software/AI Product organizations.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.services.rag_service import rag_service
from src.services.booking_state_service import booking_state_service, BookingState, BookingSession


# -----------------------------------------------------------------------------
# 1. SALON DOMAIN TEST
# -----------------------------------------------------------------------------
SALON_RAG_TEXT = """
Organization Name: Velvet Touch Salon & Spa
Services Offered:
- Haircut ($35)
- Hair Coloring ($85)
- Facial & Skin Spa ($60)
- Manicure & Pedicure ($45)

Stylists and Staff:
- Sarah (Master Stylist)
- John (Hair Color Specialist)
- Priya (Spa Therapist)

Working Hours: Monday to Saturday 9:00 AM - 8:00 PM.
"""

def test_salon_rag_domain():
    """Verify Salon RAG asks for service/stylist and NEVER mentions Doctor."""
    with patch.object(rag_service, "get_organization_master_context", return_value=SALON_RAG_TEXT):
        terminology = rag_service.get_appointment_entity_terminology(organization_id=2, document_id="salon_doc")
        assert terminology["service_based"] is True
        assert terminology["entity_term"] == "service"
        assert "doctor" not in terminology["ask_target_question"].lower()

        session = BookingSession("test_salon_sess")
        session.reset()
        session.organization_id = 2
        session.active_doc_id = "salon_doc"
        session.state = BookingState.WAITING_FOR_DOCTOR  # Unknown target state

        q_ta = booking_state_service._get_target_entity_question(session, lang="ta")
        q_en = booking_state_service._get_target_entity_question(session, lang="en")
        q_hi = booking_state_service._get_target_entity_question(session, lang="hi")

        assert "doctor" not in q_ta.lower()
        assert "doctor" not in q_en.lower()
        assert "doctor" not in q_hi.lower()
        assert "service" in q_en.lower() or "book" in q_en.lower()


# -----------------------------------------------------------------------------
# 2. SCHOOL DOMAIN TEST
# -----------------------------------------------------------------------------
SCHOOL_RAG_TEXT = """
Organization Name: Saint Joseph High School
Services & Enquiries:
- Admission Enquiry
- Campus Visit & Tour
- Parent-Teacher Meeting
- Academic Counselling

Staff Members:
- Admissions Officer
- Guidance Counsellor

Visiting Hours: Weekdays 10:00 AM - 3:00 PM.
"""

def test_school_rag_domain():
    """Verify School RAG asks for teacher/service/enquiry and NEVER mentions Doctor."""
    with patch.object(rag_service, "get_organization_master_context", return_value=SCHOOL_RAG_TEXT):
        terminology = rag_service.get_appointment_entity_terminology(organization_id=3, document_id="school_doc")
        assert "doctor" not in (terminology.get("entity_term") or "").lower()
        assert "doctor" not in terminology.get("ask_target_question", "").lower()

        session = BookingSession("test_school_sess")
        session.reset()
        session.organization_id = 3
        session.active_doc_id = "school_doc"
        session.state = BookingState.WAITING_FOR_DOCTOR

        q_en = booking_state_service._get_target_entity_question(session, lang="en")
        assert "doctor" not in q_en.lower()


# -----------------------------------------------------------------------------
# 3. SOFTWARE / AI PRODUCT DOMAIN TEST
# -----------------------------------------------------------------------------
SOFTWARE_RAG_TEXT = """
Organization Name: CallZenza AI
Product & Services:
- AI Receptionist Platform Demo
- Voice Agent Integration Consultation
- Custom RAG Pipeline Setup

Team:
- Technical Support Team
- Sales Representative

Operating Hours: 24/7 Virtual Desk.
"""

def test_software_rag_domain():
    """Verify Software/AI RAG asks for service/product demo and NEVER mentions Doctor."""
    with patch.object(rag_service, "get_organization_master_context", return_value=SOFTWARE_RAG_TEXT):
        terminology = rag_service.get_appointment_entity_terminology(organization_id=1, document_id="callzenza_doc")
        assert terminology["service_based"] is True
        assert terminology["entity_term"] == "service"
        assert "doctor" not in terminology["ask_target_question"].lower()

        session = BookingSession("test_software_sess")
        session.reset()
        session.organization_id = 1
        session.active_doc_id = "callzenza_doc"
        session.state = BookingState.WAITING_FOR_DOCTOR

        q_ta = booking_state_service._get_target_entity_question(session, lang="ta")
        q_en = booking_state_service._get_target_entity_question(session, lang="en")

        assert "doctor" not in q_ta.lower()
        assert "doctor" not in q_en.lower()


# -----------------------------------------------------------------------------
# 4. MEDICAL DOMAIN TEST
# -----------------------------------------------------------------------------
MEDICAL_RAG_TEXT = """
Organization Name: Universal Medical Clinic
Doctor & Physician Directory:
- Dr. Specialist One (General Medicine)
- Dr. Specialist Two (Cardiology)

Departments & Services:
- General Consultation
- Cardiac Screening
- Health Checkup

Working Hours: 8:00 AM - 6:00 PM Daily.
"""

def test_medical_rag_domain():
    """Verify Medical RAG properly identifies doctor/specialist terminology."""
    with patch.object(rag_service, "get_organization_master_context", return_value=MEDICAL_RAG_TEXT):
        terminology = rag_service.get_appointment_entity_terminology(organization_id=4, document_id="hospital_doc")
        assert terminology["entity_term"] == "doctor"
        assert "doctor" in terminology["ask_target_question"].lower()

        session = BookingSession("test_medical_sess")
        session.reset()
        session.organization_id = 4
        session.active_doc_id = "hospital_doc"
        session.state = BookingState.WAITING_FOR_DOCTOR

        q_en = booking_state_service._get_target_entity_question(session, lang="en")
        assert "doctor" in q_en.lower()
