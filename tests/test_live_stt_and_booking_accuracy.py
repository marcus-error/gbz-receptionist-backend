"""
Regression tests for Deepgram live phone STT accuracy & Appointment State Machine.

Verifies:
1. Customer utterance "My name" is never extracted as a person's name "Name".
2. System prompts for customer's full name when partial or invalid name is provided.
3. Valid person names ("John Doe", "Rahul Sharma") are extracted accurately.
4. Flexible appointment slot selection ("any time", "earliest", "afternoon") resolves correctly.
5. Deepgram STT endpointing and debounce timings prevent premature finalization during natural pauses.
"""

import pytest
from src.services.booking_state_service import (
    BookingStateService, BookingSession, BookingState
)
from src.services.deepgram_stt_service import DeepgramStreamingSession


def test_is_valid_person_name_rejects_placeholders():
    """Verify placeholder strings and conversational words are rejected as names."""
    service = BookingStateService()

    # Placeholders & non-names
    assert service.is_valid_person_name("Name") is False
    assert service.is_valid_person_name("name") is False
    assert service.is_valid_person_name("My Name") is False
    assert service.is_valid_person_name("Patient") is False
    assert service.is_valid_person_name("Customer") is False
    assert service.is_valid_person_name("Full Name") is False
    assert service.is_valid_person_name("Doctor") is False
    assert service.is_valid_person_name("Yes") is False
    assert service.is_valid_person_name("Tomorrow") is False
    assert service.is_valid_person_name("") is False
    assert service.is_valid_person_name("   ") is False

    # Valid human names
    assert service.is_valid_person_name("John Doe") is True
    assert service.is_valid_person_name("Rahul Sharma") is True
    assert service.is_valid_person_name("Alice") is True
    assert service.is_valid_person_name("Jane Smith") is True


def test_extract_patient_name_does_not_extract_name_keyword():
    """Verify that 'My name' does not extract 'Name'."""
    service = BookingStateService()
    session = service.get_session("test_name_extract_session")

    # Incomplete utterances must not set patient_name
    for utterance in ["My name", "my name is", "this is", "it's"]:
        session.reset()
        session.doctor = "Dr. Smith"
        session.date = "2026-10-15"
        session.time = "10:00 AM"
        session.availability_verified = True
        session.state = BookingState.WAITING_FOR_PATIENT_NAME
        service.process_booking_turn(utterance, session)
        assert session.patient_name is None, f"Expected None for utterance: {utterance}, got {session.patient_name}"

    # Valid name utterances should set patient_name properly
    valid_cases = [
        ("My name is John Doe", "John Doe"),
        ("I am Jane Smith", "Jane Smith"),
        ("This is Michael Scott", "Michael Scott"),
        ("Call me Robert", "Robert"),
    ]
    for utterance, expected_name in valid_cases:
        session.reset()
        session.doctor = "Dr. Smith"
        session.date = "2026-10-15"
        session.time = "10:00 AM"
        session.availability_verified = True
        session.state = BookingState.WAITING_FOR_PATIENT_NAME
        service.process_booking_turn(utterance, session)
        assert session.patient_name == expected_name, f"Expected {expected_name} for '{utterance}', got {session.patient_name}"


def test_turn_handling_when_customer_says_my_name():
    """
    Test: Customer is in WAITING_FOR_PATIENT_NAME and says 'My name'.
    Expected: AI does NOT say 'Thanks, Name.' It prompts for their full name.
    """
    service = BookingStateService()
    session = service.get_session("test_my_name_session")
    session.reset()

    session.doctor = "Dr. Smith"
    session.date = "2026-10-15"
    session.time = "10:00 AM"
    session.availability_verified = True
    session.state = BookingState.WAITING_FOR_PATIENT_NAME

    # Customer speaks incomplete utterance "My name"
    resp = service.process_booking_turn("My name", session)

    # Must NOT address them as "Name"
    assert "Thanks, Name" not in resp
    assert "Name" != (session.patient_name or "")
    assert session.patient_name is None
    assert "full name" in resp.lower() or "name" in resp.lower()

    # Customer completes with actual name
    resp2 = service.process_booking_turn("My name is Sarah Connor", session)
    assert session.patient_name == "Sarah Connor"
    assert "sarah" in resp2.lower()
    assert "phone" in resp2.lower() or "email" in resp2.lower()



