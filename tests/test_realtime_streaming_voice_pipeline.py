import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from src.services.sip_call_handler import SipCallSession
from src.services.deepgram_stt_service import DeepgramStreamingSession


def test_deepgram_streaming_callbacks():
    """Verify partial and final STT transcripts update turn state correctly."""
    async def run_test():
        partials = []
        finals = []

        def on_partial(utt_id, text, duration_s):
            partials.append((utt_id, text))

        def on_final(utt_id, text, metrics):
            finals.append((utt_id, text))

        session = DeepgramStreamingSession(
            call_id="test_call_123",
            on_partial=on_partial,
            on_final=on_final,
        )

        # Test partial result message from Deepgram
        mock_partial_msg = {
            "type": "Results",
            "is_final": False,
            "speech_final": False,
            "channel": {
                "alternatives": [{"transcript": "Hello, I would like to book", "confidence": 0.95}]
            }
        }
        await session._handle_results_message(mock_partial_msg)
        assert len(partials) == 1
        assert partials[0][1] == "Hello, I would like to book"

        # Test final result message from Deepgram
        mock_final_msg = {
            "type": "Results",
            "is_final": True,
            "speech_final": True,
            "channel": {
                "alternatives": [{"transcript": "Hello, I would like to book an appointment.", "confidence": 0.98}]
            }
        }
        await session._handle_results_message(mock_final_msg)
        assert len(finals) == 1
        assert finals[0][1] == "Hello, I would like to book an appointment."

    asyncio.run(run_test())


def test_streaming_sentence_dispatch_and_latency_emission():
    """Verify SipCallSession streams LLM sentences immediately into TTS and emits pipeline_latency."""
    async def run_test():
        session = SipCallSession(
            caller_phone="+1234567890",
            caller_name="Test User",
            organization_id=1,
        )

        sent_texts = []

        async def mock_speak(text, intent="", metrics=None):
            sent_texts.append(text)
            if metrics and "tts_start" not in metrics:
                metrics["tts_start"] = time.monotonic()
                metrics["tts_first_audio"] = time.monotonic()
                metrics["tts_latency_ms"] = 50

        session._speak = mock_speak

        emitted_events = []

        async def mock_emit(event, data, **kwargs):
            emitted_events.append((event, data))

        def mock_generator(**kwargs):
            yield "Hello there! "
            yield "How can I help "
            yield "you today?"

        from src.services.ai_service import ai_service
        with patch.object(ai_service, "_fast_rule_response", return_value=None), \
             patch.object(ai_service, "generate_response_stream", side_effect=mock_generator), \
             patch("src.sockets.socket_manager.sio.emit", side_effect=mock_emit), \
             patch("src.services.rag_service.rag_service.retrieve_context", return_value="Test Context"):

            metrics = {"utt_id": "test_utt_1", "t_stt_final": time.monotonic()}
            await session._run_ai_pipeline("What are your working hours and services?", metrics=metrics)

        assert len(sent_texts) >= 1
        full_sent_str = " ".join(sent_texts)
        assert "Hello there!" in full_sent_str
        assert "How can I help" in full_sent_str or "you today?" in full_sent_str

        latency_events = [data for event, data in emitted_events if event == "pipeline_latency"]
        assert len(latency_events) == 1
        latency_data = latency_events[0]
        assert "llm_latency_ms" in latency_data
        assert "total_latency_ms" in latency_data
        assert latency_data["call_sid"] == session.call_sid

    asyncio.run(run_test())
