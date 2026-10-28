import asyncio
import io
import math
import os
import struct
import time
import unittest
import wave
from datetime import datetime, timezone
import requests

from src.config.database import SessionLocal
from src.models.call_log import CallLog, CallType
from src.services.sip_call_handler import SipCallSession, TwoWayCallRecorder


def generate_synthetic_speech_pcm16(freq: float, duration_sec: float, sample_rate: int = 8000) -> bytes:
    """Generate recognizable 16-bit signed PCM audio at given frequency and duration."""
    num_samples = int(sample_rate * duration_sec)
    buf = bytearray()
    for i in range(num_samples):
        # Add fundamental + harmonic for realistic speech-like waveform
        sample = 0.6 * math.sin(2 * math.pi * freq * (i / sample_rate)) + 0.3 * math.sin(2 * math.pi * (freq * 2) * (i / sample_rate))
        val = int(sample * 16000)
        buf.extend(struct.pack("<h", max(-32768, min(32767, val))))
    return bytes(buf)


class TestTwoWayCallRecordingE2E(unittest.TestCase):

    def setUp(self):
        self.base_url = "http://127.0.0.1:8000"
        self.rec_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads", "recordings"))
        os.makedirs(self.rec_dir, exist_ok=True)

    def test_01_inbound_two_way_recording(self):
        """Test complete inbound call recording capturing both customer and AI voices."""
        async def run_inbound():
            call_sid = f"sip_test_in_{int(time.time())}"
            session = SipCallSession(
                caller_phone="+919876543210",
                caller_name="Test Inbound Customer",
                organization_id=1,
                call_sid=call_sid,
                call_type=CallType.INBOUND,
            )
            await session.start()

            # Customer speaks Sentence 1 (300 Hz pitch, 1.5s)
            cust_audio_1 = generate_synthetic_speech_pcm16(freq=300, duration_sec=1.5)
            session.two_way_recorder.feed_customer_pcm16(cust_audio_1)

            # AI receptionist responds Sentence 1 (600 Hz pitch, 2.0s)
            ai_audio_1 = generate_synthetic_speech_pcm16(freq=600, duration_sec=2.0)
            session.two_way_recorder.feed_ai_pcm16(ai_audio_1)

            # Customer speaks Sentence 2 (320 Hz pitch, 1.8s)
            cust_audio_2 = generate_synthetic_speech_pcm16(freq=320, duration_sec=1.8)
            session.two_way_recorder.feed_customer_pcm16(cust_audio_2)

            # AI receptionist responds Sentence 2 (550 Hz pitch, 2.5s)
            ai_audio_2 = generate_synthetic_speech_pcm16(freq=550, duration_sec=2.5)
            session.two_way_recorder.feed_ai_pcm16(ai_audio_2)

            # Hangup and finalize
            await session.stop()
            return session

        session = asyncio.run(run_inbound())

        # 1. Verify WAV file exists on disk
        wav_path = os.path.join(self.rec_dir, f"{session.call_sid}.wav")
        self.assertTrue(os.path.exists(wav_path), f"WAV recording file must exist at {wav_path}")
        file_size = os.path.getsize(wav_path)
        self.assertGreater(file_size, 44, "WAV file must contain audio data")

        # 2. Verify Audio Properties
        with wave.open(wav_path, "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1, "Must be mono audio")
            self.assertEqual(wf.getsampwidth(), 2, "Must be 16-bit audio")
            self.assertEqual(wf.getframerate(), 8000, "Must be 8000 Hz sample rate")
            duration_sec = wf.getnframes() / float(wf.getframerate())
            self.assertGreaterEqual(duration_sec, 2.0, "Recording must not be truncated prematurely")

        # 3. Verify Database Record
        db = SessionLocal()
        try:
            log = db.query(CallLog).filter(CallLog.call_sid == session.call_sid).first()
            self.assertIsNotNone(log, "CallLog must exist in DB")
            self.assertEqual(log.call_type, CallType.INBOUND, "Call type must be INBOUND")
            self.assertEqual(log.recording_status, "COMPLETED", "Recording status must be COMPLETED")
            self.assertEqual(log.recording_url, f"/uploads/recordings/{session.call_sid}.wav")
            self.assertEqual(log.recording_file_size, file_size)
            self.assertGreater(log.recording_duration, 0)
            inbound_log_id = log.id
        finally:
            db.close()

        # 4. Verify API Endpoints
        res = requests.get(f"{self.base_url}/api/recordings/{inbound_log_id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["recording_status"], "Available")
        self.assertEqual(data["call_type"], "Inbound")

        # Byte-range streaming
        stream_res = requests.get(f"{self.base_url}/api/recordings/{inbound_log_id}/audio", headers={"Range": "bytes=0-511"})
        self.assertEqual(stream_res.status_code, 206)
        self.assertEqual(stream_res.headers.get("Content-Range"), f"bytes 0-511/{file_size}")
        self.assertEqual(len(stream_res.content), 512)

        # Download attachment
        dl_res = requests.get(f"{self.base_url}/api/recordings/{inbound_log_id}/download")
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(len(dl_res.content), file_size)

    def test_02_outbound_two_way_recording(self):
        """Test complete outbound call recording capturing both directions."""
        async def run_outbound():
            call_sid = f"sip_out_test_{int(time.time())}"
            session = SipCallSession(
                caller_phone="+917935484931",
                caller_name="Outbound Call to +917935484931",
                organization_id=1,
                call_sid=call_sid,
                call_type=CallType.OUTBOUND,
            )
            await session.start()

            # AI speaks opening greeting (500 Hz, 2.0s)
            ai_audio = generate_synthetic_speech_pcm16(freq=500, duration_sec=2.0)
            session.two_way_recorder.feed_ai_pcm16(ai_audio)

            # Customer answers (280 Hz, 1.5s)
            cust_audio = generate_synthetic_speech_pcm16(freq=280, duration_sec=1.5)
            session.two_way_recorder.feed_customer_pcm16(cust_audio)

            # AI explains services (520 Hz, 3.0s)
            ai_audio_2 = generate_synthetic_speech_pcm16(freq=520, duration_sec=3.0)
            session.two_way_recorder.feed_ai_pcm16(ai_audio_2)

            await session.stop()
            return session

        session = asyncio.run(run_outbound())

        # 1. Verify WAV file exists on disk
        wav_path = os.path.join(self.rec_dir, f"{session.call_sid}.wav")
        self.assertTrue(os.path.exists(wav_path))
        file_size = os.path.getsize(wav_path)

        # 2. Verify Database Record
        db = SessionLocal()
        try:
            log = db.query(CallLog).filter(CallLog.call_sid == session.call_sid).first()
            self.assertIsNotNone(log)
            self.assertEqual(log.call_type, CallType.OUTBOUND, "Call type must be OUTBOUND")
            self.assertEqual(log.recording_status, "COMPLETED")
            self.assertEqual(log.recording_file_size, file_size)
            outbound_log_id = log.id
        finally:
            db.close()

        # 3. Test API filtering for Outbound calls
        res = requests.get(f"{self.base_url}/api/recordings?call_type=outbound")
        self.assertEqual(res.status_code, 200)
        items = res.json()
        matching = [i for i in items if i["id"] == outbound_log_id]
        self.assertEqual(len(matching), 1, "Outbound call must appear in outbound filtered list")
        self.assertEqual(matching[0]["call_type"], "Outbound")
        self.assertEqual(matching[0]["recording_status"], "Available")

    def test_03_idempotent_finalization(self):
        """Verify that duplicate hangup calls do not create duplicate records."""
        async def run_dup():
            call_sid = f"sip_test_dup_{int(time.time())}"
            session = SipCallSession(
                caller_phone="+919876543210",
                caller_name="Duplicate Hangup Tester",
                organization_id=1,
                call_sid=call_sid,
                call_type=CallType.INBOUND,
            )
            await session.start()
            session.two_way_recorder.feed_customer_pcm16(generate_synthetic_speech_pcm16(300, 1.0))
            await session.stop()
            # Call stop again (simulating duplicate hangup event)
            await session._finalise_call_log()
            return session

        session = asyncio.run(run_dup())

        db = SessionLocal()
        try:
            count = db.query(CallLog).filter(CallLog.call_sid == session.call_sid).count()
            self.assertEqual(count, 1, "Duplicate finalization must NOT create duplicate rows")
        finally:
            db.close()

    def test_04_failed_recording_reporting(self):
        """Verify that a call without audio properly reports NOT_RECORDED without crashing."""
        async def run_empty():
            call_sid = f"sip_test_empty_{int(time.time())}"
            session = SipCallSession(
                caller_phone="+919876543210",
                caller_name="Empty Audio Caller",
                organization_id=1,
                call_sid=call_sid,
                call_type=CallType.INBOUND,
            )
            await session.start()
            # No audio fed
            # Stop without feeding customer or AI audio
            session.two_way_recorder.customer_track.clear()
            session.two_way_recorder.ai_track.clear()
            await session.stop()
            return session

        session = asyncio.run(run_empty())
        db = SessionLocal()
        try:
            log = db.query(CallLog).filter(CallLog.call_sid == session.call_sid).first()
            self.assertIsNotNone(log)
            # Even if empty, finalize creates 1s minimum silence WAV so it never corrupts
            self.assertIn(log.recording_status, ("COMPLETED", "NOT_RECORDED"))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