def test_flexible_slot_selection():
    """Verify natural slot utterances like 'any time', 'earliest', 'afternoon' match properly."""
    service = BookingStateService()
    session = service.get_session("test_slot_selection_session")
    session.reset()

    session.doctor = "Dr. Test"
    session.date = "2026-10-15"
    session.state = BookingState.WAITING_FOR_TIME
    session.offered_slots = ["10:00 AM", "11:30 AM", "02:00 PM", "04:30 PM"]

    # "any time" or "earliest" picks first slot
    resp1 = service.process_booking_turn("Any time works", session)
    assert session.time == "10:00 AM"

    # Reset time and test afternoon
    session.time = None
    session.state = BookingState.WAITING_FOR_TIME
    resp2 = service.process_booking_turn("Prefer afternoon", session)
    assert session.time == "02:00 PM"


def test_deepgram_stt_timing_configurations():
    """Verify Deepgram stream endpointing and debounce timings."""
    stt = DeepgramStreamingSession(call_id="test_timing_call")
    # Utterance end should be at least 1000ms to allow natural speech pauses
    assert stt.utterance_end_ms >= 1000
    # Endpointing should be at least 400ms
    assert stt.endpointing_ms >= 400


def test_booking_state_priority_while_collecting_name_or_email():
    """Verify booking state is NOT bypassed by general_conversation intent during name/email collection."""
    service = BookingStateService()
    session = service.get_session("test_priority_session")
    session.reset()

    session.doctor = "Dr. Smith"
    session.date = "2026-10-15"
    session.time = "10:00 AM"

    # In COLLECTING_PATIENT_NAME stage
    session.state = BookingState.COLLECTING_PATIENT_NAME
    session.last_asked_field = "patient_name"
    assert service.check_and_handle_intent_switch(session, "general_conversation", "my name is Sarah Connor") is False
    assert service.check_and_handle_intent_switch(session, "greeting", "hello") is False

    # In COLLECTING_EMAIL stage
    session.patient_name = "Sarah Connor"
    session.state = BookingState.COLLECTING_EMAIL
    session.last_asked_field = "email"
    assert service.check_and_handle_intent_switch(session, "general_conversation", "sarah@gmail.com") is False
    assert service.check_and_handle_intent_switch(session, "general_conversation", "@gmail.com.") is False


def test_incomplete_email_rejection_and_repeat_request():
    """Verify incomplete emails like '@gmail.com.' are rejected with a prompt to repeat or spell out."""
    service = BookingStateService()
    session = service.get_session("test_email_reject_session")
    session.reset()

    session.doctor = "Dr. Smith"
    session.date = "2026-10-15"
    session.time = "10:00 AM"
    session.patient_name = "Email Reject Test User"
    session.contact_number = "9876543299"
    session.availability_verified = True
    session.state = BookingState.COLLECTING_EMAIL
    session.last_asked_field = "email"

    # Incomplete email utterance
    resp = service.process_booking_turn("@gmail.com.", session)
    assert session.email is None
    assert "didn't catch the full email" in resp.lower() or "repeat or spell" in resp.lower()

    # Valid email utterance
    resp2 = service.process_booking_turn("sarah.connor@gmail.com", session)
    assert session.email == "sarah.connor@gmail.com"
    service.remove_session("test_email_reject_session")


@pytest.mark.anyio
async def test_deepgram_multi_chunk_accumulation():
    """Verify Deepgram streaming accumulates multi-chunk speech without overwriting earlier words."""
    finals = []
    async def mock_on_final(utt_id, text, metrics):
        finals.append((utt_id, text))

    stt = DeepgramStreamingSession(
        call_id="test_acc_call",
        on_final=mock_on_final,
    )
    stt._running = True
    stt._connected = True

    # Chunk 1: is_final=True, speech_final=False (e.g. "rahul")
    chunk1 = {
        "channel": {"alternatives": [{"transcript": "rahul", "confidence": 0.95}]},
        "is_final": True,
        "speech_final": False,
        "duration": 0.6,
    }
    await stt._handle_results_message(chunk1)
    assert stt._accumulated_finals == ["rahul"]

    # Chunk 2: is_final=True, speech_final=True (e.g. "@gmail.com")
    chunk2 = {
        "channel": {"alternatives": [{"transcript": "@gmail.com", "confidence": 0.92}]},
        "is_final": True,
        "speech_final": True,
        "duration": 1.2,
    }
    await stt._handle_results_message(chunk2)

    # Must finalize exactly once with accumulated text
    assert len(finals) == 1
    assert finals[0][1] == "rahul @gmail.com"


