import os
import io
import json
import logging
import re
from typing import Generator, Dict, Any, List, Optional

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
from pydantic import BaseModel, Field

import time
from src.config.env import settings
from src.services.stt_service import stt_service
from src.services.tts_service import tts_service
from src.services.guardrail_service import guardrail_service
from src.utils.text_sanitizer import clean_markdown_for_voice

logger = logging.getLogger(__name__)


class AIService:
    """
    Universal, Low-Latency AI Receptionist Engine.
    Features:
    - Primary: Google Gemini Flash (sub-second generation)
    - Secondary Fallback: Mistral AI (fast failover)
    - Strict RAG Grounding & Anti-Hallucination
    - General LLM Fallback for Generic Knowledge
    - Clean Prompt Demarcation ([SYSTEM], [RAG CONTEXT], [CONVERSATION], [USER QUERY])
    """
    _exhausted_gemini_models: Dict[str, float] = {}

    def _get_verified_gemini_models(self) -> List[str]:
        now = time.monotonic()
        primary_model = getattr(settings, "GEMINI_MODEL", "gemini-3.5-flash") or "gemini-3.5-flash"
        candidates = [primary_model, "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]
        obsolete = {"gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest"}
        valid = []
        for m in candidates:
            if not m or m in valid:
                continue
            if m in obsolete and m != primary_model:
                continue
            cooldown = self._exhausted_gemini_models.get(m, 0.0)
            if now < cooldown:
                continue
            valid.append(m)
        if not valid:
            valid = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"]
        return valid

    def _mark_gemini_model_exhausted(self, model: str, error_msg: str):
        low = str(error_msg).lower()
        if "resource_exhausted" in low or "429" in low or "quota" in low:
            logger.warning(f"[GEMINI QUOTA] Model '{model}' exhausted quota. Placing in cooldown.")
            self._exhausted_gemini_models[model] = time.monotonic() + 1800.0
        elif "503" in low or "unavailable" in low or "high demand" in low:
            logger.warning(f"[GEMINI 503] Model '{model}' high demand. Placing in short cooldown.")
            self._exhausted_gemini_models[model] = time.monotonic() + 60.0
        elif "not_found" in low or "404" in low or "no longer available" in low:
            logger.warning(f"[GEMINI OBSOLETE] Model '{model}' not found (404). Placing in cooldown.")
            self._exhausted_gemini_models[model] = time.monotonic() + 86400.0

    def _build_gemini_config(self, target_model: str, system_prompt: str, max_tokens: int = 300):
        cfg_kwargs = dict(
            system_instruction=system_prompt,
            temperature=0.2,
            max_output_tokens=max_tokens
        )
        if hasattr(types, "ThinkingConfig"):
            try:
                if "3.5" in target_model:
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="MINIMAL", include_thoughts=False)
                elif "3.7" in target_model or "3.8" in target_model:
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
        return types.GenerateContentConfig(**cfg_kwargs)

    def __init__(self):
        from src.config.env import settings

        # 1. Google Gemini Primary Client
        gemini_key = getattr(settings, "GEMINI_API_KEY", None)
        self.gemini_client = genai.Client(api_key=gemini_key) if (genai and gemini_key) else None
        if self.gemini_client:
            logger.info("[AI SERVICE] Google Gemini client initialized successfully.")

        # 2. Mistral Secondary Client
        mistral_key = getattr(settings, "MISTRAL_API_KEY", None)
        mistral_url = getattr(settings, "MISTRAL_BASE_URL", "https://api.mistral.ai/v1") or "https://api.mistral.ai/v1"
        if mistral_key and OpenAI:
            self.mistral_client = OpenAI(
                api_key=mistral_key,
                base_url=mistral_url,
                timeout=5.0,
                max_retries=1
            )
            logger.info("[AI SERVICE] Mistral LLM client initialized successfully.")
        else:
            self.mistral_client = None

        # 3. xAI / Grok Client
        xai_key = getattr(settings, "XAI_API_KEY", None)
        xai_url = getattr(settings, "XAI_BASE_URL", "https://api.x.ai/v1") or "https://api.x.ai/v1"
        if xai_key and OpenAI:
            self.xai_client = OpenAI(
                api_key=xai_key,
                base_url=xai_url,
                timeout=5.0,
                max_retries=1
            )
        else:
            self.xai_client = None

        # 4. Local Ollama Client
        ollama_url = getattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434/v1")
        if OpenAI:
            self.ollama_client = OpenAI(
                base_url=ollama_url,
                api_key="ollama",
                max_retries=0,
                timeout=1.0
            )
        else:
            self.ollama_client = None

    def _check_ollama_online(self) -> bool:
        if hasattr(self, "_ollama_online_status"):
            return self._ollama_online_status
        try:
            import socket
            from urllib.parse import urlparse
            from src.config.env import settings
            ollama_url = getattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434/v1")
            parsed = urlparse(ollama_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 11434
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.05)
            result = sock.connect_ex((host, port))
            sock.close()
            self._ollama_online_status = (result == 0)
        except Exception:
            self._ollama_online_status = False
        return self._ollama_online_status

    def _get_provider(self) -> str:
        from src.config.env import settings
        provider = getattr(settings, "LLM_PROVIDER", "gemini").lower().strip()
        if provider == "xai" and getattr(self, "xai_client", None):
            return "xai"
        if provider == "gemini" and self.gemini_client:
            return "gemini"
        if provider == "mistral" and self.mistral_client:
            return "mistral"
        if getattr(self, "xai_client", None):
            return "xai"
        if self.gemini_client:
            return "gemini"
        if self.mistral_client:
            return "mistral"
        return "grounded_rag"

    def _get_system_instruction(self, organization_name: Optional[str] = None, is_ongoing: bool = False, is_rag_active: bool = False, emotion: str = "neutral") -> str:
        org_title = organization_name or "the organization"

        emotion_lower = (emotion or "neutral").lower()
        if "angry" in emotion_lower or "frustrat" in emotion_lower:
            emotion_directive = "\nEMOTION/TONE ADAPTATION: The caller sounds frustrated or upset. Respond with a calm, highly empathetic, and reassuring tone. Prioritize resolving their need efficiently."
        elif "confus" in emotion_lower:
            emotion_directive = "\nEMOTION/TONE ADAPTATION: The caller sounds confused. Use simple, crystal-clear explanations with step-by-step guidance."
        elif "urgent" in emotion_lower or "emergency" in emotion_lower:
            emotion_directive = "\nEMOTION/TONE ADAPTATION: The request is urgent. Be direct, concise, and prioritize immediate helpful action."
        elif "happy" in emotion_lower or "positive" in emotion_lower:
            emotion_directive = "\nEMOTION/TONE ADAPTATION: The caller is cheerful. Maintain a warm, friendly, and courteous demeanor."
        else:
            emotion_directive = "\nEMOTION/TONE ADAPTATION: Maintain a polite, professional, and attentive receptionist tone."

        rag_mode_directive = (
            "\n=== ACTIVE RAG KNOWLEDGE BASE DIRECTIVE (RAG IS ACTIVE) ===\n"
            "An ACTIVE RAG Knowledge Base document is currently loaded for this conversation.\n"
            "1. BUSINESS INQUIRIES & FACTUAL RECORDS: When answering questions about the organization, services, staff, timings, fees, policies, or locations, rely strictly on [RAG CONTEXT]. If a specific business detail is not found in [RAG CONTEXT], politely state: 'I apologize, but that specific information is not available in our active records. How else may I assist you?'\n"
            "2. GENERAL KNOWLEDGE & TRIVIA: For general knowledge, science, physics, history, geography, or trivia questions unrelated to the business (such as 'speed of light', 'capital of France'), answer directly and concisely using general knowledge.\n"
            "3. CASUAL CONVERSATION & GREETINGS: For greetings, status updates, or casual conversational remarks (e.g. 'how are you', 'sounds good', 'thanks'), respond naturally, warmly, and helpfully as an AI receptionist.\n"
            "4. ZERO MEDICAL MENTIONS: NEVER mention 'doctors', 'medical departments', 'hospitals', or 'patients' unless the active RAG specifically lists them.\n\n"
            if is_rag_active else
            "\n=== DEFAULT AI RECEPTIONIST DIRECTIVE (NO ACTIVE RAG) ===\n"
            "No custom RAG document is currently active. Answer using neutral AI Receptionist behavior without inventing business details.\n\n"
        )

        continuity_rule = (
            "\n11. STRICT CONVERSATION CONTINUITY & STATE SURVIVAL:\n"
            "   - This is an ONGOING conversation turn. NEVER restart the conversation, re-introduce the business, say 'Welcome to...', or repeat an opening greeting.\n"
            "   - Directly address the caller's specific statement or question naturally without preamble or repetitive greetings.\n"
            "   - Maintain active booking state across turns (Intent, Service/Department, Representative/Option, Date, Time, Name, Email, Phone).\n"
            "   - When an appointment time is selected/confirmed, DO NOT RESET STATE. Immediately move to the next missing required field (Full Name -> Email -> Phone Number).\n"
            "   - If the caller provides multiple details in one sentence (e.g. name, email, phone), capture all of them simultaneously.\n"
            "   - If the caller modifies a previously selected value (e.g. 'Actually make it 2 PM'), update ONLY that specific field without clearing other collected information.\n"
            "   - NEVER ask for information that already exists in [CONVERSATION STATE].\n"
            if is_ongoing else ""
        )
        return (
            f"You are an AI Receptionist for {org_title}. Your primary responsibility is to maintain complete conversation continuity throughout the entire call, understand caller speech accurately, retrieve correct RAG information, and prompt ONLY for remaining missing details.\n\n"
            f"{rag_mode_directive}"
            "=== CORE RECEPTIONIST PIPELINE & CONVERSATION CONTINUITY RULES ===\n"
            "1. GREETING:\n"
            "   - Start the call with a simple, professional time-appropriate greeting.\n"
            "   - Do NOT repeat the greeting multiple times during the same call. Once greeted, continue directly based on the caller's request.\n\n"
            "2. UNIVERSAL CONVERSATION CONTINUITY (NEVER RESTART):\n"
            "   - Remember and reuse all information provided during the call. Never return to previously completed steps.\n"
            "   - Follow the linear booking sequence: Intent -> Service/Department -> Representative/Option -> Date -> Time Selection -> Full Name -> Email Address -> Phone Number -> Confirmation.\n"
            "   - Skip any step that is already completed. Never ask 'What service would you like to book?' when service is already known.\n\n"
            "3. RAG DOCUMENT RETRIEVAL & LONG DOCUMENT EXTRACTION:\n"
            "   - Search the complete RAG knowledge base for relevant information across all uploaded sections, pages, headings, tables, product lists, and timings.\n"
            "   - RAG queries and API calls MUST NOT reset the conversation state.\n"
            "   - If information is unlisted in RAG, clearly state it is unavailable instead of inventing facts.\n\n"
            "4. LANGUAGE DETECTION & INTENT TRANSLATION:\n"
            "   - Automatically detect caller language (English, Tanglish, Tamil, Hindi, Hinglish, Malayalam, Telugu, Kannada, etc.).\n"
            "   - Focus on core intent and meaning without translation glitches.\n"
            "   - Preserve context and meaning seamlessly without word-for-word translation glitches.\n\n"
            "5. ABSOLUTE DOMAIN ISOLATION & ZERO MEDICAL MENTIONS:\n"
            "   - Preserve exact spelling of product/staff names, departments, services, proper nouns, and terms from RAG.\n"
            "   - NEVER ASSUME THE BUSINESS TYPE IS A HOSPITAL OR CLINIC. NEVER MENTION OR SUGGEST DOCTORS, PATIENTS, OR MEDICAL DEPARTMENTS (such as Cardiology, Dentist, Ophthalmology, Pediatrics, General Medicine), EVEN WHEN DENYING AN UNLISTED QUESTION OR EXPLAINING MISSING INFORMATION.\n"
            "   - Adapt your options and suggestions strictly to the products, services, or topics in the active RAG document.\n\n"
            "6. VOICE INPUT ACCURACY & CONTEXTUAL CORRECTION:\n"
            "   - Use conversation context and active RAG lists to resolve minor STT ambiguities.\n"
            "   - Rejects random background noise or unrelated words.\n\n"
            "7. HANDLING UNCLEAR VOICE INPUT:\n"
            "   - If speech input is uncertain or confidence is low, DO NOT GUESS. Ask a single direct clarification question.\n\n"
            "8. RESPONSE LATENCY & CONCISENESS:\n"
            "   - Respond quickly with short, natural spoken plain text (1 to 3 sentences).\n"
            "   - Do NOT repeat the caller's entire sentence back to them unless explicit confirmation is needed.\n"
            "   - For multi-step tasks, ask ONLY for the single next missing piece of information.\n\n"
            "9. REPETITION PREVENTION & STATE SURVIVAL:\n"
            "   - Continuously track collected vs. missing fields ([CONVERSATION STATE]).\n"
            "   - NEVER ask for information that the caller has already provided.\n\n"
            "10. DYNAMIC APPOINTMENT FLOW & HARDCODING PREVENTION:\n"
            "   - NEVER use hardcoded default services. Ask 'What product or service would you like assistance with?' if unmentioned.\n"
            "   - DO NOT ASSUME THE BUSINESS TYPE. Adapt dynamically to active RAG.\n"
            "   - DO NOT ASK FOR A DOCTOR OR HOSPITAL DEPARTMENT UNLESS THE ACTIVE RAG SPECIFICALLY LISTS THEM.\n"
            "   - DO NOT TREAT ORDINARY CONVERSATIONAL WORDS OR PHRASES AS USER NAMES.\n\n"
            "11. VOICE RESPONSE FORMATTING & NATURAL CONVERSATIONAL SPEECH:\n"
            "   - SPOKEN CONTINUOUS HUMAN DIALOGUE: Speak naturally as a friendly, helpful human receptionist on a phone call. Keep speech flowing smoothly.\n"
            "   - CONCISE (1 TO 3 SENTENCES): Never produce long documentation dumps or monologues. Give a concise conversational response of 1 to 3 spoken sentences.\n"
            "   - ZERO MARKDOWN, HEADINGS, OR BULLETS: ABSOLUTELY NO asterisks (**bold**), NO bullet points, NO numbered lists, NO dashes (-), NO colons (:), NO section headings. If information is a list in RAG, convert it into smooth spoken sentences (e.g. 'We offer haircuts, coloring, and styling. What service would you like?').\n"
            "   - NATURAL TRANSITIONS & CONTRACTIONS: Use friendly conversational transitions ('Sure!', 'Of course!', 'I can help with that!') and natural contractions ('I\'m', 'we\'re', 'it\'s', 'can\'t').\n"
            "   - ONE NATURAL FOLLOW-UP: Ask at most one relevant follow-up question to keep the conversation flowing.\n"
            "   - ZERO METADATA LEAKAGE: Never mention chunk IDs, RAG context, system prompts, or internal document headers."
            + continuity_rule
        )

    def _fast_rule_response(
        self,
        text: str,
        intent: str = "general_question",
        organization_id: Optional[int] = None,
        is_ongoing: bool = False,
        booking_session=None,
        has_context: bool = False
    ) -> str | None:
        """
        Sub-50ms rule-based responses for common conversational pleasantries and basic identity questions.
        Strictly bound to the currently active RAG document.

        Language-switch requests are handled here as the HIGHEST-PRIORITY fast rule,
        before any RAG lookup, entity validation, or LLM call.
        """
        txt = text.lower().strip()
        txt_clean = re.sub(r'[^a-zA-Z0-9\s]', '', txt).strip()
        from src.services.cache_service import cache_service
        from src.services.rag_service import rag_service
        from src.services.language_service import language_service

        # ------------------------------------------------------------------ #
        # PRIORITY 0 — Language Switch (must fire before any RAG/LLM call)    #
        # ------------------------------------------------------------------ #
        target_lang, is_switch = language_service.check_explicit_language_switch(text)
        if is_switch and target_lang:
            return language_service.get_language_switch_confirmation(
                target_lang, booking_session=booking_session
            )
        active_doc_id = rag_service._resolve_active_document_id(organization_id)
        org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id)
        welcome_greeting = rag_service.get_organization_greeting(organization_id, document_id=active_doc_id)

        # ------------------------------------------------------------------ #
        # Pure conversational checks: must respond even if background RAG context exists
        # ------------------------------------------------------------------ #
        if any(phrase in txt_clean for phrase in ["can you hear me", "are you there", "am i audible", "can you hear", "hear me", "is anyone there"]):
            if booking_session and hasattr(booking_session, "is_active") and booking_session.is_active():
                from src.services.booking_state_service import booking_state_service
                resume_prompt = booking_state_service.get_silence_prompt(booking_session)
                return f"Yes, I can hear you clearly! {resume_prompt}"
            elif isinstance(booking_session, dict) and booking_session.get("state") not in (None, "IDLE", "COMPLETED"):
                return "Yes, I can hear you clearly! We were discussing your appointment. How would you like to proceed?"
            return "Yes, I can hear you clearly! How may I assist you today?"
        if any(phrase in txt_clean for phrase in ["who is this", "who am i speaking with", "who am i talking to"]):
            return f"This is the AI receptionist for {org_name or 'our office'}. How may I assist you today?"
        if any(phrase in txt_clean for phrase in ["how are you", "how are you doing", "whats up"]):
            return "I am doing great, thank you! How may I assist you today?"
        if any(p in txt_clean for p in ["thank you", "thanks", "thank u", "appreciated"]):
            return "You are very welcome! Please let me know if you need any other assistance."
        if any(w in txt for w in ["bye", "goodbye", "see you", "have a good day", "exit"]) or intent == "goodbye":
            return f"Thank you for contacting {org_name or 'us'}. Have a wonderful day!"
        if txt_clean in {"yeah good", "doing good", "good", "great", "im good", "i am good", "fine", "all good"}:
            if booking_session and hasattr(booking_session, "is_active") and booking_session.is_active():
                from src.services.booking_state_service import booking_state_service
                resume_prompt = booking_state_service.get_silence_prompt(booking_session)
                return f"Glad to hear! {resume_prompt}"
            return "Glad to hear! How may I assist you today?"
        if txt_clean in {"i see", "okay", "alright", "got it", "sure", "cool", "ok"}:
            if booking_session and hasattr(booking_session, "is_active") and booking_session.is_active():
                from src.services.booking_state_service import booking_state_service
                resume_prompt = booking_state_service.get_silence_prompt(booking_session)
                return f"Understood. {resume_prompt}"
            return "Understood. How can I assist you further?"

        # ------------------------------------------------------------------ #
        # Guard: Fast rules must NEVER intercept explicit RAG context or active booking #
        # ------------------------------------------------------------------ #
        if has_context:
            return None
        if booking_session and hasattr(booking_session, "is_active") and booking_session.is_active():
            return None
        if any(w in txt for w in ["book", "booking", "appointment", "schedule", "reserve", "slot", "slots"]):
            return None
        # ------------------------------------------------------------------ #

        already_greeted = is_ongoing or bool(
            (booking_session and getattr(booking_session, "greeting_sent", False))
            or (isinstance(booking_session, dict) and booking_session.get("greeting_sent"))
        )

        greetings = {
            "hi", "hello", "hey", "good morning", "good afternoon", "good evening",
            "greetings", "hello there", "hi there", "hey there", "vanakkam", "namaste"
        }

        # Pure greeting: caller ONLY said a greeting word without asking a question or mentioning services
        question_or_entity_words = {"what", "who", "where", "how", "when", "which", "is", "are", "do", "does", "can", "could", "service", "services", "book", "booking", "appointment", "doctor", "doctors", "department", "hospital", "clinic", "timing", "hours"}
        is_pure_greeting = (txt_clean in greetings or intent == "greeting") and not any(w in txt_clean.split() for w in question_or_entity_words)

        if is_pure_greeting and already_greeted:
            return "Yes, how may I assist you?"

        if any(w in txt for w in ["bye", "goodbye", "see you", "have a good day", "exit"]) or intent == "goodbye":
            return f"Thank you for contacting {org_name or 'us'}. Have a wonderful day!"

        if any(p in txt_clean for p in ["thank you", "thanks", "thank u", "appreciated", "that is all", "that's all", "that will be all"]) or intent in ("thanks", "gratitude"):
            return "You are very welcome! Please let me know if you need any other assistance."

        resp = None
        if txt_clean in greetings or intent == "greeting":
            resp = welcome_greeting
        elif any(p in txt_clean for p in ["dont be an idiot", "dont be a idiot", "be an idiot", "are you an idiot", "stop being an idiot", "be serious"]):
            resp = "I apologize for any confusion. How may I assist you with our services today?"
        elif any(p in txt for p in ["about the receptionist", "know about the receptionist", "who is the receptionist", "what is the receptionist", "tell me about the receptionist", "about receptionist", "what can you do", "what do you do", "how can you help", "tell me about yourself"]):
            if org_name:
                resp = f"Sure! I am the AI receptionist for {org_name}. I can help answer your questions, provide information about the business and its services, and assist with appointments. What would you like to know?"
            else:
                resp = "Sure! I am the AI receptionist. I can help answer your questions, provide information about the business and its services, and assist with appointments. What would you like to know?"
        elif re.search(r'\b(?:what(?:\s*is|\s*\'s|\s*s)?\s+(?:your|the)\s+(?:company|organization|business|salon|saloon|hospital|clinic|school|firm|hotel)\s+name|whats?\s+your\s+name|who\s+are\s+you|what\s+company\s+is\s+this|what\s+is\s+this\s+business)\b', txt_clean) or any(p in txt for p in ["company name", "organization name", "business name", "what is your name", "who are you"]):
            if org_name:
                resp = f"This is {org_name}. I am your AI receptionist. How may I assist you today?"
            else:
                resp = "I am your AI receptionist. How may I assist you today?"
        elif any(p in txt_clean for p in ["opening hours", "business hours", "work hours", "working hours", "timing", "timings", "open time", "close time", "opening time"]):
            hours_info = rag_service.get_organization_hours(organization_id, document_id=active_doc_id)
            if hours_info:
                resp = f"Our operating hours are {hours_info}. How else may I assist you?"
            else:
                resp = f"Operating hours are not specified in our active records for {org_name or 'our business'}."
        elif any(p in txt_clean for p in ["where are you located", "location", "address", "where is your", "where are you"]):
            loc_info = rag_service.get_organization_location(organization_id, document_id=active_doc_id)
            if loc_info:
                resp = f"We are located at {loc_info}. How else may I assist you?"
            else:
                resp = f"Address details are not specified in our active records for {org_name or 'our business'}."
        return resp


    def generate_response(
        self,
        text: str,
        context: str = "",
        session_history: list = None,
        intent: str = "general_question",
        emotion: str = "neutral",
        conversation_language: str = "en",
        organization_id: int = 1,
        conversation_state: Optional[Dict[str, Any]] = None,
        outbound_script: Optional[str] = None
    ) -> str:
        """
        Generates a natural, RAG-grounded receptionist response.
        Enforces Guardrails, Strict Context Demarcation, Language Lock, and Rapid Failover.
        """
        from src.config.env import settings
        from src.services.rag_service import rag_service

        is_ongoing = bool(
            (session_history and len(session_history) > 1) or
            (conversation_state and conversation_state.get("turn_count", 0) > 1) or
            (conversation_state and conversation_state.get("conversation_started")) or
            (conversation_state and conversation_state.get("greeting_sent"))
        )

        # Step 1: Prompt Injection Guardrail
        safety_check = guardrail_service.check_user_input_safety(text)
        if not safety_check["is_safe"] and safety_check.get("refusal_needed"):
            return safety_check["refusal_message"]

        # Step 2: Context Sanitization
        clean_context = ""
        if context:
            clean_context = re.sub(r'\[Source:.*?\]', '', context)
            clean_context = re.sub(r'\r\n|\r', '\n', clean_context)
            clean_context = re.sub(r'\n{3,}', '\n\n', clean_context).strip()[:2500]  # 2500 chars = ~400 words, enough for any answer

        # Step 3: Fast-Path Rule Response
        fast_resp = self._fast_rule_response(
            text, intent, organization_id=organization_id, is_ongoing=is_ongoing,
            booking_session=conversation_state, has_context=bool(clean_context)
        )
        if fast_resp:
            return guardrail_service.validate_and_sanitize_response(fast_resp)

        active_doc_id = rag_service._resolve_active_document_id(organization_id)
        is_rag_active = bool(active_doc_id)
        org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id)
        system_prompt = self._get_system_instruction(organization_name=org_name, is_ongoing=is_ongoing, is_rag_active=is_rag_active, emotion=emotion)

        if outbound_script:
            system_prompt = (
                f"=== CRITICAL OUTBOUND CALL INSTRUCTION ===\n"
                f"You are making an outbound call on behalf of {org_name}. "
                f"You MUST strictly follow this custom script/instruction for this specific call:\n"
                f"\"{outbound_script}\"\n"
                f"===========================================\n\n"
            ) + system_prompt

        # Language guidance
        lang_code = (conversation_language or "en").lower().strip()
        if lang_code in ("ta", "tanglish", "ta_mix"):
            system_prompt += "\nLANGUAGE LOCK: The caller is speaking in Tamil/Tanglish. You MUST respond in natural, polite Tamil or Tanglish in Latin script."
        elif lang_code in ("hi", "hinglish"):
            system_prompt += "\nLANGUAGE LOCK: The caller is speaking in Hindi. You MUST respond in polite, clear Hindi."
        elif lang_code in ("te", "telugu"):
            system_prompt += "\nLANGUAGE LOCK: The caller is speaking in Telugu. You MUST respond in polite, natural Telugu."
        elif lang_code in ("ml", "malayalam"):
            system_prompt += "\nLANGUAGE LOCK: The caller is speaking in Malayalam. You MUST respond in polite, natural Malayalam."
        elif lang_code in ("kn", "kannada"):
            system_prompt += "\nLANGUAGE LOCK: The caller is speaking in Kannada. You MUST respond in polite, natural Kannada."
        else:
            system_prompt += "\nLANGUAGE: Respond in clear, professional English."

        # Structured Prompt Assembly
        formatted_history = ""
        if session_history and len(session_history) > 0:
            history_lines = []
            for h in session_history[-3:]:  # Last 3 turns is enough context
                role_label = "Caller" if h.get("role") in ("user", "human") else "Receptionist"
                history_lines.append(f"{role_label}: {h.get('text', '')}")
            formatted_history = "\n".join(history_lines)

        conv_state_block = ""
        if conversation_state:
            collected = conversation_state.get("collected_fields") or {}
            missing = conversation_state.get("missing_fields") or []
            stage = conversation_state.get("current_stage") or "IDLE"
            c_intent = conversation_state.get("current_intent") or intent
            conv_state_block = (
                f"=== [CONVERSATION STATE] ===\n"
                f"Intent: {c_intent}\n"
                f"Stage: {stage}\n"
                f"Collected Information: {collected}\n"
                f"Missing Information: {missing}\n\n"
            )

        context_block = (
            f"=== [RAG CONTEXT (ACTIVE KNOWLEDGE BASE)] ===\n{clean_context}\n"
            if clean_context else
            "=== [RAG CONTEXT (ACTIVE KNOWLEDGE BASE)] ===\nNo specific business knowledge base records retrieved for this query. If the caller is asking a general knowledge, science, physics, history, geography, or trivia question (such as speed of light or capital cities), answer it directly using general knowledge.\n"
        )

        user_content = (
            f"{context_block}\n"
            f"{conv_state_block}"
            f"=== [RECENT CONVERSATION] ===\n{formatted_history or 'No previous turns.'}\n\n"
            f"=== [CURRENT USER QUERY] ===\n{text}"
        )

        raw_result = None
        provider = self._get_provider()
        gemini_model_name = getattr(settings, "GEMINI_MODEL", "gemini-3.6-flash")

        # 1. Primary: Google Gemini / Mistral according to provider selection
        if provider == "mistral" and self.mistral_client:
            try:
                mistral_model = getattr(settings, "MISTRAL_MODEL", "open-mistral-7b") or "open-mistral-7b"
                messages = [{"role": "system", "content": system_prompt}]

                if session_history:
                    for h in session_history[-3:]:
                        role = "assistant" if h.get("role") in ("model", "assistant") else "user"
                        messages.append({"role": role, "content": h.get("text", "")})

                messages.append({"role": "user", "content": user_content})

                response = self.mistral_client.chat.completions.create(
                    model=mistral_model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=75
                )
                ans = response.choices[0].message.content
                if ans and ans.strip():
                    raw_result = ans.strip()
            except Exception as e:
                logger.error(f"[MISTRAL ERROR] Fast Mistral generation failed: {e}")

        if not raw_result and self.gemini_client:
            gemini_models = self._get_verified_gemini_models()

            contents = []
            if session_history and intent not in ("greeting", "general_conversation"):
                for h in session_history[-3:]:
                    role = "model" if h.get("role") in ("model", "assistant") else "user"
                    contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h.get("text", ""))]))
 
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_content)]))

            for target_model in gemini_models:
                try:
                    config = self._build_gemini_config(target_model, system_prompt, max_tokens=300)
                    response = self.gemini_client.models.generate_content(
                        model=target_model,
                        contents=contents,
                        config=config
                    )
                    if response and response.text and response.text.strip():
                        raw_result = response.text.strip()
                        break
                except Exception as e:
                    self._mark_gemini_model_exhausted(target_model, str(e))
                    logger.warning(f"[GEMINI WARNING] Generation failed with model '{target_model}': {e}")

        # 2. Secondary Fallback: Mistral AI (if primary Gemini was used & failed)
        if not raw_result and self.mistral_client:
            try:
                mistral_model = getattr(settings, "MISTRAL_MODEL", "open-mistral-7b") or "open-mistral-7b"
                messages = [{"role": "system", "content": system_prompt}]

                if session_history:
                    for h in session_history[-3:]:
                        role = "assistant" if h.get("role") in ("model", "assistant") else "user"
                        messages.append({"role": role, "content": h.get("text", "")})

                messages.append({"role": "user", "content": user_content})

                response = self.mistral_client.chat.completions.create(
                    model=mistral_model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=250
                )
                ans = response.choices[0].message.content
                if ans and ans.strip():
                    raw_result = ans.strip()
            except Exception as e:
                logger.error(f"[MISTRAL ERROR] Secondary generation failed: {e}")

        # 3. Tertiary Fallback: Local Ollama (Only if daemon is online)
        if not raw_result and self.ollama_client and self._check_ollama_online():
            try:
                model_name = getattr(settings, "LLM_MODEL", getattr(settings, "OLLAMA_MODEL", "llama3.2"))
                response = self.ollama_client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    temperature=0.2,
                    max_tokens=300
                )
                ans = response.choices[0].message.content
                if ans and ans.strip():
                    raw_result = ans.strip()
            except Exception as e:
                logger.warning(f"[OLLAMA ERROR] Local fallback failed: {e}")

        if raw_result:
            clean_res = clean_markdown_for_voice(raw_result)
            return guardrail_service.validate_and_sanitize_response(clean_res)

        # 4. Smart Dynamic RAG Extraction & Synthesis Fallback
        if clean_context:
            from src.services.rag_service import rag_service
            active_doc_id = rag_service._resolve_active_document_id(organization_id)
            org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id)
            txt_low = text.lower().strip()

            # Structured Section and Key-Value Extraction from Active RAG Document
            query_tokens = set(re.findall(r'\b[a-zA-Z0-9_]+\b', txt_low))

            # Parse markdown (# SECTION) or bracket ([SECTION]) headers
            sections = {}
            sec_pattern = re.compile(r'(?:^|\n)(?:#+\s*|\[)([A-Za-z0-9\s&/-]+)\]?\s*\n(.*?)(?=(?:\n(?:#+\s*|\[)[A-Za-z0-9\s&/-]+\]?)|\Z)', re.DOTALL)
            for m in sec_pattern.finditer(clean_context):
                primary_title = m.group(1).strip().upper().split('\n')[0].strip()
                sections[primary_title] = m.group(2).strip()

            services_match = re.search(r'(?:Services|Specialties|Departments|Offerings|Treatments|Procedures|Menu)\s*:\s*([^\n]+)', clean_context, re.IGNORECASE)
            staff_match = re.search(r'(?:Stylists|Stylist|Staff|Doctors|Doctor|Team|Lawyers|Attorneys|Therapists|Specialists)\s*:\s*([^\n]+)', clean_context, re.IGNORECASE)
            hours_match = re.search(r'(?:Working Hours|Hours|Timings|Schedule|Opening Hours)\s*:\s*([^\n]+)', clean_context, re.IGNORECASE)
            addr_match = re.search(r'(?:Address|Location|Situated at)\s*:\s*([^\n]+)', clean_context, re.IGNORECASE)

            # 1. Staff / Stylists / Doctors check
            staff_keywords = {"stylist", "stylists", "staff", "doctor", "doctors", "team", "expert", "specialist", "specialists", "lawyer", "lawyers", "attorney", "physician", "physicians", "who"}
            doc_pattern = re.compile(
                r'(?:^|\n)(Dr\.\s+[A-Za-z]+(?:\s+[A-Za-z]+)*)\s*\n((?:[ \t]*(?:Role|Department|Specialization|Availability|Mode|Hours|Working Hours|Available):[^\n]+\s*\n*)+)',
                re.IGNORECASE
            )
            parsed_doctors = []
            seen_doc_names = set()
            for m in doc_pattern.finditer(clean_context):
                doc_name = m.group(1).strip()
                doc_meta = m.group(2).strip()
                if doc_name in seen_doc_names:
                    continue
                seen_doc_names.add(doc_name)
                role_m = re.search(r'Role:\s*([^\n]+)', doc_meta, re.IGNORECASE)
                dept_m = re.search(r'Department:\s*([^\n]+)', doc_meta, re.IGNORECASE)
                spec_m = re.search(r'Specialization:\s*([^\n]+)', doc_meta, re.IGNORECASE)
                avail_m = re.search(r'(?:Availability|Working Hours|Hours|Available):\s*([^\n]+)', doc_meta, re.IGNORECASE)
                parsed_doctors.append({
                    "name": doc_name,
                    "role": role_m.group(1).strip() if role_m else "",
                    "dept": dept_m.group(1).strip() if dept_m else "",
                    "spec": spec_m.group(1).strip() if spec_m else "",
                    "avail": avail_m.group(1).strip() if avail_m else ""
                })

            if parsed_doctors:
                # Check for specific doctor query
                for doc in parsed_doctors:
                    d_words = set(re.findall(r'\b[a-zA-Z0-9]+\b', doc["name"].lower())) - {'dr', 'doctor'}
                    if d_words and any(w in query_tokens for w in d_words):
                        title = doc["role"] or (f"Department of {doc['dept']}" if doc["dept"] else "") or (f"specializing in {doc['spec']}" if doc["spec"] else "")
                        time_str = f", available {doc['avail']}" if doc["avail"] else ""
                        resp = f"{doc['name']} is our {title or 'doctor'}{time_str}."
                        return guardrail_service.validate_and_sanitize_response(resp)

                # Check for time-based doctor queries (e.g., "available in 01:00", "at 1:00 PM", "available at 10 AM", etc.)
                time_tokens = re.findall(r'\b(?:0?[1-9]|1[0-2])(?::[0-5][0-9])?(?:\s*(?:am|pm))?\b', txt_low)
                has_time_query = bool(time_tokens or "01:00" in txt_low or "1:00" in txt_low or "10:00" in txt_low or "03:00" in txt_low or "3:00" in txt_low or "04:00" in txt_low or "4:00" in txt_low)
                if has_time_query:
                    matching_docs = []
                    for doc in parsed_doctors:
                        avail_low = doc["avail"].lower()
                        for t in time_tokens:
                            if t and (t.replace(" ", "") in avail_low.replace(" ", "")):
                                matching_docs.append(doc)
                                break
                        if "01:00" in txt_low and "01:00" in avail_low and doc not in matching_docs:
                            matching_docs.append(doc)
                        elif ("1:00" in txt_low or " 1 " in f" {txt_low} ") and "01:00" in avail_low and doc not in matching_docs:
                            matching_docs.append(doc)
                        elif ("10:00" in txt_low or "10 am" in txt_low) and "10:00" in avail_low and doc not in matching_docs:
                            matching_docs.append(doc)
                        elif ("3:00" in txt_low or "03:00" in txt_low or "3 pm" in txt_low) and "03:00" in avail_low and doc not in matching_docs:
                            matching_docs.append(doc)
                    if matching_docs:
                        doc_summaries = [f"{d['name']} ({d['role'] or d['dept'] or 'consultation'})" for d in matching_docs]
                        doc_list_str = ", ".join(doc_summaries[:-1]) + f", and {doc_summaries[-1]}" if len(doc_summaries) > 1 else doc_summaries[0]
                        resp = f"The doctors available around that time are {doc_list_str}. Would you like to schedule an appointment?"
                        return guardrail_service.validate_and_sanitize_response(resp)

                # General doctor query
                if (query_tokens & staff_keywords) or any(p in txt_low for p in ["who works", "who are", "your team", "your staff", "your stylists", "your doctors", "list doctors", "available doctors"]):
                    doc_summaries = [f"{d['name']} ({d['role'] or d['dept'] or 'Doctor'})" for d in parsed_doctors]
                    docs_str = ", ".join(doc_summaries[:-1]) + f", and {doc_summaries[-1]}" if len(doc_summaries) > 1 else doc_summaries[0]
                    resp = f"Our medical team at {org_name or 'our clinic'} includes {docs_str}. Would you like to schedule an appointment with one of them?"
                    return guardrail_service.validate_and_sanitize_response(resp)

            elif (query_tokens & staff_keywords) or any(p in txt_low for p in ["who works", "who are", "your team", "your staff", "your stylists", "your doctors"]):
                if staff_match:
                    staff_list = re.sub(r'[\.\s]+$', '', staff_match.group(1).strip())
                    resp = f"Our team at {org_name or 'our business'} includes {staff_list}. Would you like to schedule an appointment with one of them?"
                    return guardrail_service.validate_and_sanitize_response(resp)

            # 2. Departments Check
            dept_sec = None
            for k, v in sections.items():
                if "DEPARTMENT" in k:
                    dept_sec = v
                    break
            if dept_sec and any(w in query_tokens for w in ["department", "departments", "specialties"]):
                dept_lines = [re.sub(r'^[-*•\s]+', '', l).strip() for l in dept_sec.split('\n') if l.strip().startswith(('-', '*', '•'))]
                if dept_lines:
                    depts_str = ", ".join(dept_lines[:-1]) + f", and {dept_lines[-1]}" if len(dept_lines) > 1 else dept_lines[0]
                    resp = f"Our active medical departments include {depts_str}. How can I assist you with these?"
                    return guardrail_service.validate_and_sanitize_response(resp)

            # 3. Services & Facilities Check
            serv_sec = None
            for k, v in sections.items():
                if any(s in k for s in ["SERVICE", "FACILIT"]):
                    serv_sec = v
                    break
            if serv_sec and any(w in query_tokens for w in ["service", "services", "facility", "facilities", "offer", "offerings"]):
                serv_lines = [re.sub(r'^[-*•\s]+', '', l).strip() for l in serv_sec.split('\n') if l.strip().startswith(('-', '*', '•'))]
                if serv_lines:
                    serv_str = ", ".join(serv_lines[:-1]) + f", and {serv_lines[-1]}" if len(serv_lines) > 1 else serv_lines[0]
                    resp = f"Our facilities and services include {serv_str}."
                else:
                    clean_s = re.sub(r'\s+', ' ', serv_sec).strip()
                    resp = f"At {org_name or 'our clinic'}, {clean_s}"
                return guardrail_service.validate_and_sanitize_response(resp)

            # 4. Structured Q&A Matching (e.g. Q: ... A: ...)
            qa_pairs = re.findall(r'(?:Q|Question):\s*([^\n\r?]+(?:\?|[^\n\r]*))\s*(?:A|Answer):\s*([^\n\r]+(?:\n(?!(?:Q|Question):)[^\n\r]+)*)', clean_context, re.IGNORECASE)
            best_qa_ans = None
            best_qa_score = 0
            stopwords = {"what", "is", "the", "a", "an", "of", "and", "in", "to", "for", "me", "tell", "you", "your", "can", "please", "about", "are", "do", "does", "how", "where", "when", "which", "who", "why"}
            generic_category_words = {"department", "departments", "service", "services", "teacher", "teachers", "doctor", "doctors", "staff", "member", "members", "option", "options", "detail", "details", "info", "information"}
            query_words = [w.lower() for w in re.findall(r'\b[a-zA-Z0-9]{3,}\b', text) if w.lower() not in stopwords]
            specific_query_words = [w for w in query_words if w not in generic_category_words]

            for q_text, a_text in qa_pairs:
                q_words = set(re.findall(r'\b[a-zA-Z0-9]{3,}\b', q_text.lower()))
                match_cnt = sum(1 for qw in query_words if qw in q_words)
                specific_match = (not specific_query_words) or any(sqw in q_words for sqw in specific_query_words)
                if match_cnt > best_qa_score and specific_match:
                    best_qa_score = match_cnt
                    best_qa_ans = a_text.strip()

            if best_qa_ans and best_qa_score >= 1:
                clean_ans = re.sub(r'^(?:A|Answer):\s*', '', best_qa_ans, flags=re.IGNORECASE).strip()
                clean_ans = re.sub(r'\s+', ' ', clean_ans)
                return guardrail_service.validate_and_sanitize_response(clean_ans)

            # 5. Timings / Working Hours check
            hours_keywords = {"hour", "hours", "timing", "timings", "schedule", "opening"}
            if (query_tokens & hours_keywords) or any(p in txt_low for p in ["open", "close", "when are you", "what time do you"]):
                if hours_match:
                    hours_val = re.sub(r'[\.\s]+$', '', hours_match.group(1).strip())
                    resp = f"Our working hours at {org_name or 'our location'} are {hours_val}."
                    return guardrail_service.validate_and_sanitize_response(resp)

            # 6. Location / Address check
            loc_keywords = {"location", "address", "locate", "directions", "situated"}
            if (query_tokens & loc_keywords) or "where" in query_tokens:
                if addr_match:
                    addr_val = re.sub(r'[\.\s]+$', '', addr_match.group(1).strip())
                    resp = f"{org_name or 'We are'} located at {addr_val}."
                    return guardrail_service.validate_and_sanitize_response(resp)

            # 7. General Services / Offerings / Departments fallback listing check
            is_general_services_query = any(p in txt_low for p in [
                "what are the departments", "what are your departments", "list departments",
                "what services do you offer", "list services", "what services",
                "what do you offer", "what are your services", "what can i get here",
                "what is available", "what are all the services", "what are the offerings"
            ])
            if is_general_services_query and services_match:
                services_list = re.sub(r'[\.\s]+$', '', services_match.group(1).strip())
                resp = f"At {org_name or 'our organization'}, our offerings and departments include: {services_list}."
                return guardrail_service.validate_and_sanitize_response(resp)

            # 8. About Us / Organization Info Check
            about_match = re.search(r'About Us:\s*([^\n]+(?:\n(?!(?:#|\[|[A-Za-z]+:))[^\n]+)*)', clean_context, re.IGNORECASE)
            if about_match and any(p in txt_low for p in ["about the hospital", "about us", f"about {org_name.lower() if org_name else ''}"]) and not any(p in txt_low for p in ["visiting", "appointment", "doctor", "service"]):
                about_text = re.sub(r'\s+', ' ', about_match.group(1).strip())
                return guardrail_service.validate_and_sanitize_response(about_text)

            # 9. General sentence scoring on context
            sentences = [s.strip() for s in re.split(r'[.!?\n]', clean_context) if len(s.strip()) > 10]
            best_sentence = None
            best_score = 0

            for s in sentences:
                clean_s = re.sub(r'^(According to.*?:|Source:?|INTERNAL:?)\s*', '', s, flags=re.IGNORECASE).strip()
                if not clean_s or any(lbl in clean_s.upper() for lbl in ["DEVELOPMENT", "TEST DATA", "KNOWLEDGE BASE"]):
                    continue
                # Skip raw question headers
                if re.match(r'^(?:Q|Question):', clean_s, re.IGNORECASE):
                    continue

                s_words = set(re.findall(r'\b[a-zA-Z0-9]{3,}\b', clean_s.lower()))
                match_cnt = sum(1 for qw in query_words if qw in s_words)
                specific_match = (not specific_query_words) or any(sqw in s_words for sqw in specific_query_words)

                if match_cnt > best_score and specific_match:
                    best_score = match_cnt
                    best_sentence = clean_s

            if best_sentence and best_score > 0:
                clean_best = re.sub(r'^(?:A|Answer):\s*', '', best_sentence, flags=re.IGNORECASE).strip()
                final_s = clean_best if clean_best.endswith(('.', '!', '?')) else f"{clean_best}."
                return guardrail_service.validate_and_sanitize_response(final_s)

            # Factual query about an unlisted entity
            if any(w in txt_low for w in ["department", "teacher", "doctor", "service", "specialist", "cost", "fee", "program", "about", "tell me"]):
                return guardrail_service.validate_and_sanitize_response(
                    f"I apologize, but that specific information is not available in our active records for {org_name or 'our organization'}. How else may I assist you?"
                )

            # General knowledge rule fallback when no LLM key is present
            if "speed of light" in txt_low:
                return guardrail_service.validate_and_sanitize_response(
                    "The speed of light in a vacuum is approximately 299,792,458 meters per second or about 300,000 kilometers per second."
                )

        # Natural conversational continuation for general conversation / remarks
        from src.services.rag_service import rag_service
        active_doc_id = rag_service._resolve_active_document_id(organization_id)
        org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id)
        txt_clean_query = text.lower().strip()
        is_factual_query = any(w in txt_clean_query for w in ["what", "when", "where", "who", "which", "how much", "how many", "cost", "price", "fee", "hours", "timing", "address", "doctor", "service", "stylist", "menu"])

        if "speed of light" in txt_clean_query:
            return guardrail_service.validate_and_sanitize_response(
                "The speed of light in a vacuum is approximately 299,792,458 meters per second or about 300,000 kilometers per second."
            )

        return guardrail_service.validate_and_sanitize_response(
            "I apologize, but that specific information is not available in our active records. How else may I assist you?"
        )

    def generate_response_stream(
        self,
        text: str,
        context: str = "",
        session_history: list = None,
        intent: str = "general_question",
        emotion: str = "neutral",
        conversation_language: str = "en",
        organization_id: Optional[int] = None,
        conversation_state: Optional[Dict[str, Any]] = None,
        outbound_script: Optional[str] = None
    ) -> Generator[str, None, None]:
        """
        Streaming response generator for real-time voice playback with minimal first-token latency.
        """
        from src.config.env import settings
        from src.services.rag_service import rag_service

        is_ongoing = bool(
            (session_history and len(session_history) > 1) or
            (conversation_state and conversation_state.get("turn_count", 0) > 1) or
            (conversation_state and conversation_state.get("conversation_started")) or
            (conversation_state and conversation_state.get("greeting_sent"))
        )

        safety_check = guardrail_service.check_user_input_safety(text)
        if not safety_check["is_safe"] and safety_check.get("refusal_needed"):
            yield safety_check["refusal_message"]
            return

        clean_context = ""
        if context:
            clean_context = re.sub(r'\[Source:.*?\]', '', context)
            clean_context = re.sub(r'\r\n|\r', '\n', clean_context)
            clean_context = re.sub(r'\n{3,}', '\n\n', clean_context).strip()[:5000]

        fast_resp = self._fast_rule_response(
            text, intent, organization_id=organization_id, is_ongoing=is_ongoing,
            booking_session=conversation_state, has_context=bool(clean_context)
        )
        if fast_resp:
            yield fast_resp
            return

        active_doc_id = rag_service._resolve_active_document_id(organization_id)
        is_rag_active = bool(active_doc_id)
        org_name = rag_service.get_organization_name(organization_id, document_id=active_doc_id)
        system_prompt = self._get_system_instruction(organization_name=org_name, is_ongoing=is_ongoing, is_rag_active=is_rag_active, emotion=emotion)

        if outbound_script:
            system_prompt = (
                f"=== CRITICAL OUTBOUND CALL INSTRUCTION ===\n"
                f"You are making an outbound call on behalf of {org_name}. "
                f"You MUST strictly follow this custom script/instruction for this specific call:\n"
                f"\"{outbound_script}\"\n"
                f"===========================================\n\n"
            ) + system_prompt

        formatted_history = ""
        if session_history and len(session_history) > 0:
            history_lines = []
            for h in session_history[-4:]:
                role_label = "Caller" if h.get("role") in ("user", "human") else "Receptionist"
                history_lines.append(f"{role_label}: {h.get('text', '')}")
            formatted_history = "\n".join(history_lines)

        conv_state_block = ""
        if conversation_state:
            collected = conversation_state.get("collected_fields") or {}
            missing = conversation_state.get("missing_fields") or []
            stage = conversation_state.get("current_stage") or "IDLE"
            c_intent = conversation_state.get("current_intent") or intent
            conv_state_block = (
                f"=== [CONVERSATION STATE] ===\n"
                f"Intent: {c_intent}\n"
                f"Stage: {stage}\n"
                f"Collected Information: {collected}\n"
                f"Missing Information: {missing}\n\n"
            )

        context_block = (
            f"=== [RAG CONTEXT (ACTIVE KNOWLEDGE BASE)] ===\n{clean_context}\n"
            if clean_context else
            "=== [RAG CONTEXT (ACTIVE KNOWLEDGE BASE)] ===\nNo specific knowledge base context found for this query.\n"
        )

        user_content = (
            f"{context_block}\n"
            f"{conv_state_block}"
            f"=== [RECENT CONVERSATION] ===\n{formatted_history or 'No previous turns.'}\n\n"
            f"=== [CURRENT USER QUERY] ===\n{text}"
        )

        provider = self._get_provider()
        gemini_model_name = getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash")

        try:
            # 1. xAI / Grok Streaming (primary when LLM_PROVIDER=xai)
            if provider == "xai" and getattr(self, "xai_client", None):
                xai_model = getattr(settings, "XAI_MODEL", "grok-beta") or "grok-beta"
                messages = [{"role": "system", "content": system_prompt}]
                if session_history:
                    for h in session_history[-4:]:
                        role = "assistant" if h.get("role") in ("model", "assistant") else "user"
                        messages.append({"role": role, "content": h.get("text", "")})
                messages.append({"role": "user", "content": user_content})
                try:
                    response_stream = self.xai_client.chat.completions.create(
                        model=xai_model,
                        messages=messages,
                        temperature=0.2,
                        max_tokens=200,
                        stream=True
                    )
                    token_count = 0
                    for chunk in response_stream:
                        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                            token_count += 1
                            yield chunk.choices[0].delta.content
                    if token_count > 0:
                        return
                except Exception as e:
                    logger.warning(f"[XAI STREAM WARNING] xAI stream failed: {e}")

            # 2. Gemini Streaming with Multi-Model Failover
            if self.gemini_client and (provider in ("gemini",) or not self.mistral_client):
                gemini_models = self._get_verified_gemini_models()

                contents = []
                if session_history and intent not in ("greeting", "general_conversation"):
                    for h in session_history[-4:]:
                        role = "model" if h.get("role") in ("model", "assistant") else "user"
                        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h.get("text", ""))]))

                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_content)]))

                for target_model in gemini_models:
                    try:
                        config = self._build_gemini_config(target_model, system_prompt, max_tokens=300)
                        response_stream = self.gemini_client.models.generate_content_stream(
                            model=target_model,
                            contents=contents,
                            config=config
                        )
                        token_count = 0
                        for response_chunk in response_stream:
                            if response_chunk.text:
                                token_count += 1
                                yield response_chunk.text
                        if token_count > 0:
                            return
                    except Exception as e:
                        self._mark_gemini_model_exhausted(target_model, str(e))
                        logger.warning(f"[GEMINI STREAM WARNING] Generation failed with model '{target_model}': {e}")

            # 3. Mistral Streaming Fallback
            if self.mistral_client:
                mistral_model = getattr(settings, "MISTRAL_MODEL", "mistral-tiny")
                messages = [{"role": "system", "content": system_prompt}]
                if session_history:
                    for h in session_history[-4:]:
                        role = "assistant" if h.get("role") in ("model", "assistant") else "user"
                        messages.append({"role": role, "content": h.get("text", "")})
                messages.append({"role": "user", "content": user_content})

                response_stream = self.mistral_client.chat.completions.create(
                    model=mistral_model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=200,
                    stream=True
                )
                token_count = 0
                for chunk in response_stream:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        token_count += 1
                        yield chunk.choices[0].delta.content
                if token_count > 0:
                    return

        except Exception as exc:
            logger.warning(f"[LLM STREAM WARNING] Stream error ({exc}). Falling back to non-stream generation.")

        # Fallback to non-stream generator (handles Ollama + RAG extraction)
        full_ans = self.generate_response(
            text, context, session_history, intent, emotion,
            conversation_language=conversation_language,
            organization_id=organization_id,
            conversation_state=conversation_state
        )
        yield full_ans

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        return stt_service.transcribe_audio_bytes(audio_bytes)

    def generate_tts(self, text: str) -> bytes:
        return tts_service.synthesize_speech(text)


ai_service = AIService()
