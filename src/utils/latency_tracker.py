"""
Latency metrics tracking for AI Receptionist pipeline turns.

Measures:
- audio_received_at
- stt_started_at / stt_completed_at / stt_ms
- language_detected_at / intent_detected_at / intent_ms
- retrieval_started_at / retrieval_completed_at / retrieval_ms
- llm_started_at / first_token_at / llm_completed_at / llm_first_token_ms / llm_total_ms
- tts_started_at / first_audio_at / tts_completed_at / tts_first_audio_ms / tts_total_ms
- total_latency_ms

Logs clean timing summaries and exports telemetry dict for UI monitoring.
"""

import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class LatencyTracker:
    def __init__(self, call_sid: Optional[str] = None):
        self.call_sid = call_sid or "unknown"
        self.t_zero = time.perf_counter()
        self.timestamps: Dict[str, float] = {
            "audio_received_at": round(self.t_zero, 4)
        }
        self.metrics: Dict[str, float] = {}

    def mark(self, event_name: str) -> float:
        """Record timestamp offset in seconds relative to turn start."""
        now = time.perf_counter()
        elapsed_ms = (now - self.t_zero) * 1000.0
        self.timestamps[event_name] = round(elapsed_ms, 2)
        return elapsed_ms

    def record(self, metric_name: str, duration_ms: float):
        """Record duration in milliseconds for a specific stage."""
        self.metrics[metric_name] = round(duration_ms, 2)

    def to_telemetry(self) -> Dict[str, Any]:
        """Export combined metrics and milestone timings for WebSocket UI updates."""
        total_ms = round((time.perf_counter() - self.t_zero) * 1000.0, 2)
        return {
            "call_sid": self.call_sid,
            "total_latency_ms": total_ms,
            "metrics": self.metrics,
            "milestones_ms": self.timestamps
        }

    def log_summary(self) -> Dict[str, Any]:
        """Output metric log summary without revealing caller PII."""
        telemetry = self.to_telemetry()
        total_ms = telemetry["total_latency_ms"]

        summary_parts = [f"CallSid={self.call_sid}", f"total={total_ms}ms"]
        for key, val in self.metrics.items():
            summary_parts.append(f"{key}={val}ms")

        logger.info("[PERF METRICS] " + " | ".join(summary_parts))
        return telemetry