@pytest.mark.anyio
async def test_deepgram_single_finalization_prevents_duplicate_callbacks():
    """Verify that speech_final followed by late UtteranceEnd or duplicate frame calls on_final exactly ONCE."""
    finals = []
    async def mock_on_final(utt_id, text, metrics):
        finals.append((utt_id, text))

    stt = DeepgramStreamingSession(
        call_id="test_single_call",
        on_final=mock_on_final,
    )
    stt._running = True
    stt._connected = True

    # speech_final frame arrives
    chunk = {
        "channel": {"alternatives": [{"transcript": "I want an appointment", "confidence": 0.96}]},
        "is_final": True,
        "speech_final": True,
        "duration": 1.5,
    }
    await stt._handle_results_message(chunk)
    assert len(finals) == 1

    # Duplicate speech_final arrives before next speech
    await stt._handle_results_message(chunk)
    assert len(finals) == 1

    # Late UtteranceEnd arrives
    await stt._handle_utterance_end({})
    assert len(finals) == 1


def test_slot_repetition_suppression():
    """Verify system does not re-read full slot list if already presented on this date unless requested."""
    service = BookingStateService()
    session = service.get_session("test_slot_rep_session")
    session.reset()

    session.doctor = "Dr. Smith"
    session.date = "2026-10-15"
    session.offered_slots = ["10:00 AM", "11:30 AM", "02:00 PM"]
    session._slots_presented_for = ("Dr. Smith", "2026-10-15")
    session.state = BookingState.WAITING_FOR_TIME
    session.last_asked_field = "time"

    # Ambiguous customer response
    resp = service.process_booking_turn("Hello", session)
    # Must NOT re-read "10:00 AM, 11:30 AM, and 02:00 PM"
    assert "10:00 am, 11:30 am" not in resp.lower()
    assert "which of those times" in resp.lower()


@pytest.mark.anyio
async def test_interim_cancels_fallback_debounce_so_speech_is_not_cut_off():
    """
    Test: Middle confirmed chunk 'What' starts debounce timer.
    Subsequent interim words 'are the clinic timings' must CANCEL the debounce timer,
    preventing premature cut-off as 'What'.
    """
    import asyncio
    finals = []
    async def mock_on_final(utt_id, text, metrics):
        finals.append((utt_id, text))

    stt = DeepgramStreamingSession(
        call_id="test_debounce_cancel_call",
        on_final=mock_on_final,
    )
    stt._running = True
    stt._connected = True

    # 1. Customer says "What" -> middle chunk
    await stt._handle_results_message({
        "channel": {"alternatives": [{"transcript": "What", "confidence": 0.95}]},
        "is_final": True,
        "speech_final": False,
        "duration": 0.5,
    })
    assert stt._debounce_task is not None
    assert not stt._debounce_task.done()

    # 2. Customer continues speaking: interim results arrive
    await stt._handle_results_message({
        "channel": {"alternatives": [{"transcript": "are the clinic timings", "confidence": 0.92}]},
        "is_final": False,
        "speech_final": False,
        "duration": 1.1,
    })

    # Debounce task must be cancelled by the new speech
    assert stt._debounce_task is None or stt._debounce_task.cancelled() or stt._debounce_task.done()
    assert len(finals) == 0  # NOT prematurely finalized as "What"!

    # 3. Final chunk arrives: "are the clinic timings" with speech_final=True
    await stt._handle_results_message({
        "channel": {"alternatives": [{"transcript": "are the clinic timings", "confidence": 0.94}]},
        "is_final": True,
        "speech_final": True,
        "duration": 1.6,
    })

    # Complete sentence finalized together
    assert len(finals) == 1
    assert finals[0][1] == "What are the clinic timings"


