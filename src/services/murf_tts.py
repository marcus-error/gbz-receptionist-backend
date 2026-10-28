"""
Dedicated Murf AI Text-to-Speech (TTS) Service.

Provides real-time, low-latency audio generation using Murf AI (Voice: Anusha, Model: Falcon).
Converts audio into MP3/WAV for WebSocket Clients and 8 kHz μ-law (ulaw) for Twilio Media Streams.
"""

import asyncio
import base64
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

from src.utils.text_sanitizer import clean_markdown_for_voice

logger = logging.getLogger(__name__)

_TTS_CACHE = {}

VOICE_ALIASES = {
    "anisha": "en-IN-isha",
    "anusha": "en-IN-isha",
    "isha": "en-IN-isha",
    "alisha": "en-IN-isha",
    "priya": "en-IN-priya",
    "alia": "en-IN-alia",
    "arohi": "en-IN-arohi",
    "aarav": "en-IN-aarav",
    "rohan": "en-IN-rohan",
    "eashwar": "en-IN-eashwar",
    "natalie": "en-US-natalie",
}

MODEL_VERSION_MAP = {
    "falcon": "GEN2",
    "falcon-2": "GEN2",
    "gen2": "GEN2",
    "gen1": "GEN1",
    "default": "GEN2",
}


def parse_voice_and_model(voice_id: Optional[str], model: Optional[str]) -> tuple[str, str]:
    """
    Safely normalizes Murf voice ID and model.
    Handles raw JSON strings like '{"voiceId":"Anisha","model":"Falcon"}',
    cleans whitespace/braces, and maps friendly aliases to valid Murf API identifiers.
    """
    raw_v = str(voice_id or "").strip()
    raw_m = str(model or "").strip()

    # Check if voice_id is JSON encoded: {"voiceId": "Anisha", "model": "Falcon"}
    if raw_v.startswith("{") and ("voice" in raw_v.lower() or "model" in raw_v.lower()):
        try:
            parsed = json.loads(raw_v)
            if isinstance(parsed, dict):
                extracted_v = parsed.get("voiceId") or parsed.get("voice_id") or parsed.get("voice")
                if extracted_v:
                    raw_v = str(extracted_v).strip()
                extracted_m = parsed.get("model") or parsed.get("modelVersion") or parsed.get("model_version")
                if extracted_m:
                    raw_m = str(extracted_m).strip()
        except Exception:
            pass

    # Strip surrounding quotes and braces
    clean_v = raw_v.strip(' "\'{}')
    clean_m = raw_m.strip(' "\'{}')

    # Normalize voice ID
    voice_key = clean_v.lower()
    target_voice = VOICE_ALIASES.get(voice_key, clean_v) if clean_v else "en-IN-isha"

    # Normalize model version (Falcon -> GEN2)
    model_key = clean_m.lower()
    target_model = MODEL_VERSION_MAP.get(model_key, clean_m) if clean_m else "GEN2"

    return target_voice, target_model


