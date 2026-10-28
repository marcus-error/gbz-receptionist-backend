"""
Multilingual Language Detection & Conversation Language Lock Service.

Supports Tamil (script & Tanglish), Hindi (script & Hinglish), Malayalam,
Telugu, Kannada, and English.
Manages conversation language locking, explicit language switches, and
entity-name immunity.

Priority pipeline:
  is_language_switch_request()  ← fast predicate (called first by voice_socket)
  check_explicit_language_switch()   ← returns (target_lang, True/False)
  determine_conversation_language()  ← full stateful resolver
"""

import logging
import re
from typing import Tuple, Optional

logger = logging.getLogger(__name__)


LANG_TAMIL = "ta"
LANG_TANGLISH = "tanglish"
LANG_HINDI = "hi"
LANG_ENGLISH = "en"
LANG_MALAYALAM = "ml"
LANG_TELUGU = "te"
LANG_KANNADA = "kn"


# ---------------------------------------------------------------------------
# Canonical language name → code mapping
# ---------------------------------------------------------------------------
_LANG_NAME_TO_CODE = {
    "tamil": LANG_TAMIL,
    "tamizh": LANG_TAMIL,
    "தமிழ்": LANG_TAMIL,
    "hindi": LANG_HINDI,
    "हिंदी": LANG_HINDI,
    "english": LANG_ENGLISH,
    "malayalam": LANG_MALAYALAM,
    "മലയാളം": LANG_MALAYALAM,
    "telugu": LANG_TELUGU,
    "తెలుగు": LANG_TELUGU,
    "kannada": LANG_KANNADA,
    "ಕನ್ನಡ": LANG_KANNADA,
}

# Phrases that indicate a language-switch request (caller → AI direction).
# Matched case-insensitively against the full lowercased input.
_LANG_SWITCH_TRIGGER_PHRASES = [
    # Generic verbs + "in <lang>" / "<lang> please"
    r'\b(?:please\s+)?(?:talk|speak|chat|converse|respond|reply|answer|communicate)\s+(?:to\s+me\s+)?in\s+([a-zA-Z\u0b80-\u0bff\u0900-\u097f\u0d00-\u0d7f\u0c00-\u0c7f\u0c80-\u0cff]+)',
    r'\bcan\s+you\s+(?:please\s+)?(?:talk|speak|chat|converse|respond)\s+(?:to\s+me\s+)?in\s+([a-zA-Z\u0b80-\u0bff\u0900-\u097f\u0d00-\u0d7f\u0c00-\u0c7f\u0c80-\u0cff]+)',
    r'\bcould\s+you\s+(?:please\s+)?(?:talk|speak|chat)\s+(?:to\s+me\s+)?in\s+([a-zA-Z\u0b80-\u0bff\u0900-\u097f\u0d00-\u0d7f\u0c00-\u0c7f\u0c80-\u0cff]+)',
    r'\bplease\s+(?:talk|speak|chat)\s+(?:to\s+me\s+)?in\s+([a-zA-Z\u0b80-\u0bff\u0900-\u097f\u0d00-\u0d7f\u0c00-\u0c7f\u0c80-\u0cff]+)',
    # "switch to <lang>"
    r'\bswitch\s+to\s+([a-zA-Z\u0b80-\u0bff\u0900-\u097f\u0d00-\u0d7f\u0c00-\u0c7f\u0c80-\u0cff]+)',
    r'\bchange\s+(?:the\s+)?language\s+to\s+([a-zA-Z\u0b80-\u0bff\u0900-\u097f\u0d00-\u0d7f\u0c00-\u0c7f\u0c80-\u0cff]+)',
    # "talk <lang>" / "speak <lang>"
    r'\b(?:talk|speak)\s+([a-zA-Z]+)(?:\s+please)?\b',
    # "<lang> please" / "<lang> la pesu/pesunga"
    r'\b([a-zA-Z]+)\s+please\b',
    r'\b([a-zA-Z]+)\s+la\s+pesu(?:nga)?\b',
    r'\b([a-zA-Z]+)\s+mein\s+baat\s+karo\b',
    r'\b([a-zA-Z]+)\s+lo\s+matladandi\b',
    # Native-script requests (Tamil)
    r'(தமிழில்\s+பேசுங்கள்|தமிழில்\s+பேசு|தமிழில்\s+பேச\s+முடியுமா)',
    # Hindi
    r'(हिंदी\s+में\s+बात)',
]


