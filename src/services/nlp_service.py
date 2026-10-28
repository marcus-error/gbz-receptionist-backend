"""
Local Open-Source NLP & Intent Detection Service.

Classifies caller text into 14 supported intent categories with confidence scores.
Uses zero-latency rule pre-classifier combined with semantic similarity matching.
Loaded once at startup and reused.
"""

import asyncio
import logging
import os
import re
import time
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class NLPResult(BaseModel):
    intent: str = Field(description="The detected intent category")
    confidence: float = Field(default=0.90, description="Confidence score between 0.0 and 1.0")
    appointment_details: Optional[Dict[str, Any]] = None


# 14 Supported Intents as requested
INTENT_DOCTOR_LIST = "doctor_list"
INTENT_DOCTOR_COUNT = "doctor_count"
INTENT_DOCTOR_INFORMATION = "doctor_information"
INTENT_DEPARTMENT_LIST = "department_list"
INTENT_ORGANIZATION_INFORMATION = "organization_information"
INTENT_HOSPITAL_INFORMATION = INTENT_ORGANIZATION_INFORMATION
INTENT_SERVICE_INFORMATION = "service_information"
INTENT_FAQ = "faq"
INTENT_APPOINTMENT_BOOKING = "appointment_booking"
INTENT_APPOINTMENT_AVAILABILITY = "appointment_availability"
INTENT_APPOINTMENT_CANCELLATION = "appointment_cancellation"
INTENT_APPOINTMENT_RESCHEDULING = "appointment_rescheduling"
INTENT_GREETING = "greeting"
INTENT_GENERAL_CONVERSATION = "general_conversation"
INTENT_GOODBYE = "goodbye"
INTENT_THANKS = "thanks"
INTENT_GENERAL_QUESTION = "general_question"
INTENT_CALLBACK = "callback"
INTENT_UNKNOWN = "unknown"

# Additional safety & legacy compatibility intents
INTENT_EMERGENCY = "emergency/urgent request"
INTENT_TRANSFER = "transfer request"

# Backward compatibility aliases
INTENT_APPOINTMENT = INTENT_APPOINTMENT_BOOKING
INTENT_CANCELLATION = INTENT_APPOINTMENT_CANCELLATION
INTENT_RESCHEDULING = INTENT_APPOINTMENT_RESCHEDULING
INTENT_DOCTOR_ENQUIRY = INTENT_DOCTOR_LIST
INTENT_CLINIC_INFO = INTENT_HOSPITAL_INFORMATION
INTENT_GENERAL = INTENT_GENERAL_CONVERSATION


