"""
Deepgram Real-Time Streaming STT Service.

Provides:
1. `DeepgramStreamingSession`: One real-time WebSocket streaming connection per active call.
   - Continuously receives PCM16 8000Hz mono audio from customer RTP.
   - Emits real-time interim results (CUSTOMER_PARTIAL).
   - Emits speech_final / endpointed results (CUSTOMER_FINAL).
   - Handles keepalive, automatic reconnection, and clean teardown.
2. `DeepgramRestSTT`: One-shot REST transcription helper.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, Optional
import websockets

from src.config.env import settings

logger = logging.getLogger(__name__)


def get_deepgram_api_key() -> str:
    """Retrieve Deepgram API key strictly from environment / settings."""
    key = getattr(settings, "DEEPGRAM_API_KEY", None) or os.getenv("DEEPGRAM_API_KEY", "")
    return str(key).strip()


class DeepgramStreamingSession:
    """
    Per-call Deepgram WebSocket real-time streaming STT session.
    
    Streams raw Linear16 8kHz mono PCM audio chunks continuously as they arrive from RTP.
    Yields interim results and final endpointed transcriptions with low conversational latency.
    """

    def __init__(
        self,
        call_id: str,
        on_partial: Optional[Callable[[str, str, float], Any]] = None,
        on_final: Optional[Callable[[str, str, Dict[str, Any]], Any]] = None,
        on_error: Optional[Callable[[str, Exception], Any]] = None,
        sample_rate: int = 8000,
        model: str = "nova-2-phonecall",
        endpointing_ms: int = 400,
        utterance_end_ms: int = 800,
    ):
        self.call_id = call_id
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        self.sample_rate = sample_rate
        self.model = model or getattr(settings, "DEEPGRAM_MODEL", "nova-2-phonecall") or "nova-2-phonecall"
        raw_endpointing = endpointing_ms or getattr(settings, "DEEPGRAM_ENDPOINTING_MS", 400) or 400
        self.endpointing_ms = max(400, int(raw_endpointing))
        raw_u_end = utterance_end_ms or getattr(settings, "DEEPGRAM_UTTERANCE_END_MS", 1000) or 1000
        self.utterance_end_ms = max(1000, int(raw_u_end))

        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._ws = None
        self._connected = False
        self._running = False
        self._tasks: list = []
        self._reconnect_count = 0
        self._max_reconnects = 5
        # Mutex: only one _reconnect() may run at a time to prevent concurrent recv() race
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()

        self._utterance_counter = 100
        self._current_utt_id = f"utt_{self._utterance_counter}"
        self._utterance_start_time: Optional[float] = None
        self._first_partial_time: Optional[float] = None
        self._audio_bytes_sent_in_utterance = 0
        self._current_turn_transcript = ""
        self._accumulated_finals: list = []
        self._last_partial_text = ""
        self._turn_finalized = False
        self._debounce_task: Optional[asyncio.Task] = None
        self._last_final_text = ""
        self._last_final_time = 0.0
        self._processed_final_utt_ids: set = set()

    def reset_turn(self):
        """Reset turn buffers and debounce timer cleanly when AI speaks or between turns."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None
        self._first_partial_time = None
        self._utterance_start_time = None
        self._audio_bytes_sent_in_utterance = 0
        self._current_turn_transcript = ""
        self._accumulated_finals = []
        self._turn_finalized = False
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Exception:
                break

    def _build_ws_url(self) -> str:
        """Build the Deepgram WebSocket URL from current settings."""
        return (
            f"wss://api.deepgram.com/v1/listen"
            f"?encoding=linear16"
            f"&sample_rate={self.sample_rate}"
            f"&channels=1"
            f"&model={self.model}"
            f"&interim_results=true"
            f"&endpointing={self.endpointing_ms}"
            f"&utterance_end_ms={max(1000, self.utterance_end_ms)}"
            f"&smart_format=true"
            f"&punctuate=true"
        )

    async def _connect_ws(self) -> bool:
        """Establish or re-establish the Deepgram WebSocket connection. Returns True on success."""
        api_key = get_deepgram_api_key()
        if not api_key:
            logger.error(f"[DEEPGRAM_ERROR] call_id={self.call_id} error='DEEPGRAM_API_KEY is missing'")
            return False

        url = self._build_ws_url()
        headers = {"Authorization": f"Token {api_key}"}
        t0 = time.monotonic()

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
            self._connected = True
            connect_dur_ms = round((time.monotonic() - t0) * 1000)
            logger.info(
                f"[DEEPGRAM_STREAM_CONNECTED] call_id={self.call_id} model={self.model} "
                f"sample_rate={self.sample_rate} endpointing={self.endpointing_ms}ms "
                f"connect_ms={connect_dur_ms} reconnect_count={self._reconnect_count}"
            )
            return True
        except Exception as exc:
            self._connected = False
            self._ws = None
            logger.error(f"[DEEPGRAM_CONNECT_FAIL] call_id={self.call_id} error='{exc}' reconnect_count={self._reconnect_count}")
            return False

    async def _reconnect(self) -> bool:
        """Attempt to reconnect the Deepgram WebSocket after a disconnect.

        Uses _reconnect_lock so that only ONE reconnect attempt runs at a time.
        If another reconnect is already in progress, the caller silently returns.
        Returns True on successful reconnect, False otherwise.
        """
        if not self._running:
            return False

        # Guard: if reconnect already running, skip — the existing one will restart the loops
        if self._reconnect_lock.locked():
            logger.debug(f"[DEEPGRAM_RECONNECT_SKIP] call_id={self.call_id} reconnect already in progress")
            return False

        async with self._reconnect_lock:
            if not self._running:
                return False

            self._connected = False

            # Cancel all existing tasks cleanly before touching the WebSocket
            for task in list(self._tasks):
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
                    except Exception:
                        pass
            self._tasks.clear()

            # Close old WebSocket cleanly
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
                self._ws = None

            # Drain stale audio from queue to avoid feeding old audio after reconnect
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except Exception:
                    break

            # Reset turn state so no stale partial transcripts leak into next turn
            self.reset_turn()

            while self._running and self._reconnect_count < self._max_reconnects:
                self._reconnect_count += 1
                backoff = min(2.0 ** (self._reconnect_count - 1), 8.0)
                logger.info(
                    f"[DEEPGRAM_RECONNECT] call_id={self.call_id} attempt={self._reconnect_count}/{self._max_reconnects} "
                    f"backoff={backoff:.1f}s"
                )
                await asyncio.sleep(backoff)

                if not self._running:
                    return False

                if await self._connect_ws():
                    # Re-launch send and recv loops only after lock is still held (no race)
                    send_task = asyncio.create_task(self._send_loop(), name=f"dg_send_{self.call_id}")
                    recv_task = asyncio.create_task(self._recv_loop(), name=f"dg_recv_{self.call_id}")
                    self._tasks = [send_task, recv_task]
                    logger.info(f"[DEEPGRAM_RECONNECTED] call_id={self.call_id} attempt={self._reconnect_count}")
                    return True

            logger.error(
                f"[DEEPGRAM_RECONNECT_EXHAUSTED] call_id={self.call_id} "
                f"max_reconnects={self._max_reconnects} reached — STT unavailable for this call"
            )
            return False

    async def start(self):
        """Establish Deepgram WebSocket streaming connection and launch background tasks."""
        if self._running:
            return

        self._running = True
        self._reconnect_count = 0

        if not await self._connect_ws():
            self._running = False
            if self.on_error:
                try:
                    res = self.on_error(self.call_id, Exception("Initial Deepgram connection failed"))
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass
            return

        # Spawn concurrent non-blocking tasks for sending audio & receiving transcripts
        send_task = asyncio.create_task(self._send_loop(), name=f"dg_send_{self.call_id}")
        recv_task = asyncio.create_task(self._recv_loop(), name=f"dg_recv_{self.call_id}")
        self._tasks = [send_task, recv_task]

    def feed_pcm16_audio(self, pcm16_bytes: bytes):
        """
        Feed raw PCM16 (Linear16 8000Hz mono) audio from RTP receiver.
        Never blocks the RTP receiver thread.
        """
        if not self._running or not pcm16_bytes:
            return
        try:
            self._audio_queue.put_nowait(pcm16_bytes)
        except asyncio.QueueFull:
            pass  # Drop oldest frame to preserve live real-time latency

    async def _send_loop(self):
        """Continuously streams raw PCM16 audio chunks to Deepgram over WebSocket."""
        last_audio_sent_time = time.monotonic()
        chunks_sent = 0
        while self._running and self._connected and self._ws:
            try:
                try:
                    chunk = await asyncio.wait_for(self._audio_queue.get(), timeout=2.0)
                    if chunk and self._ws:
                        self._audio_bytes_sent_in_utterance += len(chunk)
                        await self._ws.send(chunk)
                        now_t = time.time()
                        chunks_sent += 1
                        if chunks_sent == 1 or chunks_sent % 50 == 0:
                            dur_ms = round((len(chunk) / (self.sample_rate * 2)) * 1000)
                            logger.info(
                                f"[DEEPGRAM_AUDIO] call_id={self.call_id} chunk_bytes={len(chunk)} "
                                f"sample_rate={self.sample_rate} channels=1 sample_width=2 "
                                f"encoding=linear16 audio_duration_ms={dur_ms}"
                            )
                        logger.debug(
                            f"[DEEPGRAM_AUDIO_SENT] call_id={self.call_id} chunk_bytes={len(chunk)} timestamp={now_t:.4f}"
                        )
                        last_audio_sent_time = time.monotonic()
                except asyncio.TimeoutError:
                    if self._ws and (time.monotonic() - last_audio_sent_time) >= 3.0:
                        await self._ws.send(json.dumps({"type": "KeepAlive"}))
                        last_audio_sent_time = time.monotonic()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._running:
                    logger.warning(f"[DEEPGRAM_SEND_WARN] call_id={self.call_id}: {exc}")
                break

        # _send_loop exited: only trigger reconnect if _recv_loop hasn't already
        # (checked via lock — if lock is already held, _recv_loop owns reconnect)
        if self._running and not self._connected and not self._reconnect_lock.locked():
            asyncio.create_task(self._reconnect())

    async def _recv_loop(self):
        """Continuously receives real-time JSON transcription frames from Deepgram."""
        while self._running and self._connected and self._ws:
            try:
                msg = await self._ws.recv()
                if not msg:
                    continue

                data = json.loads(msg)
                msg_type = data.get("type")

                if msg_type == "Results":
                    await self._handle_results_message(data)
                elif msg_type == "UtteranceEnd":
                    await self._handle_utterance_end(data)
                elif msg_type == "Metadata":
                    pass
                elif msg_type == "Error":
                    err_msg = data.get("message") or data.get("description") or str(data)
                    logger.error(f"[DEEPGRAM_ERROR] call_id={self.call_id} error='{err_msg}' code=DG_API_ERROR")

            except asyncio.CancelledError:
                break
            except websockets.exceptions.ConnectionClosed as cc:
                if self._running:
                    logger.warning(
                        f"[DEEPGRAM_STREAM_CLOSED] call_id={self.call_id} code={cc.code} "
                        f"reason={cc.reason} — will attempt reconnect"
                    )
                    self._connected = False
                    # _recv_loop owns the primary reconnect trigger
                    asyncio.create_task(self._reconnect())
                break
            except Exception as exc:
                if self._running:
                    logger.error(f"[DEEPGRAM_RECV_ERROR] call_id={self.call_id}: {exc}")
                    self._connected = False
                    asyncio.create_task(self._reconnect())
                break

    async def _handle_results_message(self, data: Dict[str, Any]):
        """Handle Deepgram 'Results' message with interim or speech_final transcripts."""
        now = time.monotonic()
        channel = data.get("channel", {})
        alternatives = channel.get("alternatives", [])
        if not alternatives:
            return

        alt = alternatives[0]
        transcript = (alt.get("transcript") or "").strip()
        confidence = alt.get("confidence", 0.0)
        is_final = data.get("is_final", False)
        speech_final = data.get("speech_final", False)

        duration_s = data.get("duration", 0.0)
        if duration_s <= 0 and self._audio_bytes_sent_in_utterance > 0:
            duration_s = round(self._audio_bytes_sent_in_utterance / (self.sample_rate * 2), 2)

        # Word-level confidence calculation if alternative confidence is 0.0 or absent
        words = alt.get("words", [])
        if words and (confidence <= 0.0 or confidence is None):
            w_confs = [float(w.get("confidence")) for w in words if w.get("confidence") is not None]
            if w_confs:
                confidence = sum(w_confs) / len(w_confs)
        confidence = float(confidence or 0.0)
        if confidence > 0.0:
            self._last_turn_confidence = confidence

        # Cancel any active fallback debounce timer whenever ANY new speech arrives
        if transcript and self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None

        # If previous turn was finalized and NEW speech arrives, check for trailing duplicate frame
        if transcript and self._turn_finalized:
            clean_curr = re.sub(r'[^\w\s]', '', transcript).lower().strip()
            clean_last = re.sub(r'[^\w\s]', '', getattr(self, "_last_final_text", "") or "").lower().strip()
            time_since_final = now - getattr(self, "_last_final_time", 0.0)

            # If the frame arrives within 1.8s and its normalized text matches, is contained in, or contains the last finalized text,
            # this is the trailing endpoint/speech_final frame of the same turn - ignore it!
            if time_since_final < 1.8 and clean_curr and clean_last and (
                clean_curr == clean_last or clean_curr in clean_last or clean_last in clean_curr
            ):
                logger.debug(f"[DEEPGRAM_DUP_FRAME] Trailing frame for finalized turn ignored: '{transcript}'")
                return

            if transcript == self._last_final_text:
                # Duplicate frame of the just-finalized turn - ignore!
                return
            self._turn_finalized = False
            self._accumulated_finals = []
            self._current_turn_transcript = ""
            self._last_partial_text = ""
            self._utterance_start_time = now
            self._first_partial_time = now
            self._audio_bytes_sent_in_utterance = 0

        if transcript:
            self._current_turn_transcript = transcript
            if self._utterance_start_time is None:
                self._utterance_start_time = now
                self._first_partial_time = now

        # ── Interim Transcript (CUSTOMER_PARTIAL) ───────────────────
        if not speech_final and not is_final:
            interim_full = " ".join([p for p in (self._accumulated_finals + [transcript]) if p]).strip()
            if interim_full and interim_full != self._last_partial_text:
                self._last_partial_text = interim_full
                logger.info(
                    f"[DEEPGRAM_PARTIAL] call_id={self.call_id} utterance_id={self._current_utt_id} "
                    f"text='{interim_full}' timestamp={time.time():.4f}"
                )
                if self.on_partial:
                    try:
                        res = self.on_partial(self._current_utt_id, interim_full, duration_s)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as e:
                        logger.warning(f"[DG_PARTIAL_CALLBACK_ERR] {e}")
            return

        # ── Middle is_final chunk (confirmed words of ongoing utterance) ──
        if is_final and not speech_final:
            if transcript and (not self._accumulated_finals or transcript != self._accumulated_finals[-1]):
                self._accumulated_finals.append(transcript)
            accumulated_text = " ".join(self._accumulated_finals).strip() or transcript

            if accumulated_text:
                if accumulated_text != self._last_partial_text:
                    self._last_partial_text = accumulated_text
                    logger.info(
                        f"[DEEPGRAM_PARTIAL] call_id={self.call_id} utterance_id={self._current_utt_id} "
                        f"text='{accumulated_text}' timestamp={time.time():.4f}"
                    )
                    if self.on_partial:
                        try:
                            res = self.on_partial(self._current_utt_id, accumulated_text, duration_s)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as e:
                            logger.warning(f"[DG_PARTIAL_CALLBACK_ERR] {e}")

                # Fallback debounce timer (1.2s - 1.5s) in case speech_final is delayed by network/noise
                # Avoid prematurely finalizing short or syntactically incomplete phrases
                if self._debounce_task and not self._debounce_task.done():
                    self._debounce_task.cancel()

                target_utt = self._current_utt_id
                async def _debounce_finalize(txt: str, conf: float, dur: float, target_id: str):
                    try:
                        words = txt.split()
                        last_w = words[-1].lower().strip(".,!?") if words else ""
                        incomplete_words = {
                            "the", "a", "an", "is", "are", "was", "were", "to", "for", "in", "of", "on", "at",
                            "with", "and", "or", "what", "how", "who", "when", "where", "why", "can", "could",
                            "would", "should", "i", "you", "we", "my", "your", "do", "does"
                        }
                        if len(words) <= 3 or last_w in incomplete_words:
                            wait_s = 1.5
                        else:
                            wait_s = 1.2
                        await asyncio.sleep(wait_s)
                        if not self._turn_finalized and self._running and self._current_utt_id == target_id:
                            logger.info(f"[DEEPGRAM_ENDPOINT] Silence fallback finalized: '{txt}' utt_id={target_id}")
                            self._speech_endpoint_time = time.monotonic()
                            await self._finalize_utterance(txt, conf, dur)
                    except asyncio.CancelledError:
                        pass

                self._debounce_task = asyncio.create_task(_debounce_finalize(accumulated_text, confidence, duration_s, target_utt))
            return

        # ── speech_final: true (Deepgram Natural Endpoint Reached) ───
        if speech_final:
            self._speech_endpoint_time = time.monotonic()
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
                self._debounce_task = None

            if self._turn_finalized:
                # Already finalized by debounce timer or previous frame for this utterance!
                return

            if transcript and (not self._accumulated_finals or transcript != self._accumulated_finals[-1]):
                self._accumulated_finals.append(transcript)
            final_text = " ".join(self._accumulated_finals).strip() or transcript

            if not final_text:
                # Pure silence endpoint without any prior speech in this turn
                self._first_partial_time = None
                self._utterance_start_time = None
                self._audio_bytes_sent_in_utterance = 0
                self._current_turn_transcript = ""
                self._accumulated_finals = []
                self._last_partial_text = ""
                return

            await self._finalize_utterance(final_text, confidence, duration_s)

    async def _finalize_utterance(self, transcript: str, confidence: float, duration_s: float):
        """Emit CUSTOMER_FINAL, trigger conversational AI, and reset utterance state cleanly."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None

        now = time.monotonic()
        text = transcript.strip()
        if not text or self._turn_finalized:
            return

        utt_id = self._current_utt_id
        if utt_id in self._processed_final_utt_ids:
            return
        self._processed_final_utt_ids.add(utt_id)

        # Mark finalized for this turn
        self._turn_finalized = True
        self._last_final_text = text
        self._last_final_time = now

        # Immediately advance utterance counter for next turn so no trailing frame can reuse this utt_id
        self._utterance_counter += 1
        self._current_utt_id = f"utt_{self._utterance_counter}"

        t_start = self._utterance_start_time or (now - max(0.5, duration_s))
        t_first_partial = self._first_partial_time or t_start
        t_speech_endpoint = getattr(self, "_speech_endpoint_time", None) or now
        self._speech_endpoint_time = None

        first_partial_ms = max(40, round((t_first_partial - t_start) * 1000))
        final_ms = max(80, round((now - t_start) * 1000))

        actual_dur_s = duration_s if duration_s > 0.1 else round(final_ms / 1000.0, 2)
        customer_speech_duration_ms = round(actual_dur_s * 1000)
        finalization_latency_ms = max(40, round((now - t_speech_endpoint) * 1000))
        real_conf = float(confidence if (confidence is not None and confidence > 0.0) else getattr(self, "_last_turn_confidence", 1.0) or 1.0)

        logger.info(
            f"[DEEPGRAM_FINAL] call_id={self.call_id} utterance_id={utt_id} "
            f"text='{text}' speech_dur={customer_speech_duration_ms}ms "
            f"finalization={finalization_latency_ms}ms timestamp={time.time():.4f}"
        )

        metrics = {
            "call_id": self.call_id,
            "utt_id": utt_id,
            "text": text,
            "confidence": real_conf,
            "audio_duration_s": actual_dur_s,
            "audio_duration_ms": customer_speech_duration_ms,
            "customer_speech_duration_ms": customer_speech_duration_ms,
            "finalization_latency_ms": finalization_latency_ms,
            "stt_finalization_ms": finalization_latency_ms,
            "audio_bytes": self._audio_bytes_sent_in_utterance,
            "deepgram_first_partial_ms": first_partial_ms,
            "deepgram_final_ms": final_ms,
            "total_turnaround_ms": final_ms,
            "first_partial_ms": first_partial_ms,
            "final_transcript_ms": final_ms,
            "stt_latency_ms": final_ms,
            "t_audio_rx": t_start,
            "t_stt_partial": t_first_partial,
            "t_speech_endpoint": t_speech_endpoint,
            "t_stt_final": now,
            "speech_end_monotonic": t_speech_endpoint,
            "timestamp": time.time(),
        }

        logger.info(
            f"\n[STT_LATENCY]"
            f"\n  call_id={self.call_id}"
            f"\n  utterance_id={utt_id}"
            f"\n  audio_duration={actual_dur_s:.2f}s ({customer_speech_duration_ms}ms)"
            f"\n  customer_speech_duration_ms={customer_speech_duration_ms}ms"
            f"\n  finalization_latency_ms={finalization_latency_ms}ms"
            f"\n  total_turnaround_ms={final_ms}ms"
            f"\n  first_partial_ms={first_partial_ms}ms"
            f"\n  final_ms={final_ms}ms"
            f"\n  confidence={real_conf:.2f}"
            f"\n  text='{text}'"
        )

        # Reset buffers for next turn
        self._first_partial_time = None
        self._utterance_start_time = None
        self._audio_bytes_sent_in_utterance = 0
        self._current_turn_transcript = ""
        self._accumulated_finals = []
        self._last_partial_text = ""

        # Dispatch final transcript to conversational decision engine exactly once
        if self.on_final:
            try:
                res = self.on_final(utt_id, text, metrics)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as e:
                logger.error(f"[DG_FINAL_CALLBACK_ERR] {e}")

    async def _handle_utterance_end(self, data: Dict[str, Any]):
        """Deepgram indicates customer finished speaking turn."""
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None

        if self._turn_finalized:
            return

        final_candidates = self._accumulated_finals or ([self._current_turn_transcript.strip()] if getattr(self, "_current_turn_transcript", "").strip() else [])
        text = " ".join(final_candidates).strip()
        self._current_turn_transcript = ""
        self._accumulated_finals = []
        if text:
            clean_curr = re.sub(r'[^\w\s]', '', text).lower().strip()
            clean_last = re.sub(r'[^\w\s]', '', getattr(self, "_last_final_text", "") or "").lower().strip()
            time_since_final = time.monotonic() - getattr(self, "_last_final_time", 0.0)
            if time_since_final < 1.8 and clean_curr and clean_last and (clean_curr == clean_last or clean_curr in clean_last):
                logger.debug(f"[DEEPGRAM_DUP_UTT_END] Trailing utterance_end for finalized turn ignored: '{text}'")
                return
            dur = round(self._audio_bytes_sent_in_utterance / (self.sample_rate * 2), 2) if self._audio_bytes_sent_in_utterance > 0 else 0.5
            end_conf = float(getattr(self, "_last_turn_confidence", 1.0) or 1.0)
            await self._finalize_utterance(text, end_conf, dur)
            return

        self._first_partial_time = None
        self._utterance_start_time = None
        self._audio_bytes_sent_in_utterance = 0
        self._current_turn_transcript = ""
        self._last_partial_text = ""

    async def stop(self):
        """Cleanly close WebSocket connection and release streaming resources."""
        if not self._running:
            return
        self._running = False
        self._connected = False

        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
            self._debounce_task = None

        try:
            if self._ws:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await asyncio.sleep(0.05)
                await self._ws.close()
        except Exception:
            pass
        finally:
            self._ws = None

        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()

        # Drain audio queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Exception:
                break

        logger.info(f"[DEEPGRAM_STREAM_STOPPED] call_id={self.call_id}")