class LanguageDetectionService:
    """
    Language Detection & Lock Manager.
    Determines input language and enforces language persistence.
    """

    def __init__(self):
        # Tanglish / Mixed Tamil-English keyword indicators
        self.tanglish_keywords = {
            "tamil", "tamizh", "pannanum", "paannatum", "venum", "venom", "iruka", "irukku", "irukkangala",
            "kitta", "vanakkam", "naalai", "indru", "inru", "ennodaya", "peru",
            "nallairuken", "seiyangga", "solloonga", "solunga", "unga", "enaku", "enakku",
            "pesunga", "mama", "nalladhu", "paapom", "paru", "pesalam", "mudiyuma",
            "varuma", "varum", "kudunga", "eduthu", "teriyum", "theriyum", "sapadunga"
        }

        # Hinglish / Hindi keyword indicators
        self.hinglish_keywords = {
            "hindi", "hinglish", "chahiye", "kaise", "kya", "hai", "hain", "karo", "karna", "aap", "baat",
            "batao", "dhanyawad", "shukriya", "boliye", "kaha", "kahan", "kaun", "mera",
            "namaste", "samajh"
        }

    # ------------------------------------------------------------------
    # Fast predicate — call this FIRST before any NLP/RAG routing
    # ------------------------------------------------------------------
    def is_language_switch_request(self, text: str) -> bool:
        """
        Returns True immediately if the text is a language-switch instruction
        (e.g. "Can you please talk in Tamil?", "Switch to Hindi").

        This MUST be called before RAG retrieval, entity extraction, and
        booking-state routing to prevent language phrases from being
        misrouted into directory/entity lookups.
        """
        _, is_switch = self.check_explicit_language_switch(text)
        return is_switch

    # ------------------------------------------------------------------
    # Core language detection
    # ------------------------------------------------------------------
    def detect_language(self, text: str) -> str:
        """
        Detect language of user text input.
        Returns: 'ta', 'hi', 'ml', 'te', 'kn', or 'en'.
        """
        txt = (text or "").strip()
        if not txt:
            return LANG_ENGLISH

        txt_lower = txt.lower()

        # 1. Tamil Script (Unicode \u0b80-\u0bff)
        if re.search(r'[\u0b80-\u0bff]', txt):
            return LANG_TAMIL

        # 2. Hindi / Devanagari (\u0900-\u097f)
        if re.search(r'[\u0900-\u097f]', txt):
            return LANG_HINDI

        # 3. Malayalam (\u0d00-\u0d7f)
        if re.search(r'[\u0d00-\u0d7f]', txt):
            return LANG_MALAYALAM

        # 4. Telugu (\u0c00-\u0c7f)
        if re.search(r'[\u0c00-\u0c7f]', txt):
            return LANG_TELUGU

        # 5. Kannada (\u0c80-\u0cff)
        if re.search(r'[\u0c80-\u0cff]', txt):
            return LANG_KANNADA

        # 6. Tanglish / Tamil in Latin
        words = set(re.findall(r'\b[a-z]+\b', txt_lower))
        if words.intersection(self.tanglish_keywords) or "tamil" in txt_lower or "tamizh" in txt_lower:
            return LANG_TAMIL

        # 7. Hinglish / Hindi in Latin
        if words.intersection(self.hinglish_keywords) or "hindi" in txt_lower:
            return LANG_HINDI

        return LANG_ENGLISH

    def check_explicit_language_switch(self, text: str) -> Tuple[Optional[str], bool]:
        """
        Check if caller explicitly requests a language switch.
        Returns (target_language_code, is_explicit_switch_requested).

        Covers all natural phrasings including:
          - "Can you please talk in Tamil?"
          - "Please speak Tamil"
          - "Switch to Hindi"
          - "Tamil la pesunga"
          - "தமிழில் பேசுங்கள்"
          - "Hindi mein baat karo"
          - "Please speak in English"
        """
        txt = (text or "").strip()
        if not txt:
            return None, False

        txt_lower = txt.lower()

        # --- Native script fast checks ---
        if re.search(r'தமிழில்\s+பேசு|தமிழ்\s+பேசு|தமிழில்\s+பேச', txt):
            return LANG_TAMIL, True
        if re.search(r'हिंदी\s+में\s+बात|हिंदी\s+बोलो', txt):
            return LANG_HINDI, True

        # --- Pattern matching ---
        for pattern in _LANG_SWITCH_TRIGGER_PHRASES:
            m = re.search(pattern, txt_lower, re.IGNORECASE)
            if m:
                # Extract the captured language name group (group 1 if present)
                lang_word = ""
                try:
                    lang_word = m.group(1).strip().lower()
                except IndexError:
                    lang_word = ""

                # Map the extracted word to a language code
                lang_code = _LANG_NAME_TO_CODE.get(lang_word)
                if lang_code:
                    logger.info(
                        f"[LANGUAGE SWITCH DETECTED] Pattern matched '{pattern}' → "
                        f"lang_word='{lang_word}' → code='{lang_code}'"
                    )
                    return lang_code, True

        return None, False

    def determine_conversation_language(self, text: str, current_lang: Optional[str] = None) -> str:
        """
        Stateful Language Lock Resolver.
        Maintains current conversation language unless an explicit switch or
        clear non-entity turn is given.
        """
        # Check explicit switch request
        explicit_target, is_explicit = self.check_explicit_language_switch(text)
        if is_explicit and explicit_target:
            logger.info(f"[LANGUAGE SWITCH] Caller explicitly requested language switch to '{explicit_target}'")
            return explicit_target

        detected = self.detect_language(text)

        # If no prior locked language, set initial detected language
        if not current_lang:
            return detected

        # If current locked language is Tamil or Tanglish:
        # Do NOT switch to English if detected is English but input contains entities or single words
        if current_lang in (LANG_TAMIL, LANG_TANGLISH):
            if detected == LANG_ENGLISH:
                # Check if turn is purely entity/short response (e.g. "Priya Sharma", "haircut", "10:30 AM")
                words = re.findall(r'\b[a-zA-Z0-9]+\b', text)
                if len(words) <= 4:
                    # Retain locked Tamil language for short entity answers
                    return current_lang
                # Retain locked language unless explicit switch
                return current_lang
            return detected

        # If current locked language is Hindi or English, update if detected language changed significantly
        if detected != current_lang and detected in (LANG_TAMIL, LANG_HINDI, LANG_MALAYALAM, LANG_TELUGU, LANG_KANNADA):
            return detected

        return current_lang or detected

    def get_language_switch_confirmation(
        self,
        target_lang: str,
        booking_session=None
    ) -> str:
        """
        Returns a natural, bilingual language-switch acknowledgment.

        If the caller is mid-appointment, appends the current pending question
        in the new language so the conversation continues seamlessly.

        Never resets conversation or repeats already-collected information.
        """
        # Base acknowledgment per target language
        _ACK = {
            LANG_TAMIL: "நிச்சயமாக! இனிமேல் நான் தமிழில் பேசுகிறேன்.",
            LANG_HINDI: "Zaroor! Ab main Hindi mein baat karunga.",
            LANG_ENGLISH: "Sure! I'll continue in English.",
            LANG_MALAYALAM: "തീർച്ചയായും! ഇനി ഞാൻ മലയാളത്തിൽ സംസാരിക്കും.",
            LANG_TELUGU: "తప్పకుండా! ఇకపై నేను తెలుగులో మాట్లాడుతాను.",
            LANG_KANNADA: "ಖಂಡಿತ! ಇನ್ನು ನಾನು ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡುತ್ತೇನೆ.",
        }
        ack = _ACK.get(target_lang, "Sure! I'll switch to the requested language now.")

        # If no active booking session or session is idle, just return ack + open prompt
        if not booking_session:
            _OPEN = {
                LANG_TAMIL: " எப்படி உதவலாம்?",
                LANG_HINDI: " Aapki kaise sahayata kar sakta hoon?",
                LANG_ENGLISH: " How may I assist you?",
                LANG_MALAYALAM: " എനിക്ക് നിങ്ങളെ എങ്ങനെ സഹായിക്കാൻ കഴിയും?",
                LANG_TELUGU: " మీకు ఎలా సహాయం చేయగలను?",
                LANG_KANNADA: " ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
            }
            return ack + _OPEN.get(target_lang, " How may I assist you?")

        from src.services.booking_state_service import BookingState
        state = getattr(booking_session, "state", BookingState.IDLE)
        doctor = getattr(booking_session, "doctor", None)
        date = getattr(booking_session, "date", None)

        # Determine what was being asked before the language switch and re-ask in new language
        if state == BookingState.WAITING_FOR_DOCTOR or (state != BookingState.IDLE and not doctor):
            _CONT = {
                LANG_TAMIL: " யாரை அல்லது எதற்கு appointment வேண்டும்?",
                LANG_HINDI: " Aap kisse milna ya kya book karna chahte hain?",
                LANG_ENGLISH: " Sure. Who or what would you like to book the appointment for?",
                LANG_MALAYALAM: " ആരെ കാണണം അല്ലെങ്കിൽ എന്ത് ബുക്ക് ചെയ്യണം?",
                LANG_TELUGU: " మీరు ఎవరిని కలవాలనుకుంటున్నారు లేదా ఏమి బుక్ చేయాలనుకుంటున్నారు?",
                LANG_KANNADA: " ಯಾರನ್ನು ಭೇಟಿ ಮಾಡಬೇಕು ಅಥವಾ ಏನನ್ನು ಬುಕ್ ಮಾಡಬೇಕು?",
            }
            return ack + _CONT.get(target_lang, " How may I assist you?")

        if state == BookingState.WAITING_FOR_DATE or (doctor and not date):
            _CONT = {
                LANG_TAMIL: f" {doctor}-க்கு எந்த நாளுக்கு appointment வேண்டும்?",
                LANG_HINDI: f" {doctor} ke liye kaunsi date prefer karenge?",
                LANG_ENGLISH: f" Which date would you prefer for your {doctor} appointment?",
                LANG_MALAYALAM: f" {doctor}-ന്‌ ഏത് തീയതി appointment വേണം?",
                LANG_TELUGU: f" {doctor} కోసం ఏ తేదీ appointment కావాలి?",
                LANG_KANNADA: f" {doctor} appointment ಯಾವ ದಿನಕ್ಕೆ ಬೇಕು?",
            }
            return ack + _CONT.get(target_lang, f" Which date would you prefer for {doctor}?")

        if state == BookingState.WAITING_FOR_TIME:
            _CONT = {
                LANG_TAMIL: f" {date}-ல் எந்த நேரம் suitable-ஆ இருக்கும்?",
                LANG_HINDI: f" {date} ko kaunsa samay prefer karenge?",
                LANG_ENGLISH: f" What time would you prefer on {date}?",
                LANG_MALAYALAM: f" {date}-ൽ ഏത് സമയം ആണ് suitable?",
                LANG_TELUGU: f" {date}న ఏ సమయం suitable?",
                LANG_KANNADA: f" {date}ನಂದು ಯಾವ ಸಮಯ suitable?",
            }
            return ack + _CONT.get(target_lang, f" What time would you prefer on {date}?")

        # Default: open prompt
        _OPEN = {
            LANG_TAMIL: " எப்படி உதவலாம்?",
            LANG_HINDI: " Aapki kaise sahayata kar sakta hoon?",
            LANG_ENGLISH: " How may I assist you?",
            LANG_MALAYALAM: " എനിക்ക് നിങ്ങളെ എങ്ങനെ സഹായിക്കാൻ കഴിയും?",
            LANG_TELUGU: " మీకు ఎలా సహాయం చేయగలను?",
            LANG_KANNADA: " ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
        }
        return ack + _OPEN.get(target_lang, " How may I assist you?")


language_service = LanguageDetectionService()
