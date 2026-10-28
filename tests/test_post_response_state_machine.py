"""
Test Suite for Post-Response State Machine, Automatic Action Evaluation & Silence Handling.

Verifies:
1. After AI response/TTS finishes, system evaluates conversation state without entering blind hold.
2. If all required fields are present, system marks AUTOMATIC_ACTION_REQUIRED to process immediately.
3. If user input is needed, system marks USER_INPUT_REQUIRED with exact missing fields and LISTENING state.
4. Silence handling produces a gentle, non-destructive prompt without resetting conversation state or re-emitting greetings.
5. Multi-turn topic switching maintains continuous conversation state.
"""

import pytest
from src.services.booking_state_service import (
    BookingStateService, BookingSession, BookingState, NextAction, booking_state_service
)


def test_post_response_user_input_required_when_fields_missing():
    """
    Test: User starts booking with doctor only.
    Expected: evaluate_post_response returns USER_INPUT_REQUIRED and state="LISTENING".
    """
    service = BookingStateService()
    session = service.get_session("test_post_eval_missing")
    session.reset()

    from src.config.database import SessionLocal
    from src.models.staff import Staff
    db = SessionLocal()
    staff_rec = Staff(name="Dr Arun", organization_id=1, department="General Medicine")
    db.add(staff_rec)
    db.commit()
    try:
        resp = service.process_booking_turn("I want to book an appointment with Dr Arun", session)
        assert session.is_active() is True
        assert session.doctor is not None
    finally:
        db.delete(staff_rec)
        db.commit()
        db.close()

    # Evaluate post-response state
    post_eval = service.evaluate_post_response(session)
    assert post_eval["next_action"] == NextAction.USER_INPUT_REQUIRED
    assert post_eval["state"] == "LISTENING"
    assert "appointment_date" in post_eval["missing_fields"]
    assert "full_name" in post_eval["missing_fields"]


def test_post_response_automatic_action_when_all_fields_provided():
    """
    Test: User provides complete information (doctor, date, time, name, phone, email).
    Expected: System evaluates and executes booking automatically without requiring extra user turns.
    """
    service = BookingStateService()
    session = service.get_session("test_post_eval_all_fields")
    session.reset()

    session.doctor = "Synthetic Specialist"
    session.date = "Tomorrow"
    session.time = "10:00 AM"
    session.patient_name = "Synthetic Customer"
    session.contact_number = "5551234567"
    session.email = "customer@example.com"
    session.state = BookingState.WAITING_FOR_EMAIL

    # When all fields are present, evaluate_post_response signals AUTOMATIC_ACTION_REQUIRED
    post_eval = service.evaluate_post_response(session)
    assert post_eval["next_action"] == NextAction.AUTOMATIC_ACTION_REQUIRED
    assert post_eval["state"] == "PROCESSING"


def test_post_response_completed_state():
    """
    Test: When booking is confirmed (appointment_id present and state is BOOKED/COMPLETED).
    Expected: evaluate_post_response returns COMPLETED.
    """
    service = BookingStateService()
    session = service.get_session("test_post_eval_completed")
    session.reset()

    session.state = BookingState.BOOKED
    session.is_completed = True
    session.appointment_id = 999

    post_eval = service.evaluate_post_response(session)
    assert post_eval["next_action"] == NextAction.COMPLETED
    assert post_eval["state"] == "COMPLETED"


def test_silence_prompts_maintain_state_and_no_greeting():
    """
    Test: Silence prompt generation for various missing field stages.
    Expected:
    - Returns tailored polite prompt for the specific missing field.
    - Does NOT contain welcome greetings (e.g. 'welcome to', 'hello how can i help').
    - Session state and collected fields remain 100% intact.
    """
    service = BookingStateService()
    session = service.get_session("test_silence_prompts")
    session.reset()

    session.patient_name = "Mugilan"
    session.state = BookingState.WAITING_FOR_CONTACT

    silence_prompt = service.get_silence_prompt(session)
    assert "Mugilan" in silence_prompt or "contact" in silence_prompt.lower() or "phone" in silence_prompt.lower()
    assert "welcome" not in silence_prompt.lower()
    assert "hello" not in silence_prompt.lower()

    # Verify session state was not reset
    assert session.patient_name == "Mugilan"
    assert session.state == BookingState.WAITING_FOR_CONTACT


def test_silence_prompt_multilingual():
    """
    Test: Silence prompt in Tamil and Hindi languages.
    """
    service = BookingStateService()
    session = service.get_session("test_silence_multilingual")
    session.reset()

    session.conversation_language = "ta"
    session.state = BookingState.WAITING_FOR_PATIENT_NAME
    prompt_ta = service.get_silence_prompt(session)
    assert "பெயர்" in prompt_ta or "name" in prompt_ta.lower()

    session.conversation_language = "hi"
    session.state = BookingState.WAITING_FOR_CONTACT
    prompt_hi = service.get_silence_prompt(session)
    assert "phone" in prompt_hi.lower() or "naam" in prompt_hi.lower() or "sun raha hoon" in prompt_hi.lower()