@pytest.mark.anyio
async def test_finalized_utterance_retires_utt_id_and_subsequent_speech_gets_new_id():
    """
    Test: Utterance 1 finalizes under utt_100.
    Trailing silence/empty events do not reuse or mutate utt_100.
    Subsequent speech 'Hello?' gets a new ID (utt_101) and finalizes independently.
    """
    finals = []
    async def mock_on_final(utt_id, text, metrics):
        finals.append((utt_id, text))

    stt = DeepgramStreamingSession(
        call_id="test_utt_retirement_call",
        on_final=mock_on_final,
    )
    stt._running = True
    stt._connected = True

    # Utterance 1
    await stt._handle_results_message({
        "channel": {"alternatives": [{"transcript": "What are your hours", "confidence": 0.95}]},
        "is_final": True,
        "speech_final": True,
        "duration": 1.2,
    })
    assert len(finals) == 1
    first_utt_id, first_text = finals[0]
    assert first_text == "What are your hours"

    # Trailing empty speech_final frame arrives
    await stt._handle_results_message({
        "channel": {"alternatives": [{"transcript": "", "confidence": 0.0}]},
        "is_final": True,
        "speech_final": True,
        "duration": 0.1,
    })
    # Must NOT add another final callback
    assert len(finals) == 1

    # AI finished speaking, reset_turn is called
    stt.reset_turn()

    # Customer speaks new utterance "Hello?"
    await stt._handle_results_message({
        "channel": {"alternatives": [{"transcript": "Hello?", "confidence": 0.93}]},
        "is_final": True,
        "speech_final": True,
        "duration": 0.6,
    })

    assert len(finals) == 2
    second_utt_id, second_text = finals[1]
    assert second_text == "Hello?"
    # Utterance IDs must be distinct!
    assert first_utt_id != second_utt_id


def test_echo_detection_identifies_ai_substring_while_allowing_customer_barge_in():
    """
    Test: While AI is speaking, verbatim sub-phrases of AI speech are detected as echo,
    while new caller words ('excuse me', 'wait') are allowed through as customer barge-in.
    """
    from src.services.sip_call_handler import SipCallSession

    session = SipCallSession(
        caller_phone="+1234567890",
        audio_send_callback=lambda x: None,
    )
    session._ai_speaking = True
    ai_sentence = "I apologize, but that specific information is not available in our active records."
    session._recent_ai_texts.append(ai_sentence)

    # 1. Microphone picks up sub-phrase of AI's voice
    assert session._is_ai_echo("I apologize") is True
    assert session._is_ai_echo("specific information is not available") is True
    assert session._is_ai_echo("in our active records") is True

    # 2. Caller genuinely interrupts with different words
    assert session._is_ai_echo("Excuse me, do you have doctors?") is False
    assert session._is_ai_echo("Wait a second") is False
    assert session._is_ai_echo("Hello?") is False


def test_rag_retrieval_auto_syncs_and_retrieves_chunks_for_complete_question():
    """
    Test: RAG auto-syncs and retrieves non-empty relevant context for the complete question.
    """
    from src.services.rag_service import rag_service
    from src.config.database import SessionLocal
    from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus

    db = SessionLocal()
    try:
        doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == 9999).first()
        if not doc:
            doc = KnowledgeDocument(
                id=9999,
                organization_id=1,
                title="Hospital Information",
                content="# HOSPITAL INFORMATION\nVisiting hours for general wards are 04:00 PM to 07:00 PM daily.",
                ai_usage="Active",
                status=KnowledgeDocStatus.INDEXED
            )
            db.add(doc)
            db.commit()

        ctx = rag_service.retrieve_context("What are the hospital visiting hours?", organization_id=1, document_id="9999")
        assert ctx is not None
        assert len(ctx) > 0
        assert "visiting hours" in ctx.lower()
    finally:
        db.query(KnowledgeDocument).filter(KnowledgeDocument.id == 9999).delete()
        db.commit()
        db.close()