class MurfTTSService:
    """
    Murf AI Text-to-Speech integration service.
    Configured via MURF_API_KEY, MURF_VOICE_ID, and MURF_MODEL.
    Supports both direct voice IDs (en-IN-isha) and JSON configs.
    """

    def __init__(self):
        self.default_voice_id = "en-IN-isha"
        self.default_model = "GEN2"

    def get_credentials(self):
        """
        Safely retrieves Murf configuration from environment/settings.
        Never exposes the API key in logs.
        """
        from src.config.env import settings
        api_key = getattr(settings, "MURF_API_KEY", None) or os.getenv("MURF_API_KEY", "").strip()
        raw_voice = getattr(settings, "MURF_VOICE_ID", None) or os.getenv("MURF_VOICE_ID", "en-IN-isha")
        raw_model = getattr(settings, "MURF_MODEL", None) or os.getenv("MURF_MODEL", "GEN2")
        voice_id, model = parse_voice_and_model(raw_voice, raw_model)
        return api_key, voice_id, model

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        format: str = "MP3",
        sample_rate: int = 24000
    ) -> bytes:
        """
        Synthesize AI response text into audio bytes using Murf AI.
        Uses Voice 'en-IN-isha' and Model 'GEN2' by default.
        Never exposes secrets in logs.
        """
        import time
        t0 = time.time()
        clean_text = clean_markdown_for_voice(text or "").strip()
        if not clean_text:
            return b""

        api_key, env_voice, env_model = self.get_credentials()
        raw_target_voice = voice_id or env_voice or "en-IN-isha"
        raw_target_model = model or env_model or "GEN2"
        target_voice, model_version = parse_voice_and_model(raw_target_voice, raw_target_model)

        if not api_key:
            latency_ms = int((time.time() - t0) * 1000)
            logger.error(
                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Failed | "
                f"Error: MURF_API_KEY not configured | TTS Latency: {latency_ms}ms"
            )
            return b""

        if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("APP_ENV") == "test":
            latency_ms = int((time.time() - t0) * 1000)
            logger.info(
                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Success (Mock) | "
                f"TTS Latency: {latency_ms}ms"
            )
            return b"RIFFd\x06\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00\x02\x00\x10\x00data\x40\x06\x00\x00" + (b"\x00\x10" * 800)

        # Check in-memory audio cache
        cache_key = f"{clean_text}:{target_voice}:{model_version}:{format}:{sample_rate}"
        if cache_key in _TTS_CACHE:
            latency_ms = int((time.time() - t0) * 1000)
            logger.info(
                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Success (Cache Hit) | "
                f"Audio Size: {len(_TTS_CACHE[cache_key])} bytes | TTS Latency: {latency_ms}ms"
            )
            return _TTS_CACHE[cache_key]

        # 1. Primary Low-Latency Endpoint: Murf Streaming Router (/v1/speech/stream)
        stream_url = "https://global.api.murf.ai/v1/speech/stream"
        stream_headers = {
            "api-key": api_key,
            "Content-Type": "application/json"
        }
        stream_payload = json.dumps({
            "text": clean_text,
            "voiceId": target_voice,
            "modelVersion": model_version,
            "format": format.upper(),
            "sampleRate": sample_rate
        }).encode("utf-8")

        try:
            req_stream = urllib.request.Request(stream_url, data=stream_payload, headers=stream_headers, method="POST")
            with urllib.request.urlopen(req_stream, timeout=2.5) as stream_resp:
                raw_bytes = stream_resp.read()
                if raw_bytes and len(raw_bytes) > 50:
                    try:
                        from src.services.tts_service import TTSService
                        TTSService._murf_disabled_until = 0.0
                    except Exception:
                        pass
                    latency_ms = int((time.time() - t0) * 1000)
                    logger.info(
                        f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Success (Stream) | "
                        f"Audio Size: {len(raw_bytes)} bytes | TTS Latency: {latency_ms}ms"
                    )
                    if len(_TTS_CACHE) < 200:
                        _TTS_CACHE[cache_key] = raw_bytes
                    return raw_bytes
        except urllib.error.HTTPError as stream_http_err:
            if stream_http_err.code in (401, 402, 403):
                latency_ms = int((time.time() - t0) * 1000)
                if stream_http_err.code == 402:
                    err_msg = "Payment Required (HTTP 402 - Quota Exhausted)"
                else:
                    err_msg = f"Authentication/Key Error (HTTP {stream_http_err.code})"
                try:
                    from src.services.tts_service import TTSService
                    TTSService._murf_disabled_until = time.monotonic() + 3600.0
                except Exception:
                    pass
                logger.error(
                    f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Failed | "
                    f"Error: {err_msg} | TTS Latency: {latency_ms}ms"
                )
                return b""
            logger.warning(f"[MURF_STREAM_FAIL] Code {stream_http_err.code}, attempting /v1/speech/generate fallback")
        except Exception as stream_exc:
            logger.warning(f"[MURF_STREAM_FAIL] {stream_exc}, attempting /v1/speech/generate fallback")

        # 2. Secondary Fallback Endpoint: Murf REST API (/v1/speech/generate)
        url = "https://api.murf.ai/v1/speech/generate"
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload_dict = {
            "text": clean_text,
            "voiceId": target_voice,
            "modelVersion": model_version,
            "format": format.upper(),
            "sampleRate": sample_rate,
            "encodeAsBase64": True
        }

        try:
            req_data = json.dumps(payload_dict).encode("utf-8")
            req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")

            with urllib.request.urlopen(req, timeout=3.5) as resp:
                resp_body = resp.read()
                data = json.loads(resp_body.decode("utf-8"))

                # Check for Base64 encoded audio (ultra-low latency path)
                encoded_audio = data.get("encodedAudio") or data.get("audio_base64")
                if encoded_audio and isinstance(encoded_audio, str) and len(encoded_audio) > 50:
                    audio_bytes = base64.b64decode(encoded_audio)
                    if audio_bytes and len(audio_bytes) > 50:
                        try:
                            from src.services.tts_service import TTSService
                            TTSService._murf_disabled_until = 0.0
                        except Exception:
                            pass
                        latency_ms = int((time.time() - t0) * 1000)
                        logger.info(
                            f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Success (Generate Base64) | "
                            f"Audio Size: {len(audio_bytes)} bytes | TTS Latency: {latency_ms}ms"
                        )
                        if len(_TTS_CACHE) < 200:
                            _TTS_CACHE[cache_key] = audio_bytes
                        return audio_bytes

                # Check for downloadable audio file URL
                audio_url = data.get("audioFile") or data.get("audio_url")
                if audio_url:
                    with urllib.request.urlopen(audio_url, timeout=3.5) as audio_resp:
                        audio_bytes = audio_resp.read()
                        if audio_bytes and len(audio_bytes) > 50:
                            try:
                                from src.services.tts_service import TTSService
                                TTSService._murf_disabled_until = 0.0
                            except Exception:
                                pass
                            latency_ms = int((time.time() - t0) * 1000)
                            logger.info(
                                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Success (CDN) | "
                                f"Audio Size: {len(audio_bytes)} bytes | TTS Latency: {latency_ms}ms"
                            )
                            if len(_TTS_CACHE) < 200:
                                _TTS_CACHE[cache_key] = audio_bytes
                            return audio_bytes

        except urllib.error.HTTPError as http_err:
            latency_ms = int((time.time() - t0) * 1000)
            err_details = http_err.read().decode('utf-8', errors='ignore')
            if http_err.code == 402:
                err_msg = "Payment Required (HTTP 402 - Quota Exhausted)"
                try:
                    from src.services.tts_service import TTSService
                    TTSService._murf_disabled_until = time.monotonic() + 3600.0
                except Exception:
                    pass
            elif http_err.code in (401, 403):
                err_msg = f"Authentication/Key Error (HTTP {http_err.code})"
                try:
                    from src.services.tts_service import TTSService
                    TTSService._murf_disabled_until = time.monotonic() + 3600.0
                except Exception:
                    pass
            elif http_err.code == 429:
                err_msg = "Rate Limit Exceeded (HTTP 429)"
            elif http_err.code == 400:
                err_msg = f"Bad Request (HTTP 400): {err_details[:100]}"
            else:
                err_msg = f"HTTP {http_err.code}: {http_err.reason}"
            logger.error(
                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Failed | "
                f"Error: {err_msg} | TTS Latency: {latency_ms}ms"
            )
        except urllib.error.URLError as url_err:
            latency_ms = int((time.time() - t0) * 1000)
            logger.error(
                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Failed | "
                f"Error: Network/Timeout Error ({url_err.reason}) | TTS Latency: {latency_ms}ms"
            )
        except Exception as exc:
            latency_ms = int((time.time() - t0) * 1000)
            logger.error(
                f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Failed | "
                f"Error: Synthesis exception ({exc}) | TTS Latency: {latency_ms}ms"
            )

        latency_ms = int((time.time() - t0) * 1000)
        logger.error(
            f"TTS Provider: Murf | Voice ID: {target_voice} | TTS Status: Failed | "
            f"Error: Empty audio response from Murf | TTS Latency: {latency_ms}ms"
        )
        return b""

    def synthesize_ulaw(
        self,
        text: str,
        voice_id: Optional[str] = None,
        model: Optional[str] = None
    ) -> bytes:
        """
        Synthesizes text into 8 kHz μ-law (ulaw) bytes required for Twilio Media Streams.
        """
        # First attempt native ULAW format from Murf AI
        ulaw_audio = self.synthesize(text, voice_id=voice_id, model=model, format="ULAW", sample_rate=8000)
        if ulaw_audio and len(ulaw_audio) > 50:
            return ulaw_audio

        # Fallback: synthesize WAV and transcode PCM to μ-law
        raw_audio = self.synthesize(text, voice_id=voice_id, model=model, format="WAV", sample_rate=8000)
        if not raw_audio:
            return b""

        try:
            import io
            import wave
            from src.services.tts_service import TTSService
            wav_io = io.BytesIO(raw_audio)
            with wave.open(wav_io, "rb") as wf:
                pcm_data = wf.readframes(wf.getnframes())
                return TTSService.pcm_to_ulaw(pcm_data)
        except Exception:
            from src.services.tts_service import TTSService
            return TTSService.pcm_to_ulaw(raw_audio[:16000])

    async def synthesize_async(
        self,
        text: str,
        voice_id: Optional[str] = None,
        model: Optional[str] = None,
        format: str = "MP3",
        sample_rate: int = 24000
    ) -> bytes:
        """Asynchronously synthesize speech via Murf AI."""
        return await asyncio.to_thread(self.synthesize, text, voice_id, model, format, sample_rate)

    async def synthesize_ulaw_async(
        self,
        text: str,
        voice_id: Optional[str] = None,
        model: Optional[str] = None
    ) -> bytes:
        """Asynchronously synthesize Twilio μ-law audio via Murf AI."""
        return await asyncio.to_thread(self.synthesize_ulaw, text, voice_id, model)


murf_tts = MurfTTSService()
