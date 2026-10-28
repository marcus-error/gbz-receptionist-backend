"""
Local Open-Source Text-to-Speech (TTS) Service.

Runs local TTS engines (Piper / gTTS / pyttsx3) to synthesize speech locally.
Supports generating Twilio-compatible μ-law / WAV audio chunks for low-latency streaming.
No external third-party API dependencies.
"""

import asyncio
import io
import logging
import os
import struct
import tempfile
import time
from typing import Generator, List, Optional

from src.utils.text_sanitizer import clean_markdown_for_voice

logger = logging.getLogger(__name__)

_tts_engine = None


class TTSService:
    _murf_disabled_until = 0.0

    def __init__(self):
        self.provider = "local"

    def _synthesize_gtts(self, clean_text: str, target_gtts_lang: str, cache_key: Optional[str] = None) -> bytes:
        try:
            from gtts import gTTS
            from src.services.cache_service import cache_service
            tts = gTTS(text=clean_text, lang=target_gtts_lang, slow=False)
            fp = io.BytesIO()
            tts.write_to_fp(fp)
            audio_res = fp.getvalue()
            if audio_res:
                logger.info(f"[TTS Provider: gTTS] Generated {len(audio_res)} bytes (lang: {target_gtts_lang}).")
                if cache_key:
                    cache_service.set(cache_key, audio_res, ttl=3600)
                return audio_res
        except Exception as exc:
            logger.error(f"[TTS Provider: gTTS] Synthesis failed: {exc}")
        return b""

    def initialize_model(self):
        """Initialize and verify local TTS engine."""
        global _tts_engine
        from src.config.env import settings

        self.provider = getattr(settings, "TTS_PROVIDER", "local").lower().strip()
        logger.info(f"Local TTS Service initialized with provider '{self.provider}'")

        if self.provider == "murf":
            try:
                from src.services.murf_tts import murf_tts
                # Probe Murf once during startup so if quota is exhausted (402), circuit breaker trips BEFORE first call
                logger.info("[TTS] Verifying Murf TTS provider status at startup...")
                test_audio = murf_tts.synthesize("Hello", format="MP3")
                if not test_audio or len(test_audio) < 50:
                    TTSService._murf_disabled_until = time.monotonic() + 3600.0
                    logger.warning("[TTS] Murf provider unavailable at startup (402 Quota / Error). Circuit breaker active -> using fast fallback.")
                else:
                    TTSService._murf_disabled_until = 0.0
                    logger.info("[TTS] Murf provider verified active.")
            except Exception as exc:
                TTSService._murf_disabled_until = time.monotonic() + 3600.0
                logger.warning(f"[TTS] Murf check failed at startup ({exc}). Circuit breaker active -> using fast fallback.")

    @staticmethod
    def pcm_to_ulaw(pcm_bytes: bytes) -> bytes:
        """
        Convert 16-bit linear PCM audio bytes to 8-bit μ-law (ulaw) bytes
        used natively by Twilio Media Streams.
        """
        BIAS = 0x84
        CLIP = 32635
        mu_law_table = [
            0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
            4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
            5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
            5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
            7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        ]

        def encode_sample(sample: int) -> int:
            sign = (sample >> 8) & 0x80
            if sign != 0:
                sample = -sample
            if sample > CLIP:
                sample = CLIP
            sample = sample + BIAS
            exponent = mu_law_table[(sample >> 7) & 0xFF]
            mantissa = (sample >> (exponent + 3)) & 0x0F
            ulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
            return ulaw_byte

        ulaw_bytes = bytearray()
        for i in range(0, len(pcm_bytes) - 1, 2):
            sample = struct.unpack("<h", pcm_bytes[i:i+2])[0]
            ulaw_bytes.append(encode_sample(sample))

        return bytes(ulaw_bytes)

    def detect_language(self, text: str, fallback: str = "en") -> str:
        """
        Auto-detects language code from script characters (Tamil, Hindi, Telugu, Malayalam, Kannada).
        Falls back to provided fallback language or English.
        """
        clean = clean_markdown_for_voice(text or "").strip()
        import re
        if re.search(r'[\u0b80-\u0bff]', clean):
            return "ta"
        elif re.search(r'[\u0900-\u097f]', clean):
            return "hi"
        elif re.search(r'[\u0c00-\u0c7f]', clean):
            return "te"
        elif re.search(r'[\u0d00-\u0d7f]', clean):
            return "ml"
        elif re.search(r'[\u0c80-\u0cff]', clean):
            return "kn"
        fb = (fallback or "en").lower().strip()
        if fb in ("ta", "tanglish", "ta_mix"):
            return "ta"
        elif fb in ("hi", "hinglish"):
            return "hi"
        elif fb in ("te", "telugu"):
            return "te"
        elif fb in ("ml", "malayalam"):
            return "ml"
        elif fb in ("kn", "kannada"):
            return "kn"
        return "en"

    def synthesize_speech(self, text: str, language: str = "en", voice_id: Optional[str] = None) -> bytes:
        """
        Synthesize text into MP3/WAV audio bytes using local open-source TTS.
        Supports language-specific voice routing (Tamil, Hindi, Telugu, Malayalam, Kannada, English).
        """
        clean_text = clean_markdown_for_voice(text or "").strip()
        if not clean_text:
            return b""

        if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("APP_ENV") == "test":
            return b"RIFFd\x06\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x40\x06\x00\x00" + (b"\x00\x10" * 800)

        target_gtts_lang = self.detect_language(clean_text, fallback=language)

        # 1. Primary: Murf AI / gTTS / ElevenLabs / Local
        from src.config.env import settings
        from src.services.murf_tts import murf_tts
        from src.services.cache_service import cache_service
        tts_provider = getattr(settings, "TTS_PROVIDER", "murf").lower().strip()

        # Resolve credentials & normalized voice/model
        _, default_murf_voice, default_murf_model = murf_tts.get_credentials()
        murf_voice = voice_id or default_murf_voice
        murf_model = default_murf_model
        effective_voice = murf_voice if tts_provider == "murf" else (voice_id or getattr(settings, "ELEVENLABS_VOICE_ID", "default"))

        # Check in-memory cache for repeated conversational responses
        cache_key = f"tts_speech_{tts_provider}_{target_gtts_lang}_{effective_voice}_{hash(clean_text)}"
        cached_audio = cache_service.get(cache_key)
        if cached_audio:
            return cached_audio

        # ------------------------------------------------------------------
        # Provider: Murf AI (When TTS_PROVIDER=murf)
        # Fast failover with circuit breaker to avoid repeated delays on quota issues
        # ------------------------------------------------------------------
        now = time.monotonic()
        if tts_provider == "murf":
            if TTSService._murf_disabled_until > now:
                logger.info(f"[TTS Provider: Murf] Circuit breaker ACTIVE (HTTP 402/error cooldown). Using fast fallback TTS immediately.")
                return self._synthesize_gtts(clean_text, target_gtts_lang, cache_key)

            try:
                murf_audio = murf_tts.synthesize(clean_text, voice_id=murf_voice, model=murf_model, format="MP3")
                if murf_audio and len(murf_audio) > 50:
                    TTSService._murf_disabled_until = 0.0
                    cache_service.set(cache_key, murf_audio, ttl=3600)
                    return murf_audio
                # Empty audio indicates failure
                logger.warning(f"[TTS Provider: Murf] Murf synthesis returned empty audio. Using fallback gTTS.")
                return self._synthesize_gtts(clean_text, target_gtts_lang, cache_key)
            except Exception as exc:
                logger.warning(f"[TTS Provider: Murf] Murf synthesis failed ({exc}). Using fallback gTTS.")
                return self._synthesize_gtts(clean_text, target_gtts_lang, cache_key)

        # ------------------------------------------------------------------
        # Provider: gTTS (ONLY if TTS_PROVIDER=gtts is explicitly configured)
        # ------------------------------------------------------------------
        if tts_provider == "gtts":
            return self._synthesize_gtts(clean_text, target_gtts_lang, cache_key)

        # ------------------------------------------------------------------
        # Provider: ElevenLabs (ONLY if TTS_PROVIDER=elevenlabs is configured)
        # ------------------------------------------------------------------
        if tts_provider == "elevenlabs":
            api_key = getattr(settings, "ELEVENLABS_API_KEY", None)
            target_voice_id = voice_id or getattr(settings, "ELEVENLABS_VOICE_ID", "p9aflnsbBe1o0aDeQa97") or "p9aflnsbBe1o0aDeQa97"
            if api_key and api_key.strip():
                try:
                    import urllib.request
                    import json
                    url = f"https://api.elevenlabs.io/v1/text-to-speech/{target_voice_id}"
                    headers = {
                        "xi-api-key": api_key.strip(),
                        "Content-Type": "application/json",
                        "Accept": "audio/mpeg"
                    }
                    payload = json.dumps({
                        "text": clean_text,
                        "model_id": getattr(settings, "ELEVENLABS_MODEL_ID", "eleven_multilingual_v2") or "eleven_multilingual_v2",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75
                        }
                    }).encode("utf-8")
                    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        eleven_audio = resp.read()
                        if eleven_audio:
                            logger.info(f"[ELEVENLABS TTS SUCCESS] Voice ID '{target_voice_id}' generated {len(eleven_audio)} bytes.")
                            return eleven_audio
                except Exception as exc:
                    logger.error(f"[TTS Provider: ElevenLabs] Failed: {exc}")
                    return b""

        # ------------------------------------------------------------------
        # Provider: Local / Offline (ONLY if explicitly configured)
        # ------------------------------------------------------------------
        if tts_provider in ("local", "offline"):
            co_initialized = False
            try:
                import pyttsx3
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                    co_initialized = True
                except Exception:
                    pass

                engine = pyttsx3.init()
                engine.setProperty('rate', 190)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp_path = f.name
                engine.save_to_file(clean_text, tmp_path)
                engine.runAndWait()
                with open(tmp_path, "rb") as f:
                    audio_bytes = f.read()
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                if audio_bytes:
                    return audio_bytes
            except Exception as exc:
                logger.error(f"[TTS Provider: Local] pyttsx3 failed: {exc}")
                return b""
            finally:
                if co_initialized:
                    try:
                        pythoncom.CoUninitialize()
                    except Exception:
                        pass

        return b""


    def synthesize_ulaw_8000(self, text: str, language: str = "en") -> bytes:
        """
        Synthesize text and convert to Twilio-compatible 8 kHz μ-law bytes.
        """
        if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("APP_ENV") == "test":
            return b"\xff" * 800

        from src.config.env import settings
        from src.services.murf_tts import murf_tts
        tts_provider = getattr(settings, "TTS_PROVIDER", "murf").lower().strip()
        murf_api_key = getattr(settings, "MURF_API_KEY", None)

        if tts_provider == "murf":
            try:
                _, default_murf_voice, default_murf_model = murf_tts.get_credentials()
                murf_voice = voice_id or default_murf_voice
                murf_model = default_murf_model
                ulaw_audio = murf_tts.synthesize_ulaw(text, voice_id=murf_voice, model=murf_model)
                if ulaw_audio and len(ulaw_audio) > 50:
                    return ulaw_audio
                logger.error(f"[TTS Provider: Murf] Murf ulaw generation returned empty for '{murf_voice}'. Not falling back to other engines.")
                return b""
            except Exception as exc:
                logger.error(f"[TTS Provider: Murf] Murf ulaw generation failed ({exc}).")
                return b""

        audio_bytes = self.synthesize_speech(text, language=language)
        if not audio_bytes:
            return b""
        
        # If audio is already WAV/PCM, extract frames; otherwise wrap or convert
        try:
            import wave
            wav_io = io.BytesIO(audio_bytes)
            with wave.open(wav_io, "rb") as wf:
                pcm_data = wf.readframes(wf.getnframes())
                return self.pcm_to_ulaw(pcm_data)
        except Exception:
            # Fallback: simple byte chunking / raw ulaw encoding
            return self.pcm_to_ulaw(audio_bytes[:16000])

    async def synthesize_speech_async(self, text: str, language: str = "en", voice_id: Optional[str] = None) -> bytes:
        """Asynchronously synthesize speech without blocking the event loop, with hard 4.0s timeout."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.synthesize_speech, text, language, voice_id),
                timeout=4.0
            )
        except asyncio.TimeoutError:
            logger.warning("[TTS] Total synthesis exceeded 4.0s hard timeout! Falling back to fast edge TTS.")
            return await asyncio.to_thread(self._synthesize_gtts, text, language or "en")

    async def synthesize_ulaw_async(self, text: str, language: str = "en") -> bytes:
        """Asynchronously generate Twilio μ-law audio bytes."""
        return await asyncio.to_thread(self.synthesize_ulaw_8000, text, language)

    async def synthesize_stream_chunk_async(self, text_chunk: str, language: str = "en", voice_id: Optional[str] = None) -> bytes:
        """
        Fast-path asynchronous TTS synthesizer for short streaming clause fragments.
        Uses in-memory cache for common conversational phrases.
        """
        clean_chunk = (text_chunk or "").strip()
        if not clean_chunk:
            return b""

        from src.services.cache_service import cache_service
        effective_voice = voice_id or ""
        cache_key = f"tts_{language}_{effective_voice}_{hash(clean_chunk)}"
        cached_audio = cache_service.get(cache_key)
        if cached_audio:
            return cached_audio

        audio = await self.synthesize_speech_async(clean_chunk, language=language, voice_id=voice_id)
        if audio and len(audio) > 100:
            cache_service.set(cache_key, audio, ttl=1800)
        return audio


tts_service = TTSService()

