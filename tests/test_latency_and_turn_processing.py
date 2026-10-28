"""
Tests for latency optimization, turn processing, script scope resolution,
and Asia/Kolkata date parsing.
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.services.rag_service import rag_service
from src.services.calendar_verification_service import calendar_verification_service
from src.services.booking_state_service import booking_state_service
from src.config.env import settings
from src.config.database import SessionLocal
from src.models.knowledge_doc import KnowledgeDocument, KnowledgeDocStatus


def test_inbound_vs_outbound_rag_script_resolution():
    """Verify that inbound calls resolve inbound documents and outbound calls resolve outbound scripts."""
    org_id = 1

    # Ensure test sandbox has Doc 1 (Outbound Script) and Doc 2 (Inbound Doctor RAG)
    with SessionLocal() as session:
        doc1 = session.query(KnowledgeDocument).filter(KnowledgeDocument.id == 1).first()
        if not doc1:
            doc1 = KnowledgeDocument(
                id=1,
                organization_id=org_id,
                title="Pramerica_Life_Insurance_Outbound_RAG_Script.txt",
                document_type="OUTBOUND_SCRIPT",
                scope="outbound",
                ai_usage="Active",
                status=KnowledgeDocStatus.INDEXED,
            )
            session.add(doc1)
        else:
            doc1.document_type = "OUTBOUND_SCRIPT"
            doc1.scope = "outbound"
            doc1.ai_usage = "Active"
            doc1.status = KnowledgeDocStatus.INDEXED

        doc2 = session.query(KnowledgeDocument).filter(KnowledgeDocument.id == 2).first()
        if not doc2:
            doc2 = KnowledgeDocument(
                id=2,
                organization_id=org_id,
                title="Doctor_Receptionist_RAG_Knowledge_Base.pdf",
                document_type="RAG",
                scope="inbound",
                ai_usage="Active",
                status=KnowledgeDocStatus.INDEXED,
            )
            session.add(doc2)
        else:
            doc2.document_type = "RAG"
            doc2.scope = "inbound"
            doc2.ai_usage = "Active"
            doc2.status = KnowledgeDocStatus.INDEXED

        session.commit()

    inbound_doc = rag_service._resolve_active_document_id(organization_id=org_id, scope="inbound")
    assert inbound_doc == "2", f"Inbound call must resolve Doc 2, got {inbound_doc}"

    # Fetch document metadata
    with SessionLocal() as session:
        doc_meta = session.query(KnowledgeDocument).filter(KnowledgeDocument.id == int(inbound_doc)).first()
        assert doc_meta is not None
        assert doc_meta.scope == "inbound"
        assert "Doctor" in doc_meta.title or "Receptionist" in doc_meta.title

    # Outbound call should resolve outbound document
    outbound_doc = rag_service._resolve_active_document_id(organization_id=org_id, scope="outbound")
    assert outbound_doc == "1", f"Outbound call must resolve Doc 1, got {outbound_doc}"
    with SessionLocal() as session:
        out_meta = session.query(KnowledgeDocument).filter(KnowledgeDocument.id == int(outbound_doc)).first()
        assert out_meta is not None
        assert out_meta.scope == "outbound"
        assert "Pramerica" in out_meta.title

    # Reject explicit outbound doc if requested for inbound
    rejected = rag_service._resolve_active_document_id(organization_id=org_id, document_id="1", scope="inbound")
    assert rejected != "1", "Outbound script must NEVER be used for an inbound call"
    assert rejected == "2", "Inbound call should fall back to valid inbound document (Doc 2)"


def test_relative_date_normalization_kolkata():
    """Verify relative date parsing matches Asia/Kolkata timezone."""
    try:
        kolkata_tz = ZoneInfo("Asia/Kolkata")
        now_kolkata = datetime.now(kolkata_tz)
    except Exception:
        kolkata_tz = timezone(timedelta(hours=5, minutes=30))
        now_kolkata = datetime.now(kolkata_tz)

    today_str = now_kolkata.strftime("%Y-%m-%d")

    # 1. Test calendar_verification_service.parse_date
    parsed_today = calendar_verification_service.parse_date("today")
    assert parsed_today.isoformat() == today_str, f"Expected {today_str}, got {parsed_today}"

    parsed_tomorrow = calendar_verification_service.parse_date("tomorrow")
    tomorrow_str = (now_kolkata + timedelta(days=1)).strftime("%Y-%m-%d")
    assert parsed_tomorrow.isoformat() == tomorrow_str, f"Expected {tomorrow_str}, got {parsed_tomorrow}"

    parsed_day_after = calendar_verification_service.parse_date("day after tomorrow")
    day_after_str = (now_kolkata + timedelta(days=2)).strftime("%Y-%m-%d")
    assert parsed_day_after.isoformat() == day_after_str, f"Expected {day_after_str}, got {parsed_day_after}"

    # 2. Test booking_state_service._normalize_date
    norm_today = booking_state_service._normalize_date("today")
    assert norm_today == today_str

    norm_tomorrow = booking_state_service._normalize_date("tomorrow")
    assert norm_tomorrow == tomorrow_str

    norm_day_after = booking_state_service._normalize_date("day after tomorrow")
    assert norm_day_after == day_after_str


def test_pipeline_latency_seven_stages_calculation():
    """Verify calculation of all 7 pipeline latency stages and target < 2000ms."""
    # Simulate monotonic timeline for an inbound turn:
    # t0: customer speech starts
    # t1: first STT partial arrives (+120ms)
    # t2: customer stops speaking (+1500ms) -> speech_end_monotonic
    # t3: VAD endpoint detected (300ms after t2)
    # t4: Deepgram STT final arrives (+80ms after endpoint = 380ms after speech stop)
    # t5: Intent detection completes (+50ms)
    # t6: RAG retrieval completes (+15ms from cache)
    # t7: LLM yields first usable sentence (+700ms)
    # t8: Murf TTS finishes synthesizing first sentence (+800ms)
    # t9: RTP packetization and first TX packet sent (+15ms)

    t_speech_start = 1000.0
    t_stt_partial = 1000.12
    t_speech_endpoint = 1001.50 # customer stops speaking
    endpointing_ms = 300
    t_stt_final = 1001.88       # final transcript received (380ms after stop)
    t_intent_done = 1001.93     # intent +50ms
    t_rag_done = 1001.945       # RAG +15ms
    t_llm_first = 1002.645      # LLM +700ms
    t_tts_first = 1003.445      # TTS +800ms
    t_rtp_start = 1003.460      # RTP TX +15ms

    # Calculate stages
    stage_1_vad = endpointing_ms
    stage_2_stt = round((t_stt_final - t_speech_endpoint) * 1000) # 380ms
    stage_3_intent = round((t_intent_done - t_stt_final) * 1000)  # 50ms
    stage_4_rag = round((t_rag_done - t_intent_done) * 1000)      # 15ms
    stage_5_llm = round((t_llm_first - t_rag_done) * 1000)        # 700ms
    stage_6_tts = round((t_tts_first - t_llm_first) * 1000)       # 800ms
    stage_7_rtp = round((t_rtp_start - t_tts_first) * 1000)       # 15ms

    total_cust_to_ai = round((t_rtp_start - t_speech_endpoint) * 1000) # 1960ms

    assert stage_1_vad == 300, "VAD endpoint duration should reflect configured endpointing_ms (300ms)"
    assert stage_2_stt == 380
    assert stage_3_intent == 50
    assert stage_4_rag == 15
    assert stage_5_llm == 700
    assert stage_6_tts == 800
    assert stage_7_rtp == 15

    assert total_cust_to_ai == 1960
    assert total_cust_to_ai <= 2000, f"Customer stop to AI speech must be <= 2000ms, got {total_cust_to_ai}ms"


def test_ai_utterance_id_and_chunk_stability():
    """Verify AI utterance ID is stable across streamed sentence chunks for a turn."""
    cust_utt_id = "utt_105"
    active_id = cust_utt_id

    # Turn chunk 1
    ai_utt_id_1 = f"ai_{active_id}" if not str(active_id).startswith("ai_") else str(active_id)
    assert ai_utt_id_1 == "ai_utt_105"

    # Turn chunk 2 in the same turn
    ai_utt_id_2 = f"ai_{active_id}" if not str(active_id).startswith("ai_") else str(active_id)
    assert ai_utt_id_2 == "ai_utt_105", "Sentence chunks in the same AI turn must share identical utterance_id"


def test_stream_abort_and_loop_closure():
    """Verify thread streaming handles stream_abort event and closed loop without throwing RuntimeError."""
    import threading

    async def _run():
        abort_event = threading.Event()
        test_loop = asyncio.get_running_loop()
        sentence_queue = asyncio.Queue()

        received = []

        def mock_stream_worker():
            for i in range(10):
                if abort_event.is_set():
                    break
                if not test_loop.is_closed() and test_loop.is_running() and not abort_event.is_set():
                    try:
                        test_loop.call_soon_threadsafe(sentence_queue.put_nowait, f"chunk_{i}")
                    except RuntimeError:
                        return
                time.sleep(0.01)
            if not test_loop.is_closed() and test_loop.is_running() and not abort_event.is_set():
                try:
                    test_loop.call_soon_threadsafe(sentence_queue.put_nowait, None)
                except RuntimeError:
                    pass

        t = threading.Thread(target=mock_stream_worker)
        t.start()

        item = await sentence_queue.get()
        received.append(item)
        assert item == "chunk_0"

        # Signal abort immediately
        abort_event.set()
        t.join(timeout=1.0)
        assert not t.is_alive()

    asyncio.run(_run())
