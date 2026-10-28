"""
SIP Call Handler - real-time voice pipeline for SIP calls.

Pipeline:
  RTP RX (pyVoIP) -> feed_audio(pcm8_u8)
    -> recording branch: pcm8 -> pcm16 -> _recording_pcm_frames (WAV saved on hangup)
    -> STT branch: raw u8 -> _audio_queue (separate buffer, NEVER shared with recording)
  Task A: _audio_drain_loop
    -> Pre-speech ring buffer (100ms) + Energy VAD + AI echo suppression
    -> Utterance buffering -> _stt_queue on silence/max-length
  Task B: _ai_processing_loop
    -> pcm8u_to_wav16 (u8 8kHz -> s16 16kHz WAV)
    -> Whisper STT -> hallucination filter -> duplicate filter
    -> Emit CUSTOMER_FINAL WebSocket event
    -> NLP intent -> Booking state machine / RAG / LLM
    -> TTS -> convert_tts_to_pcm8u (PCM16 -> ulaw PCMU) -> pyVoIP write_audio
    -> Emit AI_RESPONSE WebSocket event
    -> Barge-in: cancel TTS on customer speech

RTP TX codec path: TTS(WAV/MP3) -> PCM16 8kHz -> audioop.lin2ulaw -> PCMU -> pyVoIP
RTP RX codec path: pyVoIP(PCMU decode) -> u8 8kHz -> bias(-128) -> s16 -> s16 16kHz -> WAV -> Whisper
"""

import asyncio
import audioop
import base64
import copy
import io
import logging
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Callable

from src.config.database import SessionLocal
from src.services.deepgram_stt_service import DeepgramStreamingSession
from src.services.stt_service import stt_service
from src.services.tts_service import tts_service
from src.services.ai_service import ai_service
from src.services.nlp_service import nlp_service
from src.services.emotion_service import emotion_service
from src.services.rag_service import rag_service
from src.services.booking_state_service import booking_state_service
from src.services.call_service import CallService
from src.services.language_service import language_service
from src.models.call_log import CallType
from src.utils.text_sanitizer import clean_markdown_for_voice

logger = logging.getLogger(__name__)

# ─── VAD Parameters ──────────────────────────────────────────────────────────
_FRAME_SIZE_BYTES      = 160   # 20ms @ 8kHz
from src.config.env import settings
_SILENCE_TIMEOUT_S       = float(getattr(settings, "CUSTOMER_SILENCE_END_MS", 450)) / 1000.0  # 450ms natural end-of-speech silence window
_MAX_UTTERANCE_S         = 6.0   # Maximum single utterance cap (6.0s)
_MIN_UTTERANCE_S         = 0.25  # Ignore blips <250ms
_POST_AI_SPEAK_GUARD_S   = 0.15  # Echo guard: ignore VAD/RTP RX for 150ms line transit after AI stops speaking
_PRE_SPEECH_BUFFER_MS    = 160   # Ring buffer: 160ms pre-speech audio
_CALL_INACTIVITY_TIMEOUT = float(getattr(settings, "CALL_INACTIVITY_TIMEOUT_SECONDS", 600))  # 10 minutes

# Isolated speech fragments that do not represent actionable caller intent
_INCOMPLETE_FRAGMENTS = {"and", "or", "so", "but", "because", "um", "uh", "ah", "like", "well", "hmm", "er"}

# Genuine conversational closing phrases that indicate actual end of dialogue
_GENUINE_CLOSING_PHRASES = [
    "thank you goodbye", "thank you, goodbye", "thanks goodbye", "thanks bye",
    "thank you bye", "thank you so much goodbye", "thank you so much bye",
    "that's all, thank you", "that is all, thank you", "that's all thank you",
    "that's it, thank you", "that is it, thank you", "that's all", "that is all",
    "that's everything", "that is everything", "that's everything, thank you",
    "no that's all", "no that's it", "nothing else, thank you", "nothing else thank you",
    "goodbye", "bye bye", "nandri vanakkam", "dhanyawad alvida",
    "have a great day, bye", "have a good day, bye", "have a great day, goodbye"
]


def _is_genuine_closing_phrase(text: str) -> bool:
    """Check if utterance contextually represents a genuine conversation closing."""
    if not text:
        return False
    txt_lower = text.lower().strip()
    has_closing_phrase = any(phrase in txt_lower for phrase in _GENUINE_CLOSING_PHRASES) or (
        ("thank you" in txt_lower or "thanks" in txt_lower) and any(
            w in txt_lower for w in ["bye", "goodbye", "that's all", "that is all", "that's it", "that's everything", "that is everything", "nothing else", "have a nice day", "have a good day", "have a great day"]
        )
    )
    is_continuing_query = any(w in txt_lower for w in ["what", "when", "where", "who", "which", "how", "can i", "do you", "could you", "also", "and", "one more", "another", "book", "appointment"])
    return bool(has_closing_phrase and not is_continuing_query)


def _frame_rms(data: bytes) -> float:
    """RMS energy of unsigned 8-bit PCM (pyVoIP format)."""
    if not data or data in (b"\x80" * len(data), b"\x00" * len(data), b"\xff" * len(data)):
        return 0.0
    try:
        signed = audioop.bias(data, 1, -128)
        return float(audioop.rms(signed, 1))
    except Exception:
        return 0.0


def convert_tts_to_pcmu_and_pcm16(audio_bytes: bytes, tts_provider: str = "murf") -> tuple:
    """
    Studio-Grade Anti-Aliased Resampling Pipeline for Telephony.

    Decodes any audio container (MP3, WAV, AAC, OGG) using PyAV and applies
    FFmpeg's libswresample polyphase Sinc anti-aliasing low-pass filter (cutoff 3.8 kHz)
    when downsampling to 8000 Hz. Eliminates metallic, raspy, robotic aliasing artifacts.

    Applies ITU-T G.711 / P.56 compliant level normalization (target peak 22,000,
    headroom -3.5 dBFS) to prevent phone speaker and DAC clipping.

    Returns:
        tuple (pcmu_bytes, raw_pcm16):
            pcmu_bytes: G.711 u-law payload (1 byte per sample @ 8000Hz) for pyVoIP RTP TX.
            raw_pcm16: pristine 16-bit Linear PCM (2 bytes per sample @ 8000Hz) for TwoWayCallRecorder.
    """
    if not audio_bytes:
        return b"", b""
    try:
        import wave
        buf = io.BytesIO(audio_bytes)
        raw_pcm16 = b""
        in_sr = 0
        in_ch = 1
        fmt = "UNKNOWN"

        # 1. Primary: PyAV decoding with FFmpeg polyphase Sinc anti-aliased resampling
        try:
            import av
            container = av.open(buf)
            stream = container.streams.audio[0]
            in_sr = stream.rate or 24000
            in_ch = stream.channels or 1
            fmt = container.format.name.upper()

            resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
            pcm_chunks = []
            for frame in container.decode(audio=0):
                for r_frame in resampler.resample(frame):
                    pcm_chunks.append(r_frame.to_ndarray().tobytes())
            for r_frame in resampler.resample(None):
                pcm_chunks.append(r_frame.to_ndarray().tobytes())
            raw_pcm16 = b"".join(pcm_chunks)
        except Exception as av_err:
            # 2. Secondary fallback for raw WAV
            buf.seek(0)
            try:
                with wave.open(buf, "rb") as wf:
                    in_ch = wf.getnchannels()
                    in_sw = wf.getsampwidth()
                    in_sr = wf.getframerate()
                    raw = wf.readframes(wf.getnframes())
                    fmt = "WAV"
                    if in_ch == 2:
                        raw = audioop.tomono(raw, in_sw, 0.5, 0.5)
                        in_ch = 1
                    if in_sw != 2:
                        raw = audioop.lin2lin(raw, in_sw, 2)
                    if in_sr != 8000:
                        raw, _ = audioop.ratecv(raw, 2, 1, in_sr, 8000, None)
                    raw_pcm16 = raw
            except Exception as wav_err:
                logger.error(f"[AUDIO_CONVERT] Audio decoding failed (PyAV: {av_err}, WAV: {wav_err})")
                return b"", b""

        if not raw_pcm16:
            logger.warning("[AUDIO_CONVERT] Decoded PCM is empty.")
            return b"", b""

        in_duration = (len(raw_pcm16) / 2) / 8000.0

        # Detailed Audio Format Logs
        logger.info(
            f"[TTS] provider={tts_provider} sample_rate={in_sr} channels={in_ch} duration={in_duration:.2f}s"
        )
        logger.info(
            f"[AUDIO_CONVERT] input_rate={in_sr} output_rate=8000 input_format={fmt} output_format=PCM16_SINC"
        )

        # 3. Trim excessive leading/trailing silence (>150ms)
        FRAME_10MS = 160  # 10ms @ 8kHz 16-bit mono = 80 samples * 2 bytes = 160 bytes
        SILENCE_THRESH = 350

        start_idx = 0
        while start_idx + FRAME_10MS <= len(raw_pcm16):
            if audioop.rms(raw_pcm16[start_idx:start_idx + FRAME_10MS], 2) > SILENCE_THRESH:
                break
            start_idx += FRAME_10MS
        start_idx = max(0, start_idx - (4 * FRAME_10MS))  # 40ms safety padding

        end_idx = len(raw_pcm16)
        while end_idx - FRAME_10MS >= start_idx:
            if audioop.rms(raw_pcm16[end_idx - FRAME_10MS:end_idx], 2) > SILENCE_THRESH:
                break
            end_idx -= FRAME_10MS
        end_idx = min(len(raw_pcm16), end_idx + (4 * FRAME_10MS))

        if end_idx > start_idx:
            raw_pcm16 = raw_pcm16[start_idx:end_idx]

        # 4. Telephony Level Normalization (ITU-T P.56 compliance: peak <= 22,000, zero clipping)
        peak = audioop.max(raw_pcm16, 2) if raw_pcm16 else 0
        dur_trimmed = (len(raw_pcm16) / 2) / 8000.0 if raw_pcm16 else 0.0

        if peak > 0:
            target_peak = 22000.0
            factor = min(max(target_peak / float(peak), 0.7), 2.0)
            raw_pcm16 = audioop.mul(raw_pcm16, 2, factor)
            norm_rms = audioop.rms(raw_pcm16, 2)
            norm_peak = audioop.max(raw_pcm16, 2)
            logger.info(f"[TTS_LEVEL] factor={factor:.2f} rms={norm_rms} peak={norm_peak} dur={dur_trimmed:.2f}s")

        # 5. Convert 16-bit signed PCM -> standard G.711 u-law (PCMU) bytes for pyVoIP RTP TX
        pcmu_bytes = audioop.lin2ulaw(raw_pcm16, 2)
        payload_bytes = len(pcmu_bytes)

        logger.info(
            f"[G711_PCMU_ENCODED] encoded_samples={payload_bytes} payload_bytes={payload_bytes} "
            f"pyvoip_rtp_packets={payload_bytes // 160}"
        )
        return bytes(pcmu_bytes), bytes(raw_pcm16)
    except Exception as err:
        logger.error(f"[TTS->PCMU ERROR] {err}")
        return b"", b""


def convert_tts_to_pyvoip_pcm8(audio_bytes: bytes, tts_provider: str = "murf") -> bytes:
    """Backward-compatible wrapper returning only pcmu_bytes."""
    pcmu, _ = convert_tts_to_pcmu_and_pcm16(audio_bytes, tts_provider)
    return pcmu


convert_tts_to_pcmu = convert_tts_to_pyvoip_pcm8
convert_tts_to_pcm8u = convert_tts_to_pyvoip_pcm8



def write_audio_paced(audio_send_callback, pcm8_bytes: bytes, chunk_size: int = 160, silence_byte: int = 0xFF) -> None:
    """Send audio to pyVoIP in properly-timed 20ms RTP frames."""
    import time
    FRAME_MS = 0.020
    for i in range(0, len(pcm8_bytes), chunk_size):
        chunk = pcm8_bytes[i:i + chunk_size]
        if len(chunk) < chunk_size:
            chunk = chunk + bytes([silence_byte] * (chunk_size - len(chunk)))
        try:
            audio_send_callback(chunk)
        except Exception as e:
            logger.warning(f"[RTP WRITE] Chunk send error: {e}")
            break
        time.sleep(FRAME_MS)



