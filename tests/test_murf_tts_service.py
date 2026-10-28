"""
Unit tests for Murf AI Text-to-Speech (TTS) Service Integration.
"""

import pytest
import os
from unittest.mock import patch, MagicMock
from src.services.murf_tts import murf_tts, MurfTTSService
from src.services.tts_service import tts_service
from src.config.env import settings


def test_murf_tts_configuration():
    """Verify Murf AI default voice, model, and environment credentials."""
    api_key, voice_id, model = murf_tts.get_credentials()
    assert voice_id == "en-US-natalie"
    assert model == "GEN2"
    assert api_key is not None or os.getenv("MURF_API_KEY") is None or os.getenv("MURF_API_KEY") == api_key


def test_murf_tts_synthesis_mock_in_test_env():
    """Verify Murf TTS returns valid audio bytes in test environment."""
    audio_bytes = murf_tts.synthesize("Hello, welcome to Call Zenza AI Receptionist.", voice_id="en-US-natalie", model="GEN2")
    assert isinstance(audio_bytes, bytes)
    assert len(audio_bytes) > 0


def test_murf_tts_ulaw_conversion():
    """Verify Murf TTS μ-law (ulaw) audio generation for Twilio compatibility."""
    ulaw_bytes = murf_tts.synthesize_ulaw("Thank you for calling our receptionist desk.")
    assert isinstance(ulaw_bytes, bytes)
    assert len(ulaw_bytes) > 0


def test_murf_tts_missing_api_key_safe_fallback():
    """Verify missing API key logs safely without crashing or exposing secrets."""
    service = MurfTTSService()
    with patch.object(service, "get_credentials", return_value=(None, "en-US-natalie", "GEN2")):
        audio = service.synthesize("Test prompt without API key")
        assert audio == b""


def test_tts_service_routes_to_murf():
    """Verify central TTSService delegates synthesis to Murf AI when provider is murf."""
    audio = tts_service.synthesize_speech("Hello, how can I assist you today?")
    assert isinstance(audio, bytes)
    assert len(audio) > 0


def test_clean_markdown_for_voice():
    """Verify clean_markdown_for_voice removes asterisks, double dashes, parentheses, and extra spaces."""
    from src.utils.text_sanitizer import clean_markdown_for_voice
    sample = "வணக்கம்! உங்களுக்கு **அப்பாயிண்ட்மெண்ட்** பண்ண... (எ.கா: **CallZenza AI** சேவை -- ஆட்டோமேஷன் சேகரிப்பு).  அல்லது,  எந்த  தேவையாளர்  காலம் உங்களுக்கு ஏற்றது?"
    cleaned = clean_markdown_for_voice(sample)
    assert "**" not in cleaned
    assert "--" not in cleaned
    assert "(" not in cleaned
    assert ")" not in cleaned
    assert "  " not in cleaned
    assert "அப்பாயிண்ட்மெண்ட்" in cleaned
    assert "CallZenza AI" in cleaned
