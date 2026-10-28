import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from src.services.sip_call_handler import SipCallSession
from src.services.rag_service import rag_service
from src.services.booking_state_service import booking_state_service
from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus


def test_end_to_end_active_script_greeting_booking_and_document_switch():
    """
    Required Verification Sequence:
    1. Load active uploaded script.
    2. Start inbound call & verify greeting matches active script.
    3. Say 'Hello?' & verify no repeated full greeting.
    4. Request an appointment.
    5. Respond with doctor/staff from active script & verify preservation.
    6. Ask an informational question during booking & verify grounded answer without state loss.
    7. Resume booking without losing collected details.
    8. Switch active document & verify no stale cache or previous document leakage.
    """
    async def run_sequence():
        db = SessionLocal()
        try:
            # Deactivate all existing documents for organization 1 to ensure clean isolation
            db.query(KnowledgeDocument).filter(KnowledgeDocument.organization_id == 1).update({"ai_usage": "Inactive"})
            db.commit()

            # 1. Create & activate Document 1 (e.g. Apex Dental Care script)
            doc1_content = (
                "Organization Name: Apex Dental Care\n"
                "Opening Greeting: Welcome to Apex Dental Care! How may I assist your smile today?\n"
                "Doctors: Dr. Samuel, Dr. Elena\n"
                "Services: Teeth Whitening, Dental Cleaning, Root Canal\n"
                "Working Hours: Monday to Saturday 9 AM to 6 PM\n"
                "Address: 100 Main Street, Apex City\n"
            )
            doc1 = KnowledgeDocument(
                title="apex_dental_script.txt",
                content=doc1_content,
                organization_id=1,
                ai_usage="Active",
                document_type="SCRIPT",
                status=KnowledgeDocStatus.INDEXED,
            )
            db.add(doc1)
            db.commit()
            db.refresh(doc1)
            doc1_id = str(doc1.id)

            # Flush RAG cache
            rag_service.clear_organization_cache(1)

            # 2. Start call & check greeting
            session1 = SipCallSession(
                caller_phone="+1987654321",
                caller_name="Alice Smith",
                organization_id=1,
                active_script_id=doc1_id,
            )

            greetings_sent = []
            async def mock_speak_greeting(text, intent="", metrics=None):
                greetings_sent.append(text)

            session1._speak = mock_speak_greeting
            await session1._send_opening_greeting()

            assert len(greetings_sent) == 1
            assert "Apex Dental Care" in greetings_sent[0] or "Welcome to Apex Dental" in greetings_sent[0]

            # 3. Say 'Hello?' -> Should NOT replay full greeting
            responses = []
            async def mock_speak(text, intent="", metrics=None):
                responses.append(text)

            session1._speak = mock_speak
            await session1._run_ai_pipeline("Hello?", metrics={"utt_id": "u1"})
            assert len(responses) >= 1
            assert "Welcome to Apex Dental Care!" not in responses[-1]

            # 4. Request an appointment
            responses.clear()
            await session1._run_ai_pipeline("I want to book an appointment", metrics={"utt_id": "u2"})
            assert len(responses) >= 1
            assert any(k in responses[-1].lower() for k in ["doctor", "specialist", "service", "who"])

            # 5. Select doctor from active script ("Dr. Samuel")
            responses.clear()
            await session1._run_ai_pipeline("Dr. Samuel", metrics={"utt_id": "u3"})
            assert len(responses) >= 1
            assert session1.booking_session.doctor == "Dr. Samuel"
            assert any(k in responses[-1].lower() for k in ["date", "day", "when", "prefer"])

            # 6. Ask informational question during booking ("What are your working hours?")
            responses.clear()
            await session1._run_ai_pipeline("What are your working hours?", metrics={"utt_id": "u4"})
            assert len(responses) >= 1
            full_resp = " ".join(responses)
            assert "9 AM to 6 PM" in full_resp or "hours" in full_resp.lower()
            # Verify booking state still preserved!
            assert session1.booking_session.doctor == "Dr. Samuel"

            # 7. Provide date to resume booking ("Tomorrow")
            responses.clear()
            await session1._run_ai_pipeline("Tomorrow", metrics={"utt_id": "u5"})
            assert len(responses) >= 1
            assert session1.booking_session.date is not None
            assert session1.booking_session.doctor == "Dr. Samuel"

            # 8. Switch active document to Document 2 (e.g. Zenith Eye Clinic)
            doc1.ai_usage = "Inactive"
            doc2_content = (
                "Organization Name: Zenith Eye Clinic\n"
                "Opening Greeting: Hello! Thank you for contacting Zenith Eye Clinic. How may we help your vision today?\n"
                "Doctors: Dr. Visionary, Dr. Optics\n"
                "Services: LASIK, Cataract Surgery, Eye Exam\n"
                "Working Hours: Monday to Friday 10 AM to 4 PM\n"
                "Address: 200 Vision Way, Optics Town\n"
            )
            doc2 = KnowledgeDocument(
                title="zenith_eye_script.txt",
                content=doc2_content,
                organization_id=1,
                ai_usage="Active",
                document_type="SCRIPT",
                status=KnowledgeDocStatus.INDEXED,
            )
            db.add(doc2)
            db.commit()
            db.refresh(doc2)
            doc2_id = str(doc2.id)

            # Flush RAG cache
            rag_service.clear_organization_cache(1)

            # New session under doc2
            session2 = SipCallSession(
                caller_phone="+1987654322",
                caller_name="Bob Jones",
                organization_id=1,
                active_script_id=doc2_id,
            )

            responses2 = []
            async def mock_speak2(text, intent="", metrics=None):
                responses2.append(text)

            session2._speak = mock_speak2
            await session2._send_opening_greeting()

            assert len(responses2) == 1
            assert "Zenith Eye Clinic" in responses2[0]
            assert "Apex Dental Care" not in responses2[0]

            # Query location for doc2 -> must return Zenith Eye Clinic address, NOT Apex Dental Care
            responses2.clear()
            await session2._run_ai_pipeline("Where are you located?", metrics={"utt_id": "u6"})
            assert len(responses2) >= 1
            full_resp2 = " ".join(responses2)
            assert "Optics Town" in full_resp2 or "Vision Way" in full_resp2
            assert "Apex City" not in full_resp2

            # Clean up test documents from DB
            db.delete(doc1)
            db.delete(doc2)
            db.commit()

        finally:
            db.close()

    asyncio.run(run_sequence())