def pcm8u_to_wav16(pcm8_unsigned: bytes, in_rate: int = 8000, out_rate: int = 16000, call_id: str = "call", utt_id: str = "utt") -> tuple:
    """
    Convert pyVoIP u8 8kHz PCM -> clean, unclipped s16 16kHz WAV for Whisper STT.
    Returns (wav_bytes, diagnostics_dict).
    """
    if not pcm8_unsigned or len(pcm8_unsigned) < 160:
        return b"", {}
    try:
        import wave
        import struct
        s8  = audioop.bias(pcm8_unsigned, 1, -128)   # u8 (0..255) -> s8 (-128..127)
        s16 = audioop.lin2lin(s8, 1, 2)              # s8 -> s16 (-32768..32767)

        # Measure raw levels
        raw_rms  = audioop.rms(s16, 2) if s16 else 0
        raw_peak = audioop.max(s16, 2) if s16 else 0

        # Safe nominal headroom-preserving scaling: target peak ~14,000 to prevent digital clipping
        if raw_peak > 200:
            target_peak = 14000.0
            scale_factor = min(max(target_peak / float(raw_peak), 0.5), 3.0)
            s16 = audioop.mul(s16, 2, scale_factor)
        else:
            scale_factor = 1.0

        # Clean 8kHz -> 16kHz resampling for Whisper
        if in_rate != out_rate:
            s16, _ = audioop.ratecv(s16, 2, 1, in_rate, out_rate, None)

        scaled_rms  = audioop.rms(s16, 2) if s16 else 0
        scaled_peak = audioop.max(s16, 2) if s16 else 0

        # Unpack samples for detailed audio diagnostics
        sample_count = len(s16) // 2
        fmt = f"<{sample_count}h"
        samples = struct.unpack(fmt, s16) if sample_count > 0 else ()
        
        min_samp = min(samples) if samples else 0
        max_samp = max(samples) if samples else 0
        zero_samps = sum(1 for s in samples if s == 0)
        clipped_samps = sum(1 for s in samples if abs(s) >= 32700)
        first_samp = samples[0] if samples else 0
        last_samp = samples[-1] if samples else 0

        out = io.BytesIO()
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(out_rate)
            wf.writeframes(s16)

        wav_bytes = out.getvalue()

        # Save debug WAV for offline diagnostic inspection
        try:
            debug_dir = os.path.join(tempfile.gettempdir(), "ai_receptionist_debug_wav")
            os.makedirs(debug_dir, exist_ok=True)
            clean_call = "".join(c for c in str(call_id) if c.isalnum() or c in "-_")
            debug_path = os.path.join(debug_dir, f"customer_stt_debug_{clean_call}_{utt_id}.wav")
            with open(debug_path, "wb") as f_dbg:
                f_dbg.write(wav_bytes)
        except Exception:
            pass

        diag = {
            "duration_s": round(len(pcm8_unsigned) / float(in_rate), 2),
            "rtp_packet_count": len(pcm8_unsigned) // 160,
            "rtp_payload_type": 0,
            "sample_rate": out_rate,
            "channels": 1,
            "sample_format": "PCM16_MONO",
            "sample_count": sample_count,
            "raw_rms": raw_rms,
            "raw_peak": raw_peak,
            "rms": scaled_rms,
            "peak": scaled_peak,
            "min_sample": min_samp,
            "max_sample": max_samp,
            "zero_samples": zero_samps,
            "clipped_samples": clipped_samps,
            "first_sample": first_samp,
            "last_sample": last_samp,
            "scale_factor": round(scale_factor, 2),
            "audio_source": "CUSTOMER_RTP",
        }
        return wav_bytes, diag
    except Exception as exc:
        logger.warning(f"[STT Convert] {exc}")
        return b"", {}


pcm8_to_wav16 = pcm8u_to_wav16


# ─── Active call registry ─────────────────────────────────────────────────────
_active_sip_calls: Dict[str, "SipCallSession"] = {}


_call_termination_lock = asyncio.Lock()


def get_active_call(call_sid: str) -> Optional["SipCallSession"]:
    return _active_sip_calls.get(call_sid)


def register_call(session: "SipCallSession"):
    _active_sip_calls[session.call_sid] = session


def unregister_call(call_sid: str):
    _active_sip_calls.pop(call_sid, None)


def active_call_count(organization_id: Optional[int] = None) -> int:
    if organization_id is None:
        return len(_active_sip_calls)
    return sum(1 for s in _active_sip_calls.values() if getattr(s, "organization_id", None) == organization_id)


async def terminate_call(call_id: Optional[str] = None, reason: str = "LOCAL_HANGUP") -> Dict[str, Any]:
    """
    Authoritative, idempotent call termination function.
    Handles both REMOTE_HANGUP (customer phone) and LOCAL_HANGUP (laptop UI).
    """
    session = None
    if call_id and str(call_id).strip() and str(call_id).strip() != "undefined":
        session = get_active_call(call_id)
        if not session:
            for s_id, s_obj in list(_active_sip_calls.items()):
                if s_id == call_id or str(getattr(s_obj, "db_call_id", "")) == str(call_id):
                    session = s_obj
                    break

    # Fallback: If no exact ID match or call_id was undefined, pick the active call from registry
    if not session and _active_sip_calls:
        session = list(_active_sip_calls.values())[-1]
        logger.info(f"[CALL_TERMINATE_FALLBACK] Matched active session call_id={session.call_sid}")

    if not session:
        logger.info(f"[CALL_ENDED_SKIPPED] call_id={call_id} not active or already terminated")
        return {"status": "already_ended", "call_id": call_id}

    if getattr(session, "_terminated", False):
        logger.info(f"[CALL_ENDED_SKIPPED] call_id={session.call_sid} already terminated")
        return {"status": "already_ended", "call_id": session.call_sid}

    async with _call_termination_lock:
        if getattr(session, "_terminated", False):
            return {"status": "already_ended", "call_id": session.call_sid}
        session._terminated = True

        session.end_reason = reason
        logger.info(f"[CALL_HANGUP_REQUESTED] call_id={session.call_sid} reason={reason}")
        logger.info(f"[CALL_ENDING] call_id={session.call_sid} reason={reason}")

        session.ended = True
        session._ai_speaking = False
        session._barge_in_event.set()

        # 1. IMMEDIATELY & SYNCHRONOUSLY finalize recording to WAV and persist DB record
        try:
            session._finalise_call_log_sync()
        except Exception as _fe:
            logger.error(f"[TERMINATE] Finalise error for {session.call_sid}: {_fe}")

        # 2. Send SIP BYE to pyVoIP to disconnect customer's phone if call is still active
        if hasattr(session, "pyvoip_call") and session.pyvoip_call:
            try:
                p_call = session.pyvoip_call
                from pyVoIP.VoIP import CallState
                call_st = getattr(p_call, "state", None)
                if call_st == CallState.ANSWERED:
                    logger.info(f"[SIP_BYE_SENT] call_id={session.call_sid}")
                    if hasattr(p_call, "hangup"):
                        p_call.hangup()
                else:
                    logger.info(f"[SIP_ALREADY_TERMINATED] call_id={session.call_sid} state={call_st}")
            except Exception as exc:
                logger.warning(f"[SIP_BYE_ERROR] call_id={session.call_sid}: {exc}")

        # 3. Teardown session resources
        await session.stop()

        # 3. Broadcast CALL_ENDED event
        ts = datetime.now(timezone.utc).isoformat()
        payload = {
            "type": "CALL_ENDED",
            "event": "call_ended",
            "call_id": session.call_sid,
            "call_sid": session.call_sid,
            "reason": reason,
            "timestamp": ts,
        }
        status_payload = {
            "call_sid": session.call_sid,
            "call_id": session.call_sid,
            "id": session.call_sid,
            "status": "completed",
            "customer_number": session.caller_phone,
            "caller_number": session.caller_phone,
            "customer_name": session.caller_name,
            "caller_name": session.caller_name,
        }
        try:
            from src.sockets.socket_manager import sio
            await sio.emit("call_status_update", status_payload)
            await sio.emit("CALL_ENDED", payload)
            await sio.emit("call_ended", payload)
            await sio.emit("call_status_event", {"call_sid": session.call_sid, "phase": "idle", "timestamp": ts})
        except Exception as exc:
            logger.warning(f"[SOCKET EMIT ERROR] {exc}")

        logger.info(f"[CALL_ENDED] call_id={session.call_sid} reason={reason}")
        logger.info(f"[MONITOR_STOPPED] call_id={session.call_sid}")
        logger.info(f"[AI_SESSION_STOPPED] call_id={session.call_sid}")

        return {"status": "success", "call_id": session.call_sid, "reason": reason}


class TwoWayCallRecorder:
    """
    Two-Way Call Recording Engine.

    Synchronously captures and aligns:
    1. Customer voice (RX from incoming RTP audio stream)
    2. AI Receptionist voice (TX from TTS output)

    Features:
    - Drift-free sample-based timeline starting at call connect (T = 0.0s).
    - Headroom-protected soft mix (0.90 headroom) preventing clipping on barge-in.
    - True two-way standard 16-bit 8000 Hz mono WAV recording.
    - Thread-safe audio feed for simultaneous RX and TX.
    """
    def __init__(self, sample_rate: int = 8000):
        self.sample_rate = sample_rate
        self.bytes_per_sample = 2  # 16-bit signed
        self.bytes_per_sec = sample_rate * self.bytes_per_sample  # 16000 bytes/sec
        self.start_time: Optional[float] = None
        self._lock = threading.Lock()
        self.customer_track = bytearray()
        self.ai_track = bytearray()
        self._finalized_wav: Optional[bytes] = None

    def start(self, start_time: Optional[float] = None):
        with self._lock:
            self.start_time = start_time if start_time is not None else time.monotonic()
            self.customer_track.clear()
            self.ai_track.clear()
            self._finalized_wav = None

    def feed_customer_pcm16(self, pcm16_bytes: bytes, current_time: Optional[float] = None):
        """Append incoming customer PCM16 audio at the current timeline position."""
        if not pcm16_bytes:
            return
        with self._lock:
            if self.start_time is None:
                self.start_time = time.monotonic()
            now = current_time if current_time is not None else time.monotonic()
            target_offset = max(0, int((now - self.start_time) * self.sample_rate) * self.bytes_per_sample)
            # Pad silence if customer stream had an idle gap (>40ms)
            if len(self.customer_track) < target_offset - 640:
                gap = target_offset - len(self.customer_track)
                if gap % 2 != 0:
                    gap += 1
                self.customer_track.extend(b"\x00" * gap)
            self.customer_track.extend(pcm16_bytes)

    def feed_ai_pcm16(self, pcm16_bytes: bytes, current_time: Optional[float] = None):
        """Append transmitted AI PCM16 audio at the current timeline position."""
        if not pcm16_bytes:
            return
        with self._lock:
            if self.start_time is None:
                self.start_time = time.monotonic()
            now = current_time if current_time is not None else time.monotonic()
            target_offset = max(0, int((now - self.start_time) * self.sample_rate) * self.bytes_per_sample)
            if len(self.ai_track) < target_offset:
                gap = target_offset - len(self.ai_track)
                if gap % 2 != 0:
                    gap += 1
                self.ai_track.extend(b"\x00" * gap)
            self.ai_track.extend(pcm16_bytes)

    def finalize(self) -> bytes:
        """Mix both tracks with headroom limiting into a valid standard WAV."""
        with self._lock:
            if self._finalized_wav is not None:
                return self._finalized_wav

            total_len = max(len(self.customer_track), len(self.ai_track))
            if total_len == 0:
                total_len = self.bytes_per_sec  # 1 second minimum silence
            if total_len % 2 != 0:
                total_len += 1

            c_padded = bytes(self.customer_track) + (b"\x00" * (total_len - len(self.customer_track)))
            a_padded = bytes(self.ai_track) + (b"\x00" * (total_len - len(self.ai_track)))

            import numpy as np
            c = np.frombuffer(c_padded, dtype=np.int16).astype(np.float32)
            a = np.frombuffer(a_padded, dtype=np.int16).astype(np.float32)

            # Headroom-protected mix (0.90 headroom prevents clipping on barge-in)
            mixed = np.clip(c * 0.90 + a * 0.90, -32768.0, 32767.0).astype(np.int16)

            import io, wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(self.bytes_per_sample)
                wf.setframerate(self.sample_rate)
                wf.writeframes(mixed.tobytes())

            self._finalized_wav = buf.getvalue()
            return self._finalized_wav


