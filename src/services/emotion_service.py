"""
Third-Party Emotion Detection Service Client.

Emotion Detection is the ONLY component authorized to call an external API.
Executes asynchronously with a strict configurable timeout (`EMOTION_API_TIMEOUT`).
Fails gracefully to neutral default state without blocking or failing conversations.
"""

import asyncio
import logging
import os
import time
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class EmotionResult(BaseModel):
    emotion: str = Field(default="neutral", description="Detected emotion string, e.g. frustrated, happy, calm, neutral")
    confidence: float = Field(default=1.0, description="Confidence score between 0.0 and 1.0")


class EmotionService:
    def __init__(self):
        pass

    async def detect_emotion_async(self, text: str) -> EmotionResult:
        """
        Analyze emotion of caller text via third-party API.
        Executes within EMOTION_API_TIMEOUT. Returns neutral fallback on any error/timeout.
        """
        from src.config.env import settings

        api_url = getattr(settings, "EMOTION_API_URL", None) or os.environ.get("EMOTION_API_URL", "")
        api_key = getattr(settings, "EMOTION_API_KEY", None) or os.environ.get("EMOTION_API_KEY", "")
        timeout = float(getattr(settings, "EMOTION_API_TIMEOUT", 1.5))

        # Rule-based fast sentiment analysis as immediate local baseline/fallback
        local_emotion = self._fast_local_emotion(text)

        if not api_url or not api_key:
            # No third-party API configured; return local baseline smoothly
            return local_emotion

        t0 = time.perf_counter()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=timeout) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                payload = {"text": text}
                response = await client.post(api_url, json=payload, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    emo = data.get("emotion", "neutral")
                    conf = float(data.get("confidence", 0.90))
                    dur = (time.perf_counter() - t0) * 1000.0
                    logger.info(f"Emotion API returned {emo} ({conf}) in {dur:.2f} ms")
                    return EmotionResult(emotion=emo.lower(), confidence=conf)
                else:
                    logger.warning(f"Emotion API returned HTTP status {response.status_code}. Using fallback.")
        except asyncio.TimeoutError:
            logger.warning(f"Emotion API timed out after {timeout}s. Using default emotion fallback.")
        except Exception as exc:
            logger.warning(f"Emotion API failed ({exc}). Using default emotion fallback.")

        return local_emotion

    def _fast_local_emotion(self, text: str) -> EmotionResult:
        """Local baseline sentiment detection for fast fallback."""
        txt = (text or "").lower()
        if any(w in txt for w in ["angry", "upset", "frustrated", "terrible", "worst", "annoyed", "ridiculous", "hate"]):
            return EmotionResult(emotion="frustrated", confidence=0.85)
        if any(w in txt for w in ["urgent", "emergency", "help", "pain", "quick", "asap"]):
            return EmotionResult(emotion="urgent", confidence=0.90)
        if any(w in txt for w in ["happy", "great", "thank", "wonderful", "awesome", "excellent"]):
            return EmotionResult(emotion="happy", confidence=0.90)
        return EmotionResult(emotion="neutral", confidence=1.0)


emotion_service = EmotionService()