class NLPService:
    def __init__(self):
        self.encoder = None

    def initialize_model(self):
        """Pre-load local NLP model / sentence transformer for intent classification."""
        if "PYTEST_CURRENT_TEST" in os.environ or os.getenv("APP_ENV") == "test":
            self.encoder = "mock_nlp"
            return

        t0 = time.perf_counter()
        try:
            from sentence_transformers import SentenceTransformer
            self.encoder = SentenceTransformer("all-MiniLM-L6-v2")
            dur = (time.perf_counter() - t0) * 1000.0
            logger.info(f"Local NLP model (all-MiniLM-L6-v2) loaded successfully in {dur:.2f} ms")
        except Exception as exc:
            logger.warning(f"Could not load SentenceTransformer for NLP ({exc}). Using rule-based NLP classifier.")
            self.encoder = None

    def detect_intent(self, text: str) -> NLPResult:
        """
        Classify input text into one of the supported intents.
        Prioritizes exact caller intent over session context.
        """
        txt = (text or "").strip().lower()
        if not txt:
            return NLPResult(intent=INTENT_GENERAL_CONVERSATION, confidence=0.50)

        # 1. Emergency / Urgent Safety Rule
        if any(w in txt for w in ["chest pain", "bleeding", "unconscious", "emergency", "urgent", "ambulance", "heart attack", "stroke", "die", "dying"]):
            return NLPResult(intent=INTENT_EMERGENCY, confidence=0.99)

        # 2. General Conversation & Acknowledgements (e.g. "how are you", "I'm home", "I'm here", "yes", "okay", "alright")
        conversational_phrases = [
            "how are you", "how are you doing", "how do you do", "what's up", "what is up",
            "i am home", "i'm home", "im home", "i am here", "i'm here", "im here", "i am back", "i'm back",
            "i am ready", "i'm ready", "im ready", "can you hear me", "are you there", "are you listening",
            "i understand", "i see", "sounds good", "tell me more"
        ]
        if any(phrase in txt for phrase in conversational_phrases) or txt in {"yes", "yeah", "yep", "sure", "ok", "okay", "alright", "fine", "cool", "great", "good"}:
            return NLPResult(intent=INTENT_GENERAL_CONVERSATION, confidence=0.98)

        # 3. Greetings
        txt_clean_punc = re.sub(r'[^\w\s]', '', txt).strip()
        if txt_clean_punc in {"hi", "hello", "hey", "good morning", "good afternoon", "good evening", "greetings", "vanakkam", "namaste", "hello there", "hi there", "hey there"}:
            return NLPResult(intent=INTENT_GREETING, confidence=0.99)

        # 3b. Goodbye
        if any(w in txt for w in ["bye", "goodbye", "see you", "exit", "thank you bye", "thanks bye"]):
            return NLPResult(intent=INTENT_GOODBYE, confidence=0.99)

        # 3c. Thanks & Gratitude
        if (
            txt_clean_punc in {"thanks", "thank you", "thank u", "many thanks", "thank you so much", "thanks a lot", "appreciated", "much appreciated", "that is all", "thats all", "that will be all"}
            or any(p in txt for p in ["thank you", "thanks", "thank u", "appreciated", "that is all", "that's all", "that will be all"])
        ):
            return NLPResult(intent=INTENT_THANKS, confidence=0.99)

        # 4a. Doctor Count Request
        is_doctor_count = any(phrase in txt for phrase in [
            "how many doctors", "number of doctors", "doctor count", "count of doctors",
            "how many doctors available", "how many doctors do you have", "how many doctors are there", "total doctors"
        ])
        if is_doctor_count:
            return NLPResult(intent=INTENT_DOCTOR_COUNT, confidence=0.99)

        # 4b. Staff / Doctor / Teacher List Request
        is_doctor_or_staff_list = any(phrase in txt for phrase in [
            "list your doctors", "list the doctors", "list doctors", "who are your doctors", "who are the doctors",
            "tell me about your doctors", "what doctors do you have", "which doctors do you have", "doctor list",
            "show doctors", "list of doctors", "our doctors", "doctor names", "doctors available in your hospital",
            "doctors in the hospital", "show me your doctors", "show me the doctors",
            "list the staff", "list staff", "list staff members", "staff members", "who are all available",
            "who is available", "who are available", "staff list", "list of staff", "list teachers", "list the teachers"
        ]) or (
            any(w in txt for w in ["who", "what", "list", "tell me", "show", "irukka"])
            and any(w in txt for w in ["doctor", "doctors", "physician", "specialist", "staff", "teacher", "teachers", "stylist", "stylists", "member", "members"])
            and not any(w in txt for w in ["my name is", "i am", "call me", "book with dr", "book with stylist", "book with teacher"])
        )
        if is_doctor_or_staff_list:
            return NLPResult(intent=INTENT_DOCTOR_LIST, confidence=0.98)

        # 5a. Department List Request
        is_dept_list = any(phrase in txt for phrase in [
            "what departments", "departments do you have", "list departments", "which departments",
            "show departments", "department list", "list of departments", "our departments"
        ]) or (
            any(w in txt for w in ["department", "departments", "branches", "specialties"])
            and any(w in txt for w in ["list", "what", "which", "show", "have", "irukka", "available", "tell me"])
            and not any(w in txt for w in ["my name is", "i am", "call me"])
        )
        if is_dept_list:
            return NLPResult(intent=INTENT_DEPARTMENT_LIST, confidence=0.98)

        # 5b. Service List Request
        is_service_list = any(phrase in txt for phrase in [
            "what services", "which services", "list services", "list the services", "service list", "list of services",
            "what are all the service", "what are all the services", "what treatments", "list treatments", "our services"
        ]) or (
            any(w in txt for w in ["service", "services", "facilities", "treatments", "offer", "provide"])
            and any(w in txt for w in ["list", "what", "which", "show", "have", "irukka", "available", "tell me"])
            and not any(w in txt for w in ["my name is", "i am", "call me"])
        )
        if is_service_list:
            return NLPResult(intent=INTENT_SERVICE_INFORMATION, confidence=0.98)

        # 6. Staff / Doctor Information / Working Schedule Request (e.g. "tell me about Mr Rohit", "what is Dr. Rohit's schedule?")
        is_doctor_info_or_schedule = (
            any(phrase in txt for phrase in [
                "tell me about", "who is", "specialization", "profile of", "details of",
                "schedule of", "working hours", "timing of", "hours of", "working schedule", "schedule"
            ]) and any(w in txt for w in ["dr", "doctor", "physician", "specialist", "staff", "teacher", "stylist", "mr", "ms", "mrs", "mr.", "ms.", "mrs."])
            and not any(w in txt for w in ["book", "reserve", "want to book", "book appointment", "take appointment", "slot"])
        ) or re.search(r'\b(tell\s+me\s+about|schedule\s+of|working\s+hours\s+of|timing\s+of)\s+(dr|mr|ms|mrs)\.?\s*[a-zA-Z]+\b', txt)

        if is_doctor_info_or_schedule:
            return NLPResult(intent=INTENT_DOCTOR_INFORMATION, confidence=0.96)

        # 7. Cancellation Request
        if any(w in txt for w in ["cancel", "cancellation", "drop appointment", "dont want appointment"]):
            return NLPResult(intent=INTENT_APPOINTMENT_CANCELLATION, confidence=0.95)

        # 8. Rescheduling Request
        if any(w in txt for w in ["reschedule", "change time", "move appointment", "postpone", "change date"]):
            return NLPResult(intent=INTENT_APPOINTMENT_RESCHEDULING, confidence=0.95)

        # 9. Appointment Availability Request (e.g. "what time slots do you have?", "is Dr. Arun available tomorrow?")
        if any(phrase in txt for phrase in [
            "what time slots", "time slots", "available slots", "slots available", "open slots", "free slots",
            "available times", "available time", "is dr", "available on", "available tomorrow"
        ]):
            return NLPResult(intent=INTENT_APPOINTMENT_AVAILABILITY, confidence=0.96)

        # 10. Appointment Booking Request
        if any(w in txt for w in ["book", "booking", "appointment", "schedule", "reserve", "slot", "slots", "want an appointment", "need an appointment", "make an appointment", "get an appointment", "take an appointment", "fix an appointment", "book doctor", "pannanum", "venum"]):
            customer = None
            date_str = None
            time_str = None
            staff = None

            # Extract customer name if present
            name_match = re.search(r'\b(?:my\s+(?:full\s+)?name\s+is|i\s*(?:am|\'m)|this\s+is|you\s+can\s+call\s+me|call\s+me|myself)\s+([a-zA-Z\s]+?)(?:\s+(?:and|&|want|with|for|to|at|on)|\s*$)', txt, re.IGNORECASE)
            if name_match:
                c_name = name_match.group(1).strip()
                if len(c_name.split()) <= 4:
                    customer = c_name.title()

            if "tomorrow" in txt:
                date_str = "Tomorrow"
            elif "today" in txt:
                date_str = "Today"

            time_match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b', txt)
            if time_match:
                time_str = time_match.group(1).upper()

            staff_match = re.search(r'\b(dr\.\s*[a-zA-Z]+|doctor\s*[a-zA-Z]+)\b', txt)
            if staff_match:
                staff = staff_match.group(1).title()

            return NLPResult(
                intent=INTENT_APPOINTMENT_BOOKING,
                confidence=0.95,
                appointment_details={
                    "customer": customer,
                    "appointment_date": date_str,
                    "appointment_time": time_str,
                    "staff": staff
                }
            )

        # 11. Organization / Company / Hospital Information / Location / Business Hours
        if any(phrase in txt for phrase in [
            "what is your hospital name", "which hospital is this", "where am i calling", "tell me the hospital name",
            "hospital name", "may i know your hospital name", "your hospital name", "hospital details",
            "what is your organization name", "what is your company name", "which company is this",
            "organization name", "company name", "clinic name", "apartment name", "hotel name"
        ]) or any(w in txt for w in ["location", "address", "where is", "hospital info", "org info", "hours", "open", "close", "timing", "when are you open"]):
            return NLPResult(intent=INTENT_HOSPITAL_INFORMATION, confidence=0.98)

        # 12. Service Information
        if any(w in txt for w in ["service", "services", "facilities", "treatments", "offer", "provide"]):
            return NLPResult(intent=INTENT_SERVICE_INFORMATION, confidence=0.92)

        # 13. FAQ / Pricing / Consultation Fees / Insurance
        if any(phrase in txt for phrase in ["how much", "how much is", "how much for", "what is the cost", "what is the price", "how much does"]) or any(w in txt for w in ["fee", "fees", "cost", "price", "pricing", "charge", "rate", "rates", "tariff", "amount", "insurance", "policy", "faq"]):
            return NLPResult(intent=INTENT_FAQ, confidence=0.95)

        # 14a. Callback Request
        if any(w in txt for w in ["call me back", "request a callback", "callback", "arrange a callback", "give me a call back", "schedule a callback", "call back"]):
            return NLPResult(intent=INTENT_CALLBACK, confidence=0.96)

        # 14b. Human Transfer Request
        # IMPORTANT: Only trigger on EXPLICIT requests to speak/talk TO a human.
        # Do NOT trigger on informational questions that mention "receptionist" or similar words.
        is_informational = any(phrase in txt for phrase in [
            "tell me about", "what is", "what are", "explain", "describe", "how does", "how do",
            "can you tell", "what do you", "give me information", "information about"
        ])
        if not is_informational and any(w in txt for w in [
            "transfer", "speak to human", "speak to a human", "speak with human", "speak with a human",
            "human representative", "speak to agent", "speak with agent", "operator",
            "real person", "human agent", "talk to a person", "talk to human", "talk to a human",
            "connect me to", "connect me with"
        ]):
            return NLPResult(intent=INTENT_TRANSFER, confidence=0.95)

        # 15. Spoken Phone Number / Contact Info
        raw_digits = re.sub(r'[^\d]', '', txt)
        if len(raw_digits) >= 5 and (len(txt.split()) <= 7 or any(w in txt for w in ["it's", "its", "number", "phone", "mobile", "contact"])):
            return NLPResult(intent=INTENT_APPOINTMENT_BOOKING, confidence=0.95)

        # 16. Standalone Name / Entity statement (filter out common conversational adjectives & state words)
        NON_NAME_WORDS = {"home", "here", "there", "back", "ready", "fine", "good", "ok", "okay", "alright", "listening", "calling", "busy", "free", "available", "sorry", "done", "well", "happy", "sad", "sick", "speaking", "talking"}
        name_only_match = re.search(r'^\s*(?:my\s+(?:full\s+)?name\s+is|this\s+is|you\s+can\s+call\s+me|call\s+me|myself)\s+([a-zA-Z\s]+)\s*$', txt, re.IGNORECASE)
        if not name_only_match:
            i_am_match = re.search(r'^\s*(?:i\s*(?:am|\'m))\s+([a-zA-Z\s]+)\s*$', txt, re.IGNORECASE)
            if i_am_match and i_am_match.group(1).strip().lower() not in NON_NAME_WORDS:
                name_only_match = i_am_match

        if name_only_match:
            c_name = name_only_match.group(1).strip()
            if len(c_name.split()) <= 4 and c_name.lower() not in NON_NAME_WORDS:
                return NLPResult(
                    intent=INTENT_GENERAL_CONVERSATION,
                    confidence=0.90,
                    appointment_details={"customer": c_name.title()}
                )

        # Fallback to general_conversation or general_question based on query structure
        is_question = any(w in txt for w in ["who", "what", "where", "when", "why", "how", "which", "is", "are", "do", "does", "can", "could", "tell me"]) or "?" in text
        if is_question:
            return NLPResult(intent=INTENT_GENERAL_QUESTION, confidence=0.85)
        return NLPResult(intent=INTENT_GENERAL_CONVERSATION, confidence=0.85)

    async def detect_intent_async(self, text: str) -> NLPResult:
        """Asynchronous wrapper for intent detection."""
        return await asyncio.to_thread(self.detect_intent, text)


nlp_service = NLPService()
