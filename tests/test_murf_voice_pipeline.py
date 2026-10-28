import os
import sys
import pytest

sys.path.insert(0, os.path.abspath("."))
from src.services.murf_tts import murf_tts, parse_voice_and_model
from src.services.tts_service import tts_service, TTSService
from src.services.sip_call_handler import convert_tts_to_pcmu_and_pcm16


def test_murf_voice_normalization_and_json():
    # 1. JSON parsing and alias normalization
    raw_json = '{"voiceId":"Anisha","model":"Falcon"}'
    v1, m1 = parse_voice_and_model(raw_json, "GEN2")
    assert v1 == "en-IN-isha"
    assert m1 == "GEN2"

    # 2. Friendly alias normalization
    v2, m2 = parse_voice_and_model("Priya", "Falcon")
    assert v2 == "en-IN-priya"
    assert m2 == "GEN2"

    v3, m3 = parse_voice_and_model("Natalie", "default")
    assert v3 == "en-US-natalie"
    assert m3 == "GEN2"


def test_murf_live_synthesis_and_telephony_conversion():
    # Clear circuit breaker
    TTSService._murf_disabled_until = 0.0

    # Ensure mock is bypassed for live verification
    os.environ.pop("PYTEST_CURRENT_TEST", None)

    # Synthesis via murf_tts directly
    audio_stream = murf_tts.synthesize("Welcome to our clinic. How may I help you today?")
    assert len(audio_stream) > 1000, f"Expected audio > 1000 bytes, got {len(audio_stream)}"

    # Synthesis via tts_service
    audio_service = tts_service.synthesize_speech("Welcome to our clinic. How may I help you today?")
    assert len(audio_service) > 1000, f"Expected audio > 1000 bytes, got {len(audio_service)}"

    # Circuit breaker remains healthy (0.0)
    assert TTSService._murf_disabled_until == 0.0

    # Telephony 8kHz PCMU and PCM16 conversion
    pcmu, pcm16 = convert_tts_to_pcmu_and_pcm16(audio_service, "murf")
    assert len(pcmu) > 1000
    assert len(pcm16) > 2000
    assert len(pcm16) == len(pcmu) * 2  # 16-bit is 2 bytes per 8-bit sample