class SipCallSession:
    """Full-duplex real-time AI voice bridge for a single SIP call."""

    def __init__(
        self,
        caller_phone: str = "Unknown",
        caller_name: str = "SIP Caller",
        organization_id: int = 1,
        audio_send_callback: Optional[Callable[[bytes], None]] = None,
        audio_clear_callback: Optional[Callable[[], None]] = None,
        agent_id: Optional[str] = None,
        active_script_id: Optional[str] = None,
        active_knowledge_id: Optional[str] = None,
        call_sid: Optional[str] = None,
        call_type: Optional[CallType] = None,
    ):
        self.call_sid           = call_sid or f"sip_{uuid.uuid4().hex[:12]}"
        self.caller_phone       = caller_phone
        self.caller_name        = caller_name
        self.organization_id    = organization_id

        # Determine Inbound vs Outbound call direction
        if call_type is not None:
            self.call_type = call_type
        elif self.call_sid.startswith("sip_out") or "Outbound" in str(caller_name):
            self.call_type = CallType.OUTBOUND
        else:
            self.call_type = CallType.INBOUND

        self.agent_id           = agent_id or "default_receptionist"
        from src.services.rag_service import rag_service
        self.active_script_id   = active_script_id or rag_service._resolve_active_document_id(self.organization_id, document_type="SCRIPT")
        self.active_knowledge_id = active_knowledge_id or rag_service._resolve_active_document_id(self.organization_id)
        # Receives PCMU u8 bytes to send via pyVoIP write_audio
        self.audio_send_callback  = audio_send_callback
        # Clears RTP TX buffer on barge-in
        self.audio_clear_callback = audio_clear_callback
        self.pyvoip_call          = None
        self.end_reason           = "COMPLETED"

        self.session_id  = f"sip_session_{self.call_sid}"
        self.db_call_id: Optional[int] = None
        self.started_at  = datetime.now(timezone.utc)
        self.ended       = False
        self.human_attended = False

        # ── Separate audio queues (never mix RX and TX paths) ─────
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=500)  # customer RTP -> VAD -> STT
        self._stt_queue: asyncio.Queue   = asyncio.Queue(maxsize=10)   # complete utterances

        # ── Speaking state & VAD adaptive tracking ───────────────
        self._ai_speaking           = False
        self._ai_speaking_until     = 0.0
        self._barge_in_event        = asyncio.Event()
        self._speak_lock            = asyncio.Lock()
        self._barge_in_loud_frames  = 0
        self._last_ai_speak_end_time = 0.0
        self._noise_floor           = 12.0
        self._recent_ai_texts: deque = deque(maxlen=10)

        # ── Utterance dedup & lifecycle ───────────────────────────
        self._utterance_counter    = 100
        self._last_transcript      = ""
        self._last_transcript_time = 0.0
        self._processed_utt_ids: deque = deque(maxlen=20)
        self._pipeline_lock = asyncio.Lock()
        self._active_stream_abort = None
        self._current_active_utt_id = None
        self._last_tts_ready = 0.0
        self._call_ending_scheduled = False

        # ── Deepgram Real-Time Streaming STT Session ───────────────
        self.deepgram_session = DeepgramStreamingSession(
            call_id=self.call_sid,
            on_partial=self._on_deepgram_partial,
            on_final=self._on_deepgram_final,
            sample_rate=8000,
        )

        # ── Conversation ──────────────────────────────────────────
        self.session_history: List[Dict[str, str]] = []
        self.booking_session = booking_state_service.get_session(
            session_id=self.session_id,
            conversation_id=self.session_id,
            call_sid=self.call_sid
        )
        self.booking_session.call_sid = self.call_sid
        self.booking_session.conversation_id = self.session_id
        self.booking_session.organization_id = organization_id
        self.booking_session.agent_id = self.agent_id
        self.booking_session.active_rag_id = self.active_knowledge_id
        self.booking_session.caller_phone = self.caller_phone
        self.booking_session.caller_name = self.caller_name
        # Keep caller_phone on session, but do not auto-assign contact_number without customer confirmation
        self.current_lang = "en"

        # ── Two-Way Synchronized Call Recorder ───────────────────
        self.two_way_recorder = TwoWayCallRecorder(sample_rate=8000)
        self._recording_pcm_frames = bytearray()

        # ── Customer Live Audio Monitor Buffer (for Laptop/Browser playback) ──
        self._monitor_pcm_buffer = bytearray()

        # ── Inactivity watchdog ───────────────────────────────────
        # Reset this timestamp whenever the customer speaks. The watchdog task
        # will gracefully end the call if no activity occurs for _CALL_INACTIVITY_TIMEOUT seconds.
        self._last_activity_time: float = time.monotonic()
        self._watchdog_task: Optional[asyncio.Task] = None

        self._messages: List[Dict[str, Any]] = []
        self._finalise_lock = threading.Lock()
        self._call_log_finalised = False
        self._terminated = False
        register_call(self)
        logger.info(f"[SIP CALL] New session: {self.call_sid} from {caller_phone} org={organization_id} agent={self.agent_id}")

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self):
        self.two_way_recorder.start()
        await self._create_call_log()
        try:
            from src.sockets.socket_manager import sio
            payload = {
                "call_sid": self.call_sid,
                "call_id": self.call_sid,
                "id": self.call_sid,
                "status": "in-progress",
                "customer_number": self.caller_phone,
                "caller_number": self.caller_phone,
                "caller_phone": self.caller_phone,
                "customer_name": self.caller_name,
                "caller_name": self.caller_name,
                "phone": self.caller_phone,
                "name": self.caller_name,
                "direction": "inbound" if self.call_type == CallType.INBOUND else "outbound",
                "event": "incoming-call" if self.call_type == CallType.INBOUND else "outgoing-call",
                "caller": {
                    "phone": self.caller_phone,
                    "name": self.caller_name,
                    "customer_number": self.caller_phone,
                    "customer_name": self.caller_name,
                    "is_known": (self.caller_name not in ("Unknown", "Unknown Caller", "Customer Mobile", "SIP Caller", "Private Number")),
                }
            }
            if self.call_type == CallType.INBOUND:
                await sio.emit("incoming-call", payload)
                await sio.emit("incoming_call", payload)
            await sio.emit("call_status_update", payload)
        except Exception:
            pass
        # Launch Deepgram real-time streaming session
        try:
            await self.deepgram_session.start()
            logger.info(f"[DEEPGRAM_SESSION_STARTED] call_id={self.call_sid} connected={self.deepgram_session._connected}")
        except Exception as _dg_err:
            logger.error(f"[DEEPGRAM_START_ERROR] call_id={self.call_sid}: {_dg_err}")
        # Launch inactivity watchdog — ends the call if customer is silent too long
        self._last_activity_time = time.monotonic()
        self._watchdog_task = asyncio.create_task(
            self._inactivity_watchdog(), name=f"watchdog_{self.call_sid}"
        )
        await self._send_opening_greeting()

        logger.info(f"[CALL_CONNECTED] call_id={self.call_sid} DB_id={self.db_call_id}")

    async def _inactivity_watchdog(self):
        """Gracefully ends the call after _CALL_INACTIVITY_TIMEOUT seconds of customer silence.
        Resets whenever _last_activity_time is updated (on each customer utterance).
        Only polls every 10 seconds to avoid wasting CPU.
        """
        timeout = _CALL_INACTIVITY_TIMEOUT
        logger.info(
            f"[WATCHDOG_START] call_id={self.call_sid} inactivity_timeout={timeout:.0f}s"
        )
        try:
            while not self.ended:
                await asyncio.sleep(10)  # check every 10s
                if self.ended:
                    break
                idle_secs = time.monotonic() - self._last_activity_time
                if idle_secs >= timeout:
                    logger.warning(
                        f"[WATCHDOG_TIMEOUT] call_id={self.call_sid} "
                        f"idle={idle_secs:.0f}s >= timeout={timeout:.0f}s — ending call"
                    )
                    self.end_reason = "INACTIVITY_TIMEOUT"
                    self.ended = True
                    self._barge_in_event.set()
                    try:
                        from src.services.sip_call_handler import terminate_call
                        await terminate_call(self.call_sid, reason="INACTIVITY_TIMEOUT")
                    except Exception as exc:
                        logger.warning(f"[WATCHDOG_TERMINATE_ERROR] call_id={self.call_sid}: {exc}")
                    break
        except asyncio.CancelledError:
            pass
        logger.info(f"[WATCHDOG_STOPPED] call_id={self.call_sid}")

    async def stop(self):
        self.ended = True
        self._ai_speaking = False
        self._barge_in_event.set()

        # 1. Finalize recording and call log synchronously first so data is NEVER lost
        try:
            self._finalise_call_log_sync()
        except Exception as exc:
            logger.error(f"[SIP CALL] Finalise error {self.call_sid}: {exc}")

        # 2. Cancel watchdog cleanly without cross-loop await error
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                curr_loop = asyncio.get_running_loop()
                task_loop = getattr(self._watchdog_task, "_loop", None)
                if curr_loop is task_loop:
                    await self._watchdog_task
            except (asyncio.CancelledError, RuntimeError, Exception):
                pass
            self._watchdog_task = None

        if getattr(self, "_active_stream_abort", None):
            try:
                self._active_stream_abort.set()
            except Exception:
                pass

        if hasattr(self, "deepgram_session") and self.deepgram_session:
            try:
                curr_loop = asyncio.get_running_loop()
                dg_loop = getattr(self.deepgram_session, "_loop", None)
                if dg_loop is None or curr_loop is dg_loop:
                    await self.deepgram_session.stop()
            except Exception as exc:
                logger.warning(f"[DEEPGRAM STOP ERROR] {exc}")

        # Drain queued audio & stt items so pending frames are discarded immediately
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Exception:
                break

        while not self._stt_queue.empty():
            try:
                self._stt_queue.get_nowait()
            except Exception:
                break

        try:
            unregister_call(self.call_sid)
            booking_state_service.remove_session(self.session_id)
            await self._emit_status("idle")
            from src.sockets.socket_manager import sio
            end_payload = {
                "call_sid": self.call_sid,
                "call_id": self.call_sid,
                "id": self.call_sid,
                "status": "completed",
                "customer_number": self.caller_phone,
                "caller_number": self.caller_phone,
            }
            await sio.emit("call_status_update", end_payload)
            await sio.emit("call_ended", end_payload)
            await sio.emit("CALL_ENDED", end_payload)
        except Exception as exc:
            logger.warning(f"[SIP CALL] Teardown error {self.call_sid}: {exc}")

        logger.info(f"[SIP CALL] Session {self.call_sid} stopped.")

    # ── AI Echo Detection ─────────────────────────────────────────

    def _is_ai_echo(self, text: str) -> bool:
        """
        Detect if an incoming transcript is true acoustic speakerphone echo or line loopback
        of recent AI speech arriving during or immediately after AI playback.
        
        CRITICAL RULES:
        - NEVER classify genuine customer responses or interruptions as echo.
        - Only consider echo if AI was speaking recently (within last 0.45s).
        - If the transcript is a substring or verbatim phrase of what the AI is currently uttering,
          it is acoustic feedback of the AI's own voice picked up by the microphone.
        """
        if not text or not text.strip():
            return False

        now = time.monotonic()
        in_playback = self._ai_speaking or (now < getattr(self, "_ai_speaking_until", 0.0))
        time_since_ai = now - getattr(self, "_last_ai_speak_end_time", 0.0)

        if not in_playback and time_since_ai > 2.5:
            return False

        clean_t = "".join(c.lower() for c in text if c.isalnum() or c.isspace()).strip()
        if not clean_t:
            return False

        words_t = clean_t.split()

        # CRITICAL: Never drop short customer utterances (e.g. "Hello?", "Yes", "47", "12 PM", "Thank you") as echo
        protected_customer_words = {
            "hello", "hi", "hey", "yes", "yeah", "yep", "no", "nah", "okay", "ok", "sure",
            "thanks", "thank", "today", "tomorrow", "morning", "afternoon", "evening",
            "am", "pm", "correct", "right", "fine"
        }
        if len(words_t) <= 3 and (
            any(w in protected_customer_words for w in words_t)
            or any(w.isdigit() for w in words_t)
            or bool(re.search(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b', clean_t))
        ):
            return False

        # Check against recent AI utterances
        for ai_t in self._recent_ai_texts:
            clean_ai = "".join(c.lower() for c in ai_t if c.isalnum() or c.isspace()).strip()
            if not clean_ai:
                continue

            # Verbatim exact match
            if clean_t == clean_ai:
                return True

            # If AI is currently transmitting or in immediate echo window:
            # Any phrase of 2+ words that is a verbatim substring of what AI is saying is acoustic echo
            if len(words_t) >= 2 and clean_t in clean_ai:
                return True

            # High word containment (if all or almost all words exist in AI speech)
            words_ai = set(clean_ai.split())
            if len(words_t) >= 3 and words_ai:
                in_ai_count = sum(1 for w in words_t if w in words_ai)
                if (in_ai_count / float(len(words_t))) >= 0.80:
                    return True

        return False

    # ── Audio Input (called from RTP thread, non-blocking) ────────

    def feed_audio(self, audio_bytes: bytes):
        """
        Feed raw u8 PCM bytes from pyVoIP PCMU decode.

        CRITICAL SEPARATION:
        - Recording copy: pcm16 boost -> _recording_pcm_frames (for WAV save at end)
        - Deepgram STT stream: continuous signed Linear16 8000Hz mono -> deepgram_session
        - Customer Live Audio Monitor: real-time 100ms signed PCM16 -> Socket.IO customer_audio_chunk
          Guaranteed: AI TTS/RTP TX audio is suppressed and NEVER fed to Deepgram or customer monitor.
        """
        if not audio_bytes or self.ended:
            return

        # Convert pyVoIP 8-bit unsigned PCM -> 16-bit signed Linear PCM (8000 Hz mono)
        try:
            s8  = audioop.bias(audio_bytes, 1, -128)
            s16 = audioop.lin2lin(s8, 1, 2)
        except Exception:
            try:
                s16 = audioop.ulaw2lin(audio_bytes, 2)
            except Exception:
                return

        # Two-Way Synchronized Call Recording (unclipped, natural customer audio)
        try:
            if hasattr(self, "two_way_recorder"):
                self.two_way_recorder.feed_customer_pcm16(s16)
            self._recording_pcm_frames.extend(s16)
        except Exception:
            pass

        # Echo suppression & noise floor adaptation
        rms = _frame_rms(audio_bytes)
        now = time.monotonic()
        in_ai_transmitting = (self._ai_speaking or now < getattr(self, "_ai_speaking_until", 0.0))

        if not in_ai_transmitting:
            self._noise_floor = max(4.0, min(35.0, self._noise_floor * 0.98 + rms * 0.02))

        # Stream customer audio continuously to Deepgram streaming STT session
        try:
            self.deepgram_session.feed_pcm16_audio(s16)
        except Exception:
            pass

    # ── Concurrent pipeline ───────────────────────────────────────

    async def process_audio_loop(self):
        """Maintain active session state while Deepgram streams audio and triggers callbacks."""
        await self._emit_status("listening")
        while not self.ended:
            await asyncio.sleep(0.1)

    # ── Deepgram Callbacks ────────────────────────────────────────

    async def _on_deepgram_partial(self, utt_id: str, transcript: str, duration_s: float):
        """Handle real-time interim results from Deepgram."""
        if self.ended or getattr(self, "human_attended", False):
            return
        text = (transcript or "").strip()
        if not text:
            return

        now = time.monotonic()

        # Suppress AI speech echo
        if self._is_ai_echo(text):
            logger.info(f"[AI_ECHO_FILTER] Partial echo discarded: '{text}' utt_id={utt_id}")
            return

        in_ai_speaking = self._ai_speaking or (now < getattr(self, "_ai_speaking_until", 0.0))
        if in_ai_speaking:
            # Genuine customer speech started while AI was speaking - trigger barge-in interrupt!
            logger.info(f"[BARGE_IN_PARTIAL] Customer interrupted AI speech with partial: '{text}' utt_id={utt_id}")
            self._ai_speaking = False
            self._ai_speaking_until = 0.0
            self._barge_in_event.set()
            if self.audio_clear_callback:
                try:
                    self.audio_clear_callback()
                except Exception:
                    pass

        await self._emit_transcript_typed("CUSTOMER_PARTIAL", utt_id, text, {"audio_duration_s": duration_s})

    async def _on_deepgram_final(self, utt_id: str, transcript: str, metrics: Dict[str, Any]):
        """Handle speech_final / endpointed results from Deepgram."""
        if self.ended or getattr(self, "human_attended", False):
            return
        text = (transcript or "").strip()
        if not text or text.lower() in ("", " ", "[silence]", "[noise]", "<silence>", "<noise>", "...", ".", "-"):
            logger.info(f"[DEEPGRAM_STT_EMPTY] utt_id={utt_id} (buffer cleared, no AI response generated)")
            return

        # Suppress isolated dangling conjunctions and filler fragments from triggering turns
        if text.lower() in _INCOMPLETE_FRAGMENTS:
            logger.info(f"[INCOMPLETE_SPEECH_FRAGMENT] Discarded isolated speech fragment: '{text}' utt_id={utt_id}")
            return

        now = time.monotonic()

        # Suppress AI speech echo from becoming CUSTOMER_FINAL
        if self._is_ai_echo(text):
            logger.warning(f"[AI_ECHO_FILTER] Discarded echoed AI transcript from CUSTOMER_FINAL: '{text}' utt_id={utt_id}")
            return

        # If genuine customer speech arrives while AI playback guard is active, interrupt AI playback guard
        if self._ai_speaking or now < getattr(self, "_ai_speaking_until", 0.0):
            logger.info(f"[BARGE_IN_STT] Genuine customer final transcript received during AI window: '{text}' utt_id={utt_id}")
            self._ai_speaking = False
            self._ai_speaking_until = 0.0
            self._barge_in_event.set()
            if self.audio_clear_callback:
                try:
                    self.audio_clear_callback()
                except Exception:
                    pass

        # Normalize text for duplicate check (strip punctuation and whitespace)
        clean_text = re.sub(r'[^\w\s]', '', text).lower().strip()
        clean_last = re.sub(r'[^\w\s]', '', getattr(self, "_last_transcript", "") or "").lower().strip()

        # Deduplicate using call_id + utterance_id + event_type
        event_key = f"{self.call_sid}_{utt_id}_CUSTOMER_FINAL"
        if utt_id in self._processed_utt_ids or event_key in self._processed_utt_ids:
            logger.info(f"[STT] Duplicate utterance_id/event ignored: {event_key}")
            return

        # Suppress duplicate identical/near-identical transcript arriving within 1.5s
        if clean_text and clean_text == clean_last and (now - getattr(self, "_last_transcript_time", 0.0)) < 1.5:
            logger.info(f"[STT] Duplicate transcript within 1.5s ignored: '{text}' utt_id={utt_id}")
            return

        self._processed_utt_ids.append(utt_id)
        self._processed_utt_ids.append(event_key)
        self._last_transcript = text
        self._last_transcript_time = now
        # Reset inactivity watchdog: customer is actively speaking
        self._last_activity_time = time.monotonic()

        audio_dur_s = metrics.get("audio_duration_s", 0.0)
        audio_dur_ms = metrics.get("audio_duration_ms", round(audio_dur_s * 1000))
        stt_latency_ms = metrics.get("deepgram_final_ms", metrics.get("stt_latency_ms", 0))
        finalization_ms = metrics.get("stt_finalization_ms", metrics.get("finalization_latency_ms", 0))

        # Required structured pipeline logging
        logger.info(
            f"\n[CONVERSATION_TURN]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
            f"\n  source=CUSTOMER"
            f"\n  speaker=customer"
            f"\n  event_type=CUSTOMER_FINAL"
        )

        logger.info(
            f"\n[CUSTOMER_FINAL]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
            f"\n  text='{text}'"
        )

        logger.info(
            f"\n[CUSTOMER_FINAL_METRICS]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
            f"\n  audio_duration={audio_dur_s:.2f}s ({audio_dur_ms}ms)"
            f"\n  customer_audio_duration_ms={audio_dur_ms}ms"
            f"\n  stt_finalization_ms={finalization_ms}ms"
            f"\n  total_turnaround_ms={stt_latency_ms}ms"
            f"\n  stt_latency={stt_latency_ms} ms"
            f"\n  source=CUSTOMER"
            f"\n  audio_source=CUSTOMER_RTP"
            f"\n  text='{text}'"
        )

        metrics["utt_id"] = utt_id
        await self._emit_transcript_typed("CUSTOMER_FINAL", utt_id, text, {
            "source": "CUSTOMER",
            "confidence": metrics.get("confidence", 1.0),
            "audio_duration_s": audio_dur_s,
            "stt_latency_ms": stt_latency_ms,
        })

        await self._run_ai_pipeline(text, metrics)

    # ── Task A: VAD + Speech Segmentation ─────────────────────────

    async def _audio_drain_loop(self):
        """
        Continuous, non-blocking VAD loop. Drains _audio_queue 20ms at a time.
        Uses adaptive noise floor estimation and energy hysteresis for accurate segmentation.
        Streams partial STT while customer speaks and flushes to _stt_queue immediately upon speech_end.
        Never accumulates stale audio across utterances.
        """
        speech_buffer          = bytearray()
        pre_speech_ring: deque = deque(maxlen=_PRE_SPEECH_FRAMES)
        consecutive_loud       = 0
        gate_open              = False
        silence_since: Optional[float]     = None
        speech_start_time: Optional[float] = None
        current_utt_id: str                = ""
        last_partial_dur: float            = 0.0

        await self._emit_status("listening")

        while not self.ended:
            try:
                frame = await asyncio.wait_for(self._audio_queue.get(), timeout=0.04)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if gate_open:
                    if silence_since is None:
                        silence_since = now
                    if (now - silence_since) >= _SILENCE_TIMEOUT_S:
                        dur = len(speech_buffer) / 8000.0
                        logger.info(f"[VAD] speech_end (queue timeout) dur={dur:.2f}s utt_id={current_utt_id} call_sid={self.call_sid}")
                        await self._flush_speech(speech_buffer, current_utt_id, speech_start_time, silence_since)
                        speech_buffer.clear()
                        pre_speech_ring.clear()
                        gate_open = False
                        silence_since = None
                        speech_start_time = None
                        consecutive_loud = 0
                        last_partial_dur = 0.0
                continue

            rms = _frame_rms(frame)
            now = time.monotonic()
            in_ai_transmitting = now < getattr(self, "_ai_speaking_until", 0.0)
            in_post_speak  = (now - self._last_ai_speak_end_time) < _POST_AI_SPEAK_GUARD_S

            # Echo suppression & Noise Floor adaptation
            if not gate_open:
                if (in_ai_transmitting or in_post_speak):
                    pre_speech_ring.clear()  # Never keep AI audio/echo in pre-speech ring
                    barge_thresh = max(42.0, self._noise_floor * 2.8 + 14.0)
                    if rms < barge_thresh:
                        consecutive_loud = 0
                        continue
                else:
                    # Update adaptive noise floor during quiet periods when AI is not transmitting
                    self._noise_floor = max(4.0, min(35.0, self._noise_floor * 0.98 + rms * 0.02))

            speech_open_thresh = max(40.0, self._noise_floor * 2.8 + 14.0) if (in_ai_transmitting or in_post_speak) else max(24.0, self._noise_floor * 1.8 + 6.0)
            min_loud_frames    = 4 if (in_ai_transmitting or in_post_speak) else 3
            silence_thresh     = max(16.0, self._noise_floor * 1.3 + 3.0)

            if not gate_open:
                if rms >= speech_open_thresh:
                    consecutive_loud += 1
                else:
                    consecutive_loud = 0
                pre_speech_ring.append(frame)
            else:
                if rms >= silence_thresh:
                    consecutive_loud += 1
                    # Only cancel silence if speech is sustained (>=2 frames / 40ms) to ignore random line noise spikes
                    if consecutive_loud >= 2:
                        silence_since = None
                else:
                    consecutive_loud = 0
                    if silence_since is None:
                        silence_since = now

            # Open gate on sustained loud voice signal
            if not gate_open and consecutive_loud >= min_loud_frames:
                gate_open         = True
                speech_start_time = now
                self._utterance_counter += 1
                current_utt_id = f"utt_{self._utterance_counter}"
                last_partial_dur = 0.0

                if in_ai_transmitting:
                    logger.info(f"[BARGE_IN] Genuine customer voice interrupted AI speech (rms={rms:.1f} thresh={speech_open_thresh:.1f}) utt_id={current_utt_id}")
                    self._barge_in_event.set()
                    self._ai_speaking = False
                    self._ai_speaking_until = 0.0
                    self._last_ai_speak_end_time = now
                    pre_speech_ring.clear()
                    if self.audio_clear_callback:
                        try:
                            self.audio_clear_callback()
                        except Exception:
                            pass
                else:
                    for pf in pre_speech_ring:
                        speech_buffer.extend(pf)
                    pre_speech_ring.clear()

                logger.info(f"[VAD] speech_start rms={rms:.1f} thresh={speech_open_thresh:.1f} floor={self._noise_floor:.1f} utt_id={current_utt_id} call_sid={self.call_sid}")

            if gate_open:
                speech_buffer.extend(frame)
                dur = len(speech_buffer) / 8000.0  # Exact physical audio duration

                # Live partial STT streaming every ~0.8s once past 1.0s speech
                if dur >= last_partial_dur + 0.8 and dur >= 1.0:
                    last_partial_dur = dur
                    snapshot_bytes = bytes(speech_buffer)
                    asyncio.create_task(self._process_partial_speech(current_utt_id, snapshot_bytes, speech_start_time))

                is_silence_timeout = (silence_since is not None and (now - silence_since) >= _SILENCE_TIMEOUT_S)
                is_max_dur_reached = (dur >= _MAX_UTTERANCE_S)

                if is_max_dur_reached:
                    logger.info(f"[VAD] speech_end (max dur {dur:.2f}s) utt_id={current_utt_id} call_sid={self.call_sid}")
                    await self._flush_speech(speech_buffer, current_utt_id, speech_start_time, silence_since or now)
                    speech_buffer.clear()
                    pre_speech_ring.clear()
                    gate_open = False
                    silence_since = None
                    speech_start_time = None
                    consecutive_loud = 0
                    last_partial_dur = 0.0
                elif is_silence_timeout:
                    # Trim trailing silence bytes beyond 100ms
                    silence_dur = now - silence_since
                    trailing_silence_bytes = int(silence_dur * 8000)
                    if trailing_silence_bytes > 800 and len(speech_buffer) > trailing_silence_bytes:
                        del speech_buffer[len(speech_buffer) - (trailing_silence_bytes - 800):]
                    dur = len(speech_buffer) / 8000.0
                    logger.info(f"[VAD] speech_end dur={dur:.2f}s (silence={silence_dur:.2f}s floor={self._noise_floor:.1f}) utt_id={current_utt_id} call_sid={self.call_sid}")
                    await self._flush_speech(speech_buffer, current_utt_id, speech_start_time, silence_since)
                    speech_buffer.clear()
                    pre_speech_ring.clear()
                    gate_open = False
                    silence_since = None
                    speech_start_time = None
                    consecutive_loud = 0
                    last_partial_dur = 0.0

        if speech_buffer and not self.ended:
            await self._flush_speech(speech_buffer, current_utt_id or f"utt_{self._utterance_counter+1}", speech_start_time, silence_since or time.monotonic())
            speech_buffer.clear()

    async def _flush_speech(self, buf: bytearray, utt_id: str, speech_start: Optional[float], speech_end: Optional[float] = None):
        if not buf or self.ended:
            return
        audio_seg = bytes(buf)  # Freeze immutable copy immediately
        buf.clear()             # Clear mutable buffer immediately

        duration_s = len(audio_seg) / 8000.0
        if duration_s < _MIN_UTTERANCE_S:
            logger.debug(f"[VAD] Utterance too short ({duration_s:.2f}s) - discarding")
            return

        vad_end = time.monotonic()
        actual_speech_end = speech_end or vad_end
        try:
            self._stt_queue.put_nowait((utt_id, audio_seg, speech_start or vad_end, actual_speech_end, vad_end))
            logger.info(f"[VAD] utterance_id={utt_id} queued dur={duration_s:.2f}s bytes={len(audio_seg)}")
        except asyncio.QueueFull:
            logger.warning(f"[VAD] STT queue full - dropping {utt_id}")

    # ── Task B: STT + AI + TTS ────────────────────────────────────

    async def _ai_processing_loop(self):
        """Drain _stt_queue and run full AI pipeline per utterance."""
        while not self.ended:
            try:
                utt_item = await asyncio.wait_for(self._stt_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            if self.ended:
                break
            if len(utt_item) == 5:
                utt_id, audio_seg, speech_start, speech_end, vad_end = utt_item
            else:
                utt_id, audio_seg, speech_start, vad_end = utt_item
                speech_end = vad_end
            await self._emit_status("processing")
            await self._process_speech(utt_id, audio_seg, speech_start, speech_end, vad_end)
            if not self.ended and not self._ai_speaking:
                await self._emit_status("listening")

    async def _process_partial_speech(self, utt_id: str, audio_bytes: bytes, speech_start: Optional[float]):
        """Non-blocking live partial STT transcription for streaming frontend updates."""
        if self.ended or not audio_bytes or len(audio_bytes) < 4800:
            return
        try:
            res = await asyncio.to_thread(pcm8u_to_wav16, audio_bytes)
            wav_bytes = res[0] if isinstance(res, tuple) else res
            if not wav_bytes:
                return
            partial_text = await asyncio.to_thread(stt_service.transcribe_audio_bytes, wav_bytes)
            partial_text = partial_text.strip()
            from src.utils.text_sanitizer import is_whisper_hallucination
            if partial_text and not is_whisper_hallucination(partial_text):
                await self._emit_transcript_typed("CUSTOMER_PARTIAL", utt_id, partial_text)
        except Exception as exc:
            logger.debug(f"[PARTIAL_STT] {exc}")

    async def process_text(self, text: str):
        if self.ended or not text or not text.strip():
            return
        await self._run_ai_pipeline(text.strip())

    # ── STT ──────────────────────────────────────────────────────

    async def _process_speech(self, utt_id: str, audio_bytes: bytes, speech_start: float, speech_end: float, vad_end: float):
        if self.ended or getattr(self, "human_attended", False) or not audio_bytes or len(audio_bytes) < 320:
            return
        if utt_id in self._processed_utt_ids:
            logger.info(f"[STT] Duplicate utterance_id={utt_id} skipped")
            return
        self._processed_utt_ids.append(utt_id)  # Idempotent registration
        # Reset inactivity watchdog: customer is actively speaking
        self._last_activity_time = time.monotonic()

        t_stt_start = time.monotonic()
        metrics = {
            "call_id":      self.call_sid,
            "utt_id":       utt_id,
            "speech_start": speech_start,
            "speech_end":   speech_end,
            "vad_end":      vad_end,
            "stt_start":    t_stt_start,
            "vad_latency_ms": round((vad_end - speech_start) * 1000) if speech_start else 0,
            "vad_silence_ms": round((vad_end - speech_end) * 1000) if speech_end else 0,
        }
        try:
            t0        = time.monotonic()
            res = await asyncio.to_thread(pcm8u_to_wav16, audio_bytes, 8000, 16000, self.call_sid, utt_id)
            if isinstance(res, tuple):
                wav_bytes, diag = res
            else:
                wav_bytes, diag = res, {}

            if not wav_bytes:
                logger.warning(f"[STT] pcm8u_to_wav16 empty - utt_id={utt_id}")
                return

            audio_dur_s = diag.get("duration_s", round(len(audio_bytes) / 8000.0, 2))

            logger.info(
                f"\n[STT_INPUT]"
                f"\n  call_id={self.call_sid}"
                f"\n  utterance_id={utt_id}"
                f"\n  rtp_packet_count={diag.get('rtp_packet_count', len(audio_bytes)//160)}"
                f"\n  rtp_payload_type={diag.get('rtp_payload_type', 0)}"
                f"\n  decoded_sample_rate={diag.get('sample_rate', 16000)}"
                f"\n  pcm_format={diag.get('sample_format', 'PCM16_MONO')}"
                f"\n  channels={diag.get('channels', 1)}"
                f"\n  sample_count={diag.get('sample_count', 0)}"
                f"\n  duration={audio_dur_s:.2f}s"
                f"\n  rms={diag.get('rms', 0)}"
                f"\n  peak={diag.get('peak', 0)}"
                f"\n  min_sample={diag.get('min_sample', 0)}"
                f"\n  max_sample={diag.get('max_sample', 0)}"
                f"\n  zero_samples={diag.get('zero_samples', 0)}"
                f"\n  clipped_samples={diag.get('clipped_samples', 0)}"
                f"\n  first_sample={diag.get('first_sample', 0)}"
                f"\n  last_sample={diag.get('last_sample', 0)}"
                f"\n  audio_source=CUSTOMER_RTP"
            )

            transcription = await asyncio.to_thread(stt_service.transcribe_audio_bytes, wav_bytes)
            t_stt_end     = time.monotonic()
            stt_elapsed   = t_stt_end - t0
            metrics.update(
                stt_end=t_stt_end,
                stt_latency_ms=round(stt_elapsed * 1000),
                audio_dur_s=audio_dur_s,
                audio_bytes=len(audio_bytes),
            )
            logger.info(
                f"[STT_FINAL] id={utt_id} audio={audio_dur_s:.2f}s "
                f"stt={stt_elapsed*1000:.0f}ms text='{transcription}'"
            )
        except Exception as exc:
            logger.error(f"[STT ERROR] {utt_id}: {exc}")
            return

        text = transcription.strip()
        from src.utils.text_sanitizer import is_whisper_hallucination
        if not text or is_whisper_hallucination(text):
            logger.info(f"[STT] Hallucination discarded: '{text}' (buffer cleared, no AI response generated)")
            return

        now = time.monotonic()
        if text.lower() == self._last_transcript.lower() and (now - self._last_transcript_time) < 1.0:
            logger.info(f"[STT] Rapid duplicate within 1s ignored: '{text}'")
            return

        self._last_transcript      = text
        self._last_transcript_time = now

        # Structured debug logging per requirement
        logger.info(
            f"\n[VOICE_PIPELINE]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
            f"\n  speech_start={speech_start:.3f}"
            f"\n  speech_end={speech_end:.3f}"
            f"\n  customer_audio_duration={audio_dur_s:.2f}s"
            f"\n  stt_start={t0:.3f}"
            f"\n  stt_end={t_stt_end:.3f}"
            f"\n  stt_latency_ms={metrics['stt_latency_ms']}"
            f"\n  customer_final='{text}'"
            f"\n  audio_source=CUSTOMER_RTP"
        )
        logger.info(
            f"\n[CUSTOMER_FINAL]"
            f"\n  utterance_id={utt_id}"
            f"\n  audio_duration={audio_dur_s:.2f}s"
            f"\n  stt_latency={metrics['stt_latency_ms']} ms"
            f"\n  text='{text}'"
            f"\n  audio_source=CUSTOMER_RTP"
        )

        await self._emit_transcript_typed("CUSTOMER_FINAL", utt_id, text)

        try:
            from src.sockets.socket_manager import sio
            if wav_bytes and len(wav_bytes) > 320:
                await sio.emit("audio_chunk", {
                    "call_sid": self.call_sid,
                    "audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
                    "mime_type": "audio/wav",
                    "text": text, "speaker": "customer",
                    "utterance_id": utt_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            logger.warning(f"[AUDIO MONITOR] {e}")

        await self._run_ai_pipeline(text, metrics)

    # ── NLP + Booking + LLM ───────────────────────────────────────

    async def _run_ai_pipeline(self, transcription: str, metrics: Dict[str, Any] = None):
        if self.ended or not transcription or not transcription.strip():
            return
        async with self._pipeline_lock:
            if self.ended:
                return
            await self._run_ai_pipeline_locked(transcription, metrics)

    async def _run_ai_pipeline_locked(self, transcription: str, metrics: Dict[str, Any] = None):
        self._barge_in_event.clear()
        if getattr(self, "_active_stream_abort", None):
            try:
                self._active_stream_abort.set()
            except Exception:
                pass
        import threading
        stream_abort = threading.Event()
        self._active_stream_abort = stream_abort

        if metrics is None:
            metrics = {}
        t_ai_start = time.monotonic()
        metrics["ai_start"] = t_ai_start
        utt_id = metrics.get("utt_id", getattr(self, "_last_utt_id", "utt_?"))
        self._current_active_utt_id = utt_id

        # Stable turn idempotency check: process each genuinely new customer turn once
        if not hasattr(self, "_processed_turns"):
            self._processed_turns = set()
        turn_key = (self.call_sid, str(utt_id))
        if utt_id and utt_id != "utt_?" and turn_key in self._processed_turns:
            logger.info(f"[TURN_IDEMPOTENCY] Turn {turn_key} already processed. Suppressing duplicate processing.")
            return
        self._processed_turns.add(turn_key)

        # Generate a fresh unique response ID for this customer turn
        response_id = f"resp_{uuid.uuid4().hex[:8]}"
        self._current_response_id = response_id
        metrics["response_id"] = response_id

        logger.info(
            f"\n[CONVERSATION_RECEIVED]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
            f"\n  response_id={response_id}"
            f"\n  text='{transcription}'"
        )

        state_before = copy.deepcopy(self.booking_session.get_state_dict())
        logger.info(
            f"\n[STATE_BEFORE]"
            f"\n  state={state_before.get('state')}"
        )

        self.current_lang = language_service.determine_conversation_language(
            transcription, current_lang=self.current_lang)

        txt_lower = transcription.lower().strip()
        is_informational_query = (
            any(q in txt_lower for q in ["can you list", "what are", "who are", "which are", "tell me about", "where is", "where are", "how much", "what is", "who is", "list the", "list all", "list departments", "list teachers", "list doctors", "list services", "may i know", "can i know", "do you have", "what about"])
            or bool(re.search(r'^(?:what|who|which|how|where|when|can\s+you|could\s+you|tell\s+me|show|list)\b', txt_lower))
        )
        is_booking_keyword = any(k in txt_lower for k in ["book an appointment", "book appointment", "schedule an appointment", "make an appointment", "book a slot", "book my", "reserve", "want to book", "need to book", "i want an appointment", "i need an appointment"]) and not is_informational_query

        # 1. Immediate Intent & Emotion Detection
        intent_name = "general_question"
        emotion_name = "neutral"
        t_nlp0 = time.monotonic()
        try:
            nlp_res, emo_res = await asyncio.wait_for(
                asyncio.gather(
                    nlp_service.detect_intent_async(transcription),
                    emotion_service.detect_emotion_async(transcription),
                ), timeout=0.3)
            if nlp_res and hasattr(nlp_res, "intent"):
                intent_name = nlp_res.intent
            if emo_res and hasattr(emo_res, "emotion"):
                emotion_name = emo_res.emotion
        except Exception as exc:
            logger.warning(f"[NLP] Fallback: {exc}")
            intent_name = "general_inquiry"
        t_nlp1 = time.monotonic()
        metrics["intent_latency_ms"] = round((t_nlp1 - t_nlp0) * 1000)

        logger.info(
            f"\n[INTENT]"
            f"\n  intent={intent_name}"
        )

        logger.info(
            f"\n[AI_RESPONSE_TRIGGER]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
        )

        # Emit live AI telemetry to the monitor UI immediately
        try:
            from src.sockets.socket_manager import sio
            await sio.emit("ai_telemetry", {
                "call_sid": self.call_sid,
                "intent": intent_name,
                "emotion": emotion_name,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        except Exception:
            pass

        # 2. Check for Topic/Intent Switch vs Booking Flow
        # CRITICAL: Never let NLP intent misclassifications ("greeting", "general_question", etc.)
        # override an active booking state when the customer is answering a booking prompt (e.g. name, phone, email, date, time).
        is_intent_switch = (
            intent_name in ("emergency/urgent request", "transfer request")
            or (not self.booking_session.is_active() and (is_informational_query or intent_name in ("department_list", "doctor_list", "service_information", "general_question", "faq")))
            or booking_state_service.check_and_handle_intent_switch(self.booking_session, intent_name, transcription)
        )

        is_explicit_new_booking = any(w in txt_lower for w in ["new appointment", "another appointment", "book again", "book another", "one more appointment"])
        if self.booking_session.is_completed:
            if is_explicit_new_booking:
                self.booking_session.reset_for_new_booking()
                should_route_booking = True
            else:
                should_route_booking = False
        else:
            should_route_booking = not is_intent_switch and (self.booking_session.is_active() or is_booking_keyword)

        # 1b. Genuine End-of-Call Detection (Graceful Hangup after Thank You / Goodbye)
        is_closing = _is_genuine_closing_phrase(transcription)
        if self.booking_session.is_active() and not self.booking_session.is_completed:
            is_closing = any(w in txt_lower for w in ["goodbye", "bye bye", "hang up", "disconnect"])

        if is_closing:
            if not getattr(self, "_call_ending_scheduled", False):
                self._call_ending_scheduled = True
                org_title = rag_service.get_organization_name(self.organization_id) or "CallZenza"
                if self.current_lang in ("ta", "tanglish", "ta_mix"):
                    closing_msg = f"ரொம்ப நன்றி! {org_title}-ஐ தொடர்பு கொண்டதற்கு நன்றி. நல்ல நாளாக அமையட்டும், வணக்கம்!"
                elif self.current_lang == "hi":
                    closing_msg = f"Aapka bahut dhanyawad! {org_title} mein call karne ke liye shukriya. Aapka din shubh ho, alvida!"
                else:
                    closing_msg = f"You're very welcome! Thank you for calling {org_title}. Have a wonderful day, goodbye!"

                self.session_history.append({"role": "user", "text": transcription})
                self.session_history.append({"role": "model", "text": closing_msg})
                metrics["response_id"] = response_id

                # Speak final goodbye and await complete audio playback to caller
                await self._speak(closing_msg, "goodbye", metrics)

                # Only disconnect after final audio has finished playing to caller and no barge-in occurred
                if not self._barge_in_event.is_set() and not self.ended:
                    await asyncio.sleep(0.4)  # Small safety drain for RTP jitter buffer
                    if not self._barge_in_event.is_set() and not self.ended:
                        from src.services.sip_call_handler import terminate_call
                        await terminate_call(self.call_sid, reason="CALL_COMPLETED_AFTER_FAREWELL")
                return

        # ── Fast-Path Booking State Machine ────────────────────────
        if should_route_booking:
            t_state_0 = time.monotonic()
            response_text = booking_state_service.process_booking_turn(transcription, self.booking_session, turn_id=utt_id)
            t_state_1 = time.monotonic()
            state_ms = round((t_state_1 - t_state_0) * 1000)

            if not response_text:
                logger.info(f"[BOOKING] Empty response suppressed for '{transcription}'")
                return

            logger.info(
                f"\n[CONVERSATION_TURN]"
                f"\n  call_id={self.call_sid}"
                f"\n  utterance_id={utt_id}"
                f"\n  response_id={response_id}"
                f"\n  source=AI"
                f"\n  speaker=ai"
                f"\n  event_type=AI_RESPONSE"
            )

            logger.info(
                f"\n[AI_RESPONSE]"
                f"\n  call_id={self.call_sid}"
                f"\n  utterance_id={utt_id}"
                f"\n  response_id={response_id}"
                f"\n  text='{response_text}'"
            )

            intent_name = "appointment_booking"
            self._messages.append({"role": "user", "text": transcription, "intent": intent_name})
            self.session_history.append({"role": "user", "text": transcription})
            self.session_history.append({"role": "model", "text": response_text})
            asyncio.create_task(asyncio.to_thread(self._log_to_db, "user", transcription, intent_name))

            metrics["response_id"] = response_id
            metrics["ai_end"]     = t_state_1
            metrics["ai_latency"] = t_state_1 - t_ai_start
            metrics["state_ms"]   = state_ms

            await self._speak(response_text, intent_name, metrics)
            return

        # ── General Inquiry / Active RAG Path ─────────────────────
        self._messages.append({"role": "user", "text": transcription, "intent": intent_name})
        asyncio.create_task(asyncio.to_thread(self._log_to_db, "user", transcription, intent_name))

        # RAG (skip for non-informational intents and conversational pleasantries)
        context = ""
        txt_lower_q = transcription.lower().strip()
        pure_pleasantries = {"greeting", "goodbye", "thanks", "affirmation", "negation", "emergency/urgent request", "transfer request"}
        is_informational_query = any(w in txt_lower_q for w in [
            "what", "when", "where", "who", "which", "how", "doctor", "stylist", "staff",
            "service", "treatment", "price", "cost", "fee", "hours", "timing", "open", "close",
            "location", "address", "policy", "appointment", "schedule", "menu", "facilities",
            "tell", "know", "detail", "details", "info", "information", "help", "consult",
            "consultation", "procedure", "package", "offer", "available", "can i", "do you"
        ])
        dir_scope = "outbound" if getattr(self, "call_type", None) == CallType.OUTBOUND else "inbound"
        active_doc_for_rag = rag_service._resolve_active_document_id(
            self.organization_id,
            document_id=(self.active_script_id or self.active_knowledge_id),
            scope=dir_scope
        )
        is_rag_required = bool(active_doc_for_rag) and (is_informational_query or (intent_name not in pure_pleasantries and intent_name != "unknown"))

        script_used_str = (
            "OUTBOUND_SCRIPT" if getattr(self, "outbound_script", None)
            else (f"ACTIVE_SCRIPT_{self.active_script_id}" if getattr(self, "active_script_id", None)
            else (f"ACTIVE_KNOWLEDGE_{self.active_knowledge_id}" if getattr(self, "active_knowledge_id", None)
            else "NONE"))
        )

        logger.info(
            f"\n[AI_DECISION]"
            f"\n  intent={intent_name}"
            f"\n  rag_required={is_rag_required}"
            f"\n  script_used={script_used_str}"
        )

        if is_rag_required:
            t_rag_0 = time.monotonic()
            metrics["rag_start"] = t_rag_0
            try:
                context = await asyncio.wait_for(
                    asyncio.to_thread(
                        rag_service.retrieve_context,
                        query=transcription,
                        n_results=3,
                        organization_id=self.organization_id,
                        intent=intent_name,
                        document_id=active_doc_for_rag,
                        call_id=self.call_sid,
                        utterance_id=metrics.get("utt_id")
                    ),
                    timeout=3.5
                )
            except Exception as exc:
                logger.warning(f"[RAG] Fallback to master context: {exc}")
                context = rag_service.get_organization_master_context(self.organization_id, document_id=active_doc_for_rag)
            
            if not context:
                context = rag_service.get_organization_master_context(self.organization_id, document_id=active_doc_for_rag)

            t_rag_1 = time.monotonic()
            metrics["rag_end"] = t_rag_1
            metrics["rag_latency_ms"] = round((t_rag_1 - t_rag_0) * 1000)

        # LLM - run synchronous stream in thread pool and stream sentences to TTS in real time
        try:
            self.session_history.append({"role": "user", "text": transcription})
            t_llm_start = time.monotonic()
            metrics["llm_start"] = t_llm_start

            # Capture all kwargs for the thread
            _gen_kwargs = dict(
                text=transcription, context=context,
                session_history=list(self.session_history),
                intent=intent_name, emotion=emotion_name,
                conversation_language=self.current_lang,
                organization_id=self.organization_id,
                conversation_state=self.booking_session.get_state_dict(),
                outbound_script=getattr(self, "outbound_script", None),
            )

            sentence_queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            response_id = metrics.get("response_id") or getattr(self, "_current_response_id", None) or f"resp_{uuid.uuid4().hex[:8]}"
            self._current_response_id = response_id
            metrics["response_id"] = response_id

            _ABBREVIATIONS = {
                "dr.", "mr.", "mrs.", "ms.", "prof.", "vs.", "etc.", "e.g.", "i.e.",
                "no.", "sr.", "jr.", "dept.", "approx.", "o.", "a.m.", "p.m.", "am.", "pm."
            }

            def _stream_llm_sentences(**kw):
                """Worker thread: stream tokens → extract safe complete sentences → enqueue."""
                import re as _re
                buffer = ""
                try:
                    for tok in ai_service.generate_response_stream(**kw):
                        if self.ended or self._barge_in_event.is_set() or stream_abort.is_set():
                            return
                        if loop.is_closed():
                            return
                        buffer += tok

                        # Find sentence punctuation [.!?] followed by whitespace or end
                        delims = list(_re.finditer(r'([.!?]+)(?:\s+|$)', buffer))
                        if not delims:
                            continue

                        last_end = 0
                        accum = ""
                        for d in delims:
                            cand = buffer[last_end:d.end()].strip()
                            words = cand.split()
                            if not words:
                                continue
                            if words[-1].lower() in _ABBREVIATIONS:
                                continue

                            full_s = (accum + " " + cand).strip() if accum else cand
                            if len(full_s.split()) >= 3 and len(full_s) >= 15:
                                if not loop.is_closed() and loop.is_running() and not stream_abort.is_set():
                                    try:
                                        clean_s = clean_markdown_for_voice(full_s)
                                        if clean_s:
                                            loop.call_soon_threadsafe(sentence_queue.put_nowait, clean_s)
                                    except RuntimeError:
                                        return
                                accum = ""
                                last_end = d.end()
                            else:
                                accum = full_s
                                last_end = d.end()

                        buffer = (accum + " " + buffer[last_end:]).strip() if accum else buffer[last_end:]

                    # Stream finished: check residual buffer
                    if buffer.strip() and not loop.is_closed() and loop.is_running() and not stream_abort.is_set():
                        cand_final = clean_markdown_for_voice(buffer.strip())
                        # Only enqueue if it forms a substantive thought (>=3 words), avoiding fragments like 'On'
                        if cand_final and len(cand_final.split()) >= 3 and len(cand_final) >= 12:
                            try:
                                loop.call_soon_threadsafe(sentence_queue.put_nowait, cand_final)
                            except RuntimeError:
                                return
                except Exception as _e:
                    logger.warning(f"[LLM_THREAD] Stream error: {_e}")
                finally:
                    if not loop.is_closed() and loop.is_running() and not stream_abort.is_set():
                        try:
                            loop.call_soon_threadsafe(sentence_queue.put_nowait, None)
                        except RuntimeError:
                            pass

            import threading
            threading.Thread(target=_stream_llm_sentences, kwargs=_gen_kwargs, daemon=True).start()

            first_token_recorded = False
            full_response_parts = []
            sentence_index = 0

            while True:
                sentence = await sentence_queue.get()
                if sentence is None:
                    break

                if not first_token_recorded:
                    t_token = time.monotonic()
                    metrics["llm_first_token"] = t_token
                    metrics["llm_latency_ms"]  = round((t_token - t_llm_start) * 1000)
                    metrics["llm_first_token_ms"] = metrics["llm_latency_ms"]
                    first_token_recorded = True

                full_response_parts.append(sentence)

                if self.ended or self._barge_in_event.is_set() or stream_abort.is_set():
                    logger.info(f"[LLM] Call ended or barge-in — aborting stream")
                    break

                # ── Speak each sentence as it arrives (sentence-streaming TTS) ──
                # Keep utt_id, response_id and speech endpoints in sent_metrics across all chunks
                sent_metrics = {
                    "is_chunk": True,
                    "response_id": response_id,
                    "utt_id": utt_id,
                    "speech_end_monotonic": metrics.get("speech_end_monotonic"),
                    "t_speech_endpoint": metrics.get("t_speech_endpoint"),
                    "t_stt_final": metrics.get("t_stt_final"),
                    "endpointing_ms": metrics.get("endpointing_ms", 300),
                    "finalization_latency_ms": metrics.get("finalization_latency_ms", 0),
                    "intent_latency_ms": metrics.get("intent_latency_ms", 0),
                    "rag_latency_ms": metrics.get("rag_latency_ms", 0),
                    "llm_latency_ms": metrics.get("llm_latency_ms", 0),
                    "audio_duration_s": metrics.get("audio_duration_s", 1.0),
                }
                if sentence_index == 0:
                    for k, v in metrics.items():
                        if k not in sent_metrics:
                            sent_metrics[k] = v
                sentence_index += 1
                await self._speak(sentence, intent_name, sent_metrics)

                if self.ended or self._barge_in_event.is_set() or stream_abort.is_set():
                    break

            full_response = " ".join(full_response_parts).strip()
            metrics["ai_end"]        = time.monotonic()
            metrics["ai_latency_ms"] = round((metrics["ai_end"] - t_ai_start) * 1000)
            metrics["rag_used"]      = bool(context and len(context) > 0)

            # Fallback if empty and no barge-in occurred
            if not full_response and not self._barge_in_event.is_set() and not self.ended:
                org_name = rag_service.get_organization_name(self.organization_id)
                if org_name:
                    full_response = f"Thank you for contacting {org_name}. How may I assist you today?"
                else:
                    full_response = "Thank you for contacting us. How may I assist you today?"
                fallback_metrics = dict(metrics)
                fallback_metrics["response_id"] = response_id
                fallback_metrics["is_chunk"] = False
                await self._speak(full_response, intent_name, fallback_metrics)

            logger.info(
                f"\n[AI_TURN_DEDUP]"
                f"\n  call_id={self.call_sid}"
                f"\n  utterance_id={utt_id}"
                f"\n  response_id={response_id}"
                f"\n  processing_state=PROCESSED_ONCE"
            )

            if not full_response:
                logger.warning(f"[LLM] Empty response for: '{transcription[:60]}'")
                self.session_history.append({"role": "model", "text": ""})
            else:
                logger.info(
                    f"\n[CONVERSATION_TURN]"
                    f"\n  call_id={self.call_sid}"
                    f"\n  utterance_id={utt_id}"
                    f"\n  response_id={response_id}"
                    f"\n  source=AI"
                    f"\n  speaker=ai"
                    f"\n  event_type=AI_RESPONSE"
                )
                logger.info(
                    f"\n[AI_RESPONSE]"
                    f"\n  call_id={self.call_sid}"
                    f"\n  utterance_id={utt_id}"
                    f"\n  response_id={response_id}"
                    f"\n  text='{full_response}'"
                )
                self.session_history.append({"role": "model", "text": full_response})

                # Emit single authoritative final AI_RESPONSE event for the logical turn
                try:
                    from src.sockets.socket_manager import sio
                    final_payload = {
                        "call_sid": self.call_sid,
                        "call_id": self.call_sid,
                        "conversation_id": self.session_id,
                        "utterance_id": f"ai_{utt_id}" if not str(utt_id).startswith("ai_") else str(utt_id),
                        "response_id": response_id,
                        "type": "AI_RESPONSE",
                        "event_type": "AI_RESPONSE",
                        "source": "AI",
                        "audio_source": "AI_TTS",
                        "text": full_response,
                        "intent": intent_name,
                        "speaker": "ai",
                        "role": "assistant",
                        "is_partial": False,
                        "is_final": True,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "rag_source": "RAG" if (metrics and metrics.get("rag_used")) else "Active Knowledge Base",
                    }
                    await sio.emit("ai_response", final_payload)
                    await sio.emit("transcript_event", final_payload)
                except Exception as _em_err:
                    logger.debug(f"[FINAL_AI_EMIT_ERR] {_em_err}")

            # Emit pipeline_latency event via Socket.IO for live frontend telemetry
            try:
                from src.sockets.socket_manager import sio
                stt_lat = metrics.get("stt_latency_ms", 0)
                rag_lat = metrics.get("rag_latency_ms", 0)
                llm_lat = metrics.get("llm_latency_ms", 0)
                tts_lat = metrics.get("tts_latency_ms", 0)
                tot_cust_to_ai = metrics.get("total_customer_to_speech_ms", round((time.monotonic() - t_ai_start) * 1000))

                latency_payload = {
                    "call_sid": self.call_sid,
                    "call_id": self.call_sid,
                    "utterance_id": utt_id,
                    "stt_latency_ms": stt_lat,
                    "rag_latency_ms": rag_lat,
                    "llm_latency_ms": llm_lat,
                    "tts_latency_ms": tts_lat,
                    "total_latency_ms": tot_cust_to_ai,
                    "customer_stop_to_ai_speech_ms": tot_cust_to_ai,
                    "target_met": (tot_cust_to_ai <= 2000),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                await sio.emit("pipeline_latency", latency_payload)
            except Exception as _l_err:
                logger.warning(f"[LATENCY_EMIT] {_l_err}")

            # After full AI response: clear speaking state, ready for next customer turn
            if not self.ended:
                self._ai_speaking = False
                self._last_ai_speak_end_time = time.monotonic()
                await self._emit_status("listening")
        except Exception as exc:
            logger.error(f"[LLM] {exc}")
            if not self.ended:
                await self._speak("I'm here to help. How can I assist you?", intent_name, metrics)

    # ── TTS + RTP TX ──────────────────────────────────────────────

    async def _speak(self, text: str, intent: str = "", metrics: Dict[str, Any] = None):
        """
        Synthesise TTS and send to:
        1. Phone via pyVoIP RTP TX as PCMU (mu-law u8)
        2. Browser via Socket.IO (base64 WAV/MP3)
        3. UI transcript via Socket.IO

        CODEC PATH (critical):
            TTS (WAV/MP3) -> PCM16 8kHz mono -> audioop.lin2ulaw -> PCMU u8 -> pyVoIP
        """
        if not text or self.ended:
            return

        async with self._speak_lock:
            if self.ended:
                return
            await self._speak_locked(text, intent, metrics)

    async def _speak_locked(self, text: str, intent: str = "", metrics: Dict[str, Any] = None):
        # 1. Reset Deepgram STT turn buffer so previous acoustic frames never convert to customer final
        if hasattr(self, "deepgram_session") and self.deepgram_session:
            self.deepgram_session.reset_turn()

        # 2. Register AI text in recent speech history ring
        self._recent_ai_texts.append(text)
        try:
            from src.sockets.socket_manager import sio
            await self._emit_status("speaking")
            self._ai_speaking = True
            self._barge_in_event.clear()
            if metrics and "tts_start" not in metrics:
                metrics["tts_start"] = time.monotonic()

            t0          = time.monotonic()
            audio_bytes = await tts_service.synthesize_speech_async(text, language=self.current_lang)
            elapsed     = time.monotonic() - t0
            if metrics and "tts_first_audio" not in metrics:
                metrics["tts_first_audio"] = time.monotonic()
                metrics["tts_latency_ms"]  = round(elapsed * 1000)

            if not audio_bytes:
                logger.warning(f"[TTS] No audio for: '{text[:50]}'")
                self._ai_speaking = False
                self._last_ai_speak_end_time = time.monotonic()
                return
            logger.info(f"[TTS] source=AI {len(audio_bytes)} bytes in {elapsed:.2f}s: '{text[:60]}'")

            # Browser monitoring + UI transcript (emit BEFORE phone send for minimal perceived latency)
            ts = datetime.now(timezone.utc).isoformat()
            active_id = getattr(self, "_current_active_utt_id", None) or (metrics.get("utt_id") if metrics else None)
            if active_id:
                ai_utt_id = f"ai_{active_id}" if not str(active_id).startswith("ai_") else str(active_id)
            else:
                ai_utt_id = f"ai_{uuid.uuid4().hex[:8]}"
            utt_id = ai_utt_id
            try:
                if len(audio_bytes) > 100:
                    b64  = base64.b64encode(audio_bytes).decode("ascii")
                    mime = "audio/wav" if audio_bytes[:4] == b"RIFF" else "audio/mpeg"
                    payload_audio = {
                        "call_sid": self.call_sid,
                        "call_id": self.call_sid,
                        "conversation_id": self.session_id,
                        "audio_base64": b64,
                        "mime_type": mime,
                        "text": text,
                        "intent": intent,
                        "speaker": "ai",
                        "source": "AI",
                        "audio_source": "AI_TTS_MONITOR",
                        "utterance_id": utt_id,
                        "timestamp": ts,
                    }
                    await sio.emit("ai_audio_chunk", payload_audio)
                    logger.info(
                        f"\n[AI_LAPTOP_MONITOR]"
                        f"\n  call_id={self.call_sid}"
                        f"\n  source=AI"
                        f"\n  audio_source=AI_TTS_MONITOR"
                        f"\n  bytes={len(audio_bytes)}"
                        f"\n  sample_rate=8000"
                        f"\n  duration_ms={round(len(audio_bytes)/16)}"
                    )

                is_chunk = bool(metrics and metrics.get("is_chunk"))
                resp_id = (metrics.get("response_id") if metrics else None) or getattr(self, "_current_response_id", None) or f"resp_{uuid.uuid4().hex[:8]}"
                payload_ai = {
                    "call_sid": self.call_sid,
                    "call_id": self.call_sid,
                    "conversation_id": self.session_id,
                    "utterance_id": utt_id,
                    "response_id": resp_id,
                    "type": "AI_CHUNK" if is_chunk else "AI_RESPONSE",
                    "event_type": "AI_CHUNK" if is_chunk else "AI_RESPONSE",
                    "source": "AI",
                    "audio_source": "AI_TTS",
                    "text": text,
                    "intent": intent,
                    "speaker": "ai",
                    "role": "assistant",
                    "is_partial": is_chunk,
                    "is_final": not is_chunk,
                    "timestamp": ts,
                    "rag_source": "RAG" if (metrics and metrics.get("rag_used")) else "Active Knowledge Base",
                }
                if is_chunk:
                    await sio.emit("ai_chunk", payload_ai)
                    await sio.emit("transcript_event", payload_ai)
                    logger.info(
                        f"\n[LIVE_CONVERSATION_EVENT]"
                        f"\n  call_id={self.call_sid}"
                        f"\n  utterance_id={utt_id}"
                        f"\n  response_id={resp_id}"
                        f"\n  source=AI"
                        f"\n  speaker=ai"
                        f"\n  event_type=AI_CHUNK"
                    )
                else:
                    await sio.emit("ai_response", payload_ai)
                    await sio.emit("transcript_event", payload_ai)
                    logger.info(
                        f"\n[LIVE_CONVERSATION_EVENT]"
                        f"\n  call_id={self.call_sid}"
                        f"\n  utterance_id={utt_id}"
                        f"\n  response_id={resp_id}"
                        f"\n  source=AI"
                        f"\n  speaker=ai"
                        f"\n  event_type=AI_RESPONSE"
                    )
            except Exception as _se:
                logger.warning(f"[SOCKET_EMIT] {_se}")

            # Phone - PCMU via RTP TX (paced 20ms frames to prevent VPS jitter/buffer overflow)
            if self.audio_send_callback and not self._barge_in_event.is_set():
                try:
                    from src.config.env import settings
                    tts_provider = getattr(settings, "TTS_PROVIDER", "murf").lower().strip()
                    pcmu_bytes, pcm16_audio = await asyncio.to_thread(convert_tts_to_pcmu_and_pcm16, audio_bytes, tts_provider)
                    if pcmu_bytes and not self._barge_in_event.is_set():
                        audio_play_dur = len(pcmu_bytes) / 8000.0
                        now = time.monotonic()
                        # Playback window
                        self._ai_speaking_until = now + audio_play_dur + 0.15

                        if metrics and "rtp_tx_start" not in metrics:
                            t_rtp_start = now
                            metrics["rtp_tx_start"] = t_rtp_start
                            t_speech_endpoint = metrics.get("t_speech_endpoint", metrics.get("speech_end_monotonic", metrics.get("t_stt_final", now)))
                            t_audio_rx = metrics.get("t_audio_rx", t_speech_endpoint - metrics.get("audio_duration_s", 1.0))
                            t_stt_partial = metrics.get("t_stt_partial", t_audio_rx + 0.15)
                            t_stt_final = metrics.get("t_stt_final", t_speech_endpoint)
                            t_tts_done = metrics.get("tts_first_audio", now)

                            endpoint_dur = metrics.get("endpointing_ms", 300)
                            first_part = metrics.get("first_partial_ms", round((t_stt_partial - t_audio_rx) * 1000) if (t_stt_partial and t_audio_rx) else 150)
                            final_stt  = metrics.get("finalization_latency_ms", metrics.get("final_transcript_ms", metrics.get("stt_latency_ms", round((t_stt_final - t_speech_endpoint) * 1000) if (t_stt_final >= t_speech_endpoint) else 150)))
                            rag_ms     = metrics.get("rag_ms", metrics.get("rag_latency_ms", 0))
                            llm_ms     = metrics.get("llm_ms", metrics.get("llm_latency_ms", metrics.get("state_ms", 0)))
                            intent_ms  = metrics.get("intent_latency_ms", 0)
                            tts_ms     = metrics.get("tts_first_audio_ms", metrics.get("tts_latency_ms", round((t_tts_done - metrics.get("tts_start", t_tts_done)) * 1000)))
                            rtp_ms     = max(1, round((t_rtp_start - t_tts_done) * 1000))
                            cust_to_ai = max(1, round((t_rtp_start - t_speech_endpoint) * 1000)) if t_speech_endpoint else 0

                            metrics["total_customer_to_speech_ms"] = cust_to_ai

                            logger.info(
                                f"\n[STAGE_LATENCY]"
                                f"\n  call_id={self.call_sid}"
                                f"\n  utterance_id={utt_id}"
                                f"\n  stage=audio_frame_rx duration_ms=20 status=SUCCESS"
                                f"\n  stage=stt_partial duration_ms={first_part} status=SUCCESS"
                                f"\n  stage=customer_speech_endpoint duration_ms={endpoint_dur} status=SUCCESS"
                                f"\n  stage=stt_final duration_ms={final_stt} status=SUCCESS"
                                f"\n  stage=intent_detection duration_ms={intent_ms} status=SUCCESS"
                                f"\n  stage=rag_retrieval duration_ms={rag_ms} status=SUCCESS"
                                f"\n  stage=llm_first_usable_text duration_ms={llm_ms} status=SUCCESS"
                                f"\n  stage=tts_first_audio duration_ms={tts_ms} status=SUCCESS"
                                f"\n  stage=ai_audio_rtp_tx duration_ms={rtp_ms} status=SUCCESS"
                                f"\n  stage=customer_stop_to_ai_speech duration_ms={cust_to_ai} status=SUCCESS"
                            )

                            logger.info(
                                f"\n[PIPELINE_LATENCY_REPORT]"
                                f"\n  call_id={self.call_sid}"
                                f"\n  utterance_id={utt_id}"
                                f"\n  1_vad_endpoint_ms={endpoint_dur}"
                                f"\n  2_stt_final_ms={final_stt}"
                                f"\n  3_intent_ms={intent_ms}"
                                f"\n  4_rag_ms={rag_ms}"
                                f"\n  5_llm_ms={llm_ms}"
                                f"\n  6_tts_ms={tts_ms}"
                                f"\n  7_rtp_ms={rtp_ms}"
                                f"\n  customer_speech_end_to_first_ai_audio_ms={cust_to_ai}"
                                f"\n  target_met={cust_to_ai <= 2000}"
                            )

                            if cust_to_ai > 2000:
                                stage_breakdown = {
                                    "vad_endpoint": endpoint_dur,
                                    "stt_final": final_stt,
                                    "intent_detection": intent_ms,
                                    "rag_retrieval": rag_ms,
                                    "llm_first_usable_text": llm_ms,
                                    "tts_first_audio": tts_ms,
                                    "ai_audio_rtp_tx": rtp_ms,
                                }
                                slowest_stage, slowest_ms = max(stage_breakdown.items(), key=lambda kv: kv[1])
                                logger.warning(
                                    f"[LATENCY_TARGET_EXCEEDED] Total response latency {cust_to_ai}ms > 2000ms target! "
                                    f"Bottleneck: {slowest_stage} ({slowest_ms}ms). Breakdown: {stage_breakdown}"
                                )

                            logger.info(
                                f"[TTS_FIRST_AUDIO] call_id={self.call_sid} utterance_id={utt_id} timestamp={time.time():.4f}"
                            )

                            try:
                                from src.sockets.socket_manager import sio
                                latency_payload = {
                                    "call_sid": self.call_sid,
                                    "call_id": self.call_sid,
                                    "utterance_id": utt_id,
                                    "audio_frame_rx_ms": 20,
                                    "stt_partial_ms": first_part,
                                    "customer_speech_endpoint_ms": endpoint_dur,
                                    "stt_final_ms": final_stt,
                                    "intent_latency_ms": intent_ms,
                                    "rag_latency_ms": rag_ms,
                                    "llm_latency_ms": llm_ms,
                                    "tts_latency_ms": tts_ms,
                                    "rtp_tx_latency_ms": rtp_ms,
                                    "total_latency_ms": cust_to_ai,
                                    "customer_stop_to_ai_speech_ms": cust_to_ai,
                                    "target_met": (cust_to_ai <= 2000),
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                }
                                await sio.emit("pipeline_latency", latency_payload)
                            except Exception as _em_err:
                                logger.warning(f"[LATENCY_EMIT] {_em_err}")

                        logger.info(f"[RTP_TX] source=AI_TTS Sending {len(pcmu_bytes)} PCMU bytes ({audio_play_dur:.2f}s) to phone")
                        cb = self.audio_send_callback
                        if cb:
                            try:
                                cb(pcmu_bytes)
                            except Exception as _cb_err:
                                logger.warning(f"[RTP_TX] Callback write error: {_cb_err}")

                        # Feed AI speech to TwoWayCallRecorder for synchronized recording
                        if hasattr(self, "two_way_recorder") and pcm16_audio:
                            self.two_way_recorder.feed_ai_pcm16(pcm16_audio)

                        # Yield event-loop time during the audio playback window, allowing instant barge-in
                        if audio_play_dur > 0.05:
                            try:
                                await asyncio.wait_for(self._barge_in_event.wait(), timeout=audio_play_dur)
                                logger.info("[BARGE_IN] Customer spoke - interrupted playback sleep")
                                if self.audio_clear_callback:
                                    try:
                                        self.audio_clear_callback()
                                    except Exception:
                                        pass
                            except asyncio.TimeoutError:
                                pass

                        # Mark end of this audio chunk for echo guard
                        self._ai_speaking = False
                        self._last_ai_speak_end_time = time.monotonic()
                        self._ai_speaking_until = time.monotonic() + 0.15

                    elif self._barge_in_event.is_set():
                        logger.info("[BARGE_IN] Customer spoke - dropping AI audio")
                        self._ai_speaking = False
                        self._last_ai_speak_end_time = time.monotonic()
                except Exception as e:
                    logger.warning(f"[RTP_TX] Send error: {e}")
                    self._ai_speaking = False
                    self._last_ai_speak_end_time = time.monotonic()

        except Exception as exc:
            logger.error(f"[TTS] Error: {exc}")
        finally:
            self._ai_speaking = False
            self._last_ai_speak_end_time = time.monotonic()
            if not self.ended:
                await self._emit_status("listening")

        utt_id = metrics.get("utt_id", "utt_?") if metrics else "utt_?"
        logger.info(
            f"\n[AI_RESPONSE_TEXT]"
            f"\n  call_id={self.call_sid}"
            f"\n  utterance_id={utt_id}"
            f"\n  intent={intent}"
            f"\n  source=ACTIVE_SCRIPT_RAG"
            f"\n  text='{text}'"
        )

        self._messages.append({"role": "assistant", "text": text})
        asyncio.create_task(asyncio.to_thread(self._log_to_db, "assistant", text, intent))

    async def _send_opening_greeting(self):
        try:
            doc_to_use = self.active_script_id or self.active_knowledge_id
            greeting = rag_service.get_organization_greeting(self.organization_id, document_id=doc_to_use)
            if not greeting or not greeting.strip():
                greeting = "Hello. How may I assist you today?"

            logger.info(
                f"\n[ACTIVE_CONTEXT]"
                f"\n  organization_id={self.organization_id}"
                f"\n  agent_id={self.agent_id}"
                f"\n  script_id={self.active_script_id}"
                f"\n  knowledge_id={self.active_knowledge_id}"
                f"\n  organization_name_source=ACTIVE_SCRIPT_RAG"
            )
            logger.info(
                f"\n[AI_GREETING]"
                f"\n  call_id={self.call_sid}"
                f"\n  context_used=true"
                f"\n  hardcoded_business_data=false"
                f"\n  text='{greeting}'"
            )
            if greeting:
                self.booking_session.greeting_sent = True
                self.booking_session.conversation_started = True
                if not str(self.caller_name).startswith("Outbound"):
                    logger.info(f"[INBOUND AI STARTED] Playing AI greeting for call_sid={self.call_sid}: '{greeting[:60]}...'")
                await self._speak(greeting, "greeting", {"utt_id": "utt_greeting"})
        except Exception as exc:
            logger.warning(f"[GREETING] {exc}")
        finally:
            self._ai_speaking = False
            self._last_ai_speak_end_time = time.monotonic()
            self._ai_speaking_until = time.monotonic() + 0.15
            if not self.ended:
                await self._emit_status("listening")

    # ── WebSocket emitters ────────────────────────────────────────

    async def _emit_transcript(self, speaker: str, text: str):
        try:
            from src.sockets.socket_manager import sio
            source = "CUSTOMER" if speaker == "customer" else "AI"
            audio_source = "CUSTOMER_RTP" if source == "CUSTOMER" else "AI_TTS"
            await sio.emit("transcript_event", {
                "call_sid": self.call_sid,
                "call_id": self.call_sid,
                "conversation_id": self.session_id,
                "speaker": speaker,
                "source": source,
                "audio_source": audio_source,
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
        except Exception as exc:
            logger.warning(f"[TRANSCRIPT] {exc}")

    async def _emit_transcript_typed(self, event_type: str, utt_id: str, text: str, extra_meta: Optional[Dict[str, Any]] = None):
        """Emit structured transcript event: CUSTOMER_PARTIAL / CUSTOMER_FINAL / AI_RESPONSE / etc."""
        try:
            from src.sockets.socket_manager import sio
            speaker = "customer" if event_type.startswith("CUSTOMER") else "ai"
            source = "CUSTOMER" if speaker == "customer" else "AI"
            audio_source = "CUSTOMER_RTP" if source == "CUSTOMER" else "AI_TTS"
            
            # Server-side validation: Reject any customer event that contains AI-generated speech
            if source == "CUSTOMER" and self._is_ai_echo(text):
                logger.warning(f"[CRITICAL] AI_AUDIO_OR_EVENT_LEAKED_INTO_CUSTOMER_STT Discarded customer STT matching recent AI speech: '{text}' utt_id={utt_id}")
                return

            ts      = datetime.now(timezone.utc).isoformat()
            is_partial = (event_type == "CUSTOMER_PARTIAL")
            role    = "customer" if speaker == "customer" else "assistant"
            payload = {
                "call_sid": self.call_sid,
                "call_id": self.call_sid,
                "conversation_id": self.session_id,
                "utterance_id": utt_id,
                "type": event_type,
                "event_type": event_type,
                "text": text,
                "speaker": speaker,
                "role": role,
                "source": source,
                "audio_source": audio_source,
                "is_partial": is_partial,
                "is_final": not is_partial,
                "timestamp": ts,
            }
            if extra_meta:
                payload.update(extra_meta)

            await sio.emit("transcript_event", payload)
            await sio.emit("call_transcript", payload)
            if speaker == "customer":
                await sio.emit("customer_transcript", payload)

            emit_tag = "CUSTOMER_PARTIAL_EMIT" if is_partial else "CUSTOMER_FINAL_EMIT"
            logger.info(
                f"\n[{emit_tag}]"
                f"\n  call_id={self.call_sid}"
                f"\n  utterance_id={utt_id}"
                f"\n  event_name=transcript_event"
                f"\n  delivery_result=DELIVERED"
            )
            logger.info(
                f"\n[LIVE_CONVERSATION_EVENT]"
                f"\n  call_id={self.call_sid}"
                f"\n  utterance_id={utt_id}"
                f"\n  source={source}"
                f"\n  speaker={speaker}"
                f"\n  event_type={event_type}"
            )
        except Exception as exc:
            logger.warning(f"[TRANSCRIPT_EVENT] {exc}")

    async def _emit_status(self, phase: str):
        try:
            from src.sockets.socket_manager import sio
            await sio.emit("call_status_event", {
                "call_sid": self.call_sid, "phase": phase,
                "timestamp": datetime.now(timezone.utc).isoformat()})
        except Exception:
            pass

    # ── Database ──────────────────────────────────────────────────

    async def _create_call_log(self):
        try:
            db = SessionLocal()
            try:
                from src.models.call_log import CallLog
                call = None
                if self.db_call_id:
                    call = db.query(CallLog).filter(CallLog.id == self.db_call_id).first()
                elif self.call_sid:
                    call = db.query(CallLog).filter(CallLog.call_sid == self.call_sid).first()
                    if call:
                        self.db_call_id = call.id

                direction_label = "OUTBOUND" if self.call_type == CallType.OUTBOUND else "INBOUND"

                if not call:
                    svc  = CallService(db)
                    call = svc.create_from_twilio({
                        "call_sid": self.call_sid,
                        "organization_id": self.organization_id,
                        "caller_phone": self.caller_phone,
                        "caller_name": self.caller_name,
                        "call_type": self.call_type,
                        "status": "in-progress",
                        "recording_status": "RECORDING",
                        "duration": "00:00",
                        "messages": "[]",
                        "receptionist_state": f"SIP {direction_label} - CONNECTED"
                    })
                    self.db_call_id = call.id
                else:
                    call.call_type = self.call_type
                    call.status = "in-progress"
                    call.recording_status = "RECORDING"
                    call.receptionist_state = f"SIP {direction_label} - CONNECTED"
                    db.commit()

                if getattr(call, "outbound_script", None):
                    self.outbound_script = call.outbound_script
                    logger.info(f"[SIP CALL] Loaded outbound script for call {self.call_sid}")
            finally:
                db.close()
        except Exception as exc:
            logger.error(f"[DB] Create call log failed: {exc}")

    def _finalise_call_log_sync(self):
        with self._finalise_lock:
            if self._call_log_finalised:
                return
            self._call_log_finalised = True

            try:
                import json, wave, os
                duration_secs = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
                mins, secs    = divmod(duration_secs, 60)
                duration_str  = f"{mins:02d}:{secs:02d}"
                rec_url = None
                rec_file_size = 0
                rec_duration_sec = 0
                rec_status = "COMPLETED"
                rec_err = None

                try:
                    rec_bytes = None
                    if hasattr(self, "two_way_recorder") and self.two_way_recorder:
                        rec_bytes = self.two_way_recorder.finalize()

                    if not rec_bytes and self._recording_pcm_frames:
                        buf = io.BytesIO()
                        with wave.open(buf, "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(8000)
                            wf.writeframes(bytes(self._recording_pcm_frames))
                        rec_bytes = buf.getvalue()

                    if rec_bytes and len(rec_bytes) > 44:
                        rec_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "uploads", "recordings"))
                        os.makedirs(rec_dir, exist_ok=True)
                        rec_path = os.path.join(rec_dir, f"{self.call_sid}.wav")
                        with open(rec_path, "wb") as wf:
                            wf.write(rec_bytes)
                        rec_url = f"/uploads/recordings/{self.call_sid}.wav"
                        rec_file_size = len(rec_bytes)
                        rec_duration_sec = max(1, (len(rec_bytes) - 44) // 16000)
                        if duration_secs == 0:
                            mins, secs = divmod(rec_duration_sec, 60)
                            duration_str = f"{mins:02d}:{secs:02d}"
                        rec_status = "COMPLETED"
                        logger.info(f"[RECORDING] Saved {len(rec_bytes)} bytes two-way call recording ({rec_duration_sec}s) -> {rec_path}")
                    else:
                        rec_status = "NOT_RECORDED"
                        rec_err = "No audio frames captured during call"
                except Exception as e:
                    rec_status = "FAILED"
                    rec_err = str(e)
                    logger.warning(f"[RECORDING] Save error: {e}")

                db = SessionLocal()
                try:
                    from src.models.call_log import CallLog
                    call = None
                    if self.db_call_id:
                        call = db.query(CallLog).filter(CallLog.id == self.db_call_id).first()
                    if not call and self.call_sid:
                        call = db.query(CallLog).filter(CallLog.call_sid == self.call_sid).first()
                        if call:
                            self.db_call_id = call.id

                    direction_label = "OUTBOUND" if self.call_type == CallType.OUTBOUND else "INBOUND"

                    if not call:
                        call = CallLog(
                            call_sid=self.call_sid,
                            organization_id=self.organization_id,
                            caller_phone=self.caller_phone,
                            caller_name=self.caller_name,
                            call_type=self.call_type,
                            status="completed",
                            duration=duration_str,
                        )
                        db.add(call)
                        db.flush()
                        self.db_call_id = call.id

                    call.status = "completed"
                    call.duration = duration_str
                    call.call_type = self.call_type
                    call.messages = json.dumps(self._messages)
                    call.receptionist_state = f"SIP {direction_label} COMPLETED"
                    call.recording_status = rec_status
                    call.recording_file_size = rec_file_size
                    call.recording_duration = rec_duration_sec or duration_secs
                    if rec_err:
                        call.recording_error = rec_err
                    if rec_url:
                        call.recording_url = rec_url
                    db.commit()
                    logger.info(f"[DB] Call #{self.db_call_id} ({self.call_type.value}) finalised ({duration_str}, recording: {rec_status})")

                    # Safe background lead capture (non-blocking, preserved calling flow)
                    try:
                        from src.services.lead_service import LeadService
                        if hasattr(self, "booking_session") and self.booking_session:
                            LeadService(db).upsert_lead_from_session(
                                self.booking_session,
                                call_log_id=call.id
                            )
                        b_data = getattr(self.booking_session, "booking_data", None) if hasattr(self, "booking_session") else None
                        LeadService(db).capture_or_update_from_call(call.id, session_booking_data=b_data)
                    except Exception as lead_err:
                        logger.warning(f"[LeadService] Call #{self.db_call_id} lead capture note: {lead_err}")
                finally:
                    db.close()
            except Exception as exc:
                logger.error(f"[DB] Finalise failed: {exc}")

    async def _finalise_call_log(self):
        self._finalise_call_log_sync()

    def _log_to_db(self, role: str, text: str, intent: str = ""):
        try:
            db = SessionLocal()
            try:
                CallService(db).log_conversation_message(
                    session_id=self.session_id, role=role, message=text, intent=intent)
                if self.db_call_id:
                    from src.models.call_log import CallLog
                    import json
                    call = db.query(CallLog).filter(CallLog.id == self.db_call_id).first()
                    if call:
                        call.messages = json.dumps(self._messages)
                        db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning(f"[DB] log_to_db: {exc}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "call_sid": self.call_sid, "db_call_id": self.db_call_id,
            "caller_phone": self.caller_phone, "caller_name": self.caller_name,
            "started_at": self.started_at.isoformat(), "ended": self.ended,
            "turn_count": len([m for m in self._messages if m["role"] == "user"]),
            "current_lang": self.current_lang, "ai_speaking": self._ai_speaking,
        }


# Compatibility alias
SipCallHandler = SipCallSession

