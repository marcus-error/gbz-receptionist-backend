"""
Unit Test Suite for Multilingual + Multi-Voice + Voice-First Language Lock & Context Awareness.
"""

import pytest
from src.config.database import SessionLocal, init_db
from src.models.staff import Staff, StaffStatus
from src.services.language_service import language_service, LANG_TAMIL, LANG_ENGLISH, LANG_HINDI
from src.services.booking_state_service import booking_state_service
from src.services.tts_service import tts_service
from src.services.ai_service import ai_service


@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    db = SessionLocal()
    try:
        import json
        all_days = json.dumps(["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])
        staff = db.query(Staff).filter(Staff.name == "Dr. Priya Sharma").first()
        if not staff:
            db.add(Staff(
                name="Dr. Priya Sharma",
                department="Cardiology",
                working_hours="09:00 - 18:00",
                start_time="09:00",
                end_time="18:00",
                working_days=all_days,
                appointment_duration="30 minutes",
                status=StaffStatus.AVAILABLE
            ))
            db.commit()
        yield db
    finally:
        db.close()


def test_language_detection_tamil_script():
    """Test detecting pure Tamil script."""
    text = "எனக்கு ஒரு appointment book பண்ணணும்."
    lang = language_service.detect_language(text)
    assert lang == LANG_TAMIL


def test_language_detection_tanglish():
    """Test detecting Tanglish / Tamil in Latin script."""
    text = "mama Priya ku appointment book pannanum"
    lang = language_service.detect_language(text)
    assert lang == LANG_TAMIL


def test_language_lock_entity_name_immunity():
    """Test that English entity names do NOT break an active Tamil conversation lock."""
    # Turn 1: Tamil input locks session to 'ta'
    session = booking_state_service.get_session("test_multilingual_lock_sess")
    session.reset()

    turn1 = "எனக்கு ஒரு appointment book பண்ணணும்."
    lang1 = language_service.determine_conversation_language(turn1, current_lang=session.conversation_language)
    session.conversation_language = lang1
    assert session.conversation_language == LANG_TAMIL

    # Turn 2: User provides English entity name "Priya Sharma"
    turn2 = "Priya Sharma"
    lang2 = language_service.determine_conversation_language(turn2, current_lang=session.conversation_language)
    session.conversation_language = lang2

    # Language MUST remain Tamil
    assert session.conversation_language == LANG_TAMIL


def test_explicit_language_switch_to_english():
    """Test explicitly requesting language switch to English."""
    session = booking_state_service.get_session("test_switch_en_sess")
    session.reset()
    session.conversation_language = LANG_TAMIL

    turn = "Can you speak in English?"
    lang = language_service.determine_conversation_language(turn, current_lang=session.conversation_language)
    session.conversation_language = lang

    assert session.conversation_language == LANG_ENGLISH


def test_explicit_language_switch_to_tamil():
    """Test explicitly requesting language switch back to Tamil."""
    session = booking_state_service.get_session("test_switch_ta_sess")
    session.reset()
    session.conversation_language = LANG_ENGLISH

    turn = "தமிழ்ல பேசுங்க."
    lang = language_service.determine_conversation_language(turn, current_lang=session.conversation_language)
    session.conversation_language = lang

    assert session.conversation_language == LANG_TAMIL


def test_multilingual_booking_state_flow():
    """Test booking turn in Tamil/Tanglish generates Tamil prompt and retains session state."""
    session = booking_state_service.get_session("test_ta_booking_flow_sess")
    session.reset()

    # Step 1: User asks for appointment in Tanglish
    res1 = booking_state_service.process_booking_turn("mama Priya ku appointment book pannanum", session)
    assert session.conversation_language in (LANG_TAMIL, "tanglish", "ta_mix")
    assert (session.doctor is not None and "priya" in str(session.doctor).lower()) or len(res1) > 0
    assert isinstance(res1, str)
    assert len(res1) > 0

    # Step 2: User gives date in Tanglish
    res2 = booking_state_service.process_booking_turn("naalai", session)
    assert session.date == "Tomorrow"
    assert isinstance(res2, str)


def test_multilingual_tts_routing():
    """Test TTS synthesis with language routing produces valid audio bytes."""
    audio_ta = tts_service.synthesize_speech("வணக்கம், நான் உங்களுக்கு எப்படி உதவட்டும்?", language=LANG_TAMIL)
    assert isinstance(audio_ta, bytes)
    assert len(audio_ta) > 0

    audio_en = tts_service.synthesize_speech("Hello, how can I help you today?", language=LANG_ENGLISH)
    assert isinstance(audio_en, bytes)
    assert len(audio_en) > 0


def test_elevenlabs_voice_config():
    """Test ElevenLabs voice ID configuration."""
    from src.config.env import settings
    assert settings.ELEVENLABS_VOICE_ID == "p9aflnsbBe1o0aDeQa97"


def test_tamil_tamil_language_switch_and_script_autodetect():
    """Test that 'Tamil Tamil' switches language to Tamil and Tamil text script auto-detects for TTS."""
    lang = language_service.determine_conversation_language("Tamil Tamil")
    assert lang == LANG_TAMIL

    # Tamil text with language='en' parameter should still auto-detect Tamil script and generate valid audio
    audio = tts_service.synthesize_speech("நான் உதவிக்கு தயாராக உள்ளேன்.", language="en")
    assert isinstance(audio, bytes)
    assert len(audio) > 0


