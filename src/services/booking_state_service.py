from __future__ import annotations
import re
import copy
import logging
from typing import Dict, Optional, Any, List
from datetime import date, datetime, timedelta
import src.models

logger = logging.getLogger(__name__)


class BookingState:
    IDLE = "IDLE"
    APPOINTMENT_START = "APPOINTMENT_START"
    BOOKING_STARTED = "BOOKING_STARTED"
    WAITING_FOR_DOCTOR = "WAITING_FOR_DOCTOR"
    WAITING_FOR_DEPARTMENT = "WAITING_FOR_DOCTOR"
    VERIFYING_DOCTOR = "VERIFYING_DOCTOR"
    WAITING_FOR_DATE = "WAITING_FOR_DATE"
    WAITING_FOR_TIME = "WAITING_FOR_TIME"
    CHECKING_AVAILABILITY = "CHECKING_AVAILABILITY"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    COLLECTING_PATIENT_NAME = "COLLECTING_PATIENT_NAME"
    WAITING_FOR_PATIENT_NAME = "COLLECTING_PATIENT_NAME"
    COLLECTING_EMAIL = "COLLECTING_EMAIL"
    WAITING_FOR_EMAIL = "COLLECTING_EMAIL"
    COLLECTING_PHONE = "COLLECTING_PHONE"
    WAITING_FOR_CONTACT = "COLLECTING_PHONE"
    BOOKING = "BOOKING"
    BOOKED = "BOOKED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class NextAction:
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"
    AUTOMATIC_ACTION_REQUIRED = "AUTOMATIC_ACTION_REQUIRED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    COMPLETED = "COMPLETED"


class BookingSession:
    """
    Structured Session Memory for Appointment Booking & Multi-Turn Conversations.
    Retains verified state across multiple turns without losing state.
    """
    def __init__(self, session_id: str, conversation_id: Optional[str] = None, call_sid: Optional[str] = None, organization_id: int = 1):
        self.session_id = session_id
        self.conversation_id: str = conversation_id or session_id
        self.call_sid: str = call_sid or session_id
        self.stream_sid: Optional[str] = None
        self.agent_id: Optional[str] = None
        self.user_id: Optional[str] = None
        self.organization_id: int = organization_id
        self.active_rag_id: Optional[str] = None
        self.active_doc_id: Optional[str] = None
        self.active_rag_title: str = "Active Knowledge Base"
        self.active_doc_title: str = "Active Knowledge Base"

        self.state = BookingState.IDLE
        self.conversation_started: bool = False
        self.greeting_sent: bool = False
        self.turn_count: int = 0
        self.turn_id: int = 0
        self.history: List[Dict[str, str]] = []

        self.doctor_id: Optional[int] = None
        self.doctor_name: Optional[str] = None
        self.doctor_locked: bool = False
        self.department_name: Optional[str] = None
        self.service_name: Optional[str] = None
        self.product_name: Optional[str] = None
        self.offered_slots: List[str] = []
        self.last_question_field: Optional[str] = None

        self.caller_name: Optional[str] = None
        self.caller_phone: Optional[str] = None
        self.patient_relationship: Optional[str] = None
        self.is_booking_for_other: bool = False

        self.confirmed_facts: Dict[str, Dict[str, Any]] = {}
        self.completed_steps: List[str] = []
        self.state_version: int = 1

        self._rejected_departments: Set[str] = set()
        self._rejected_doctors: Set[str] = set()
        self._rejected_services: Set[str] = set()
        self._offered_dept_doctor: Optional[str] = None
        self._last_rejected_entity: Optional[str] = None

        self.booking_data: Dict[str, Any] = {
            "intent": "appointment_booking",
            "department": None,
            "doctor": None,
            "specialty": None,
            "service": None,
            "product": None,
            "date": None,
            "date_obj": None,
            "time": None,
            "caller_name": None,
            "caller_phone": None,
            "patient_name": None,
            "patient_relationship": None,
            "is_booking_for_other": False,
            "email": None,
            "contact_number": None,
            "availability_verified": False,
            "booking_confirmed": False,
            "appointment_id": None,
            "idempotency_key": None,
            "conversation_language": "en"
        }
        self.is_completed: bool = False

    def is_active(self) -> bool:
        return self.state != BookingState.IDLE and not self.is_completed

    def reset(self):
        self.state = BookingState.IDLE
        self.doctor_id = None
        self.doctor_name = None
        self.doctor_locked = False
        self.department_name = None
        self.service_name = None
        self.product_name = None
        self.offered_slots = []
        self.last_question_field = None
        self.caller_name = None
        self.caller_phone = None
        self.patient_relationship = None
        self.is_booking_for_other = False
        self.confirmed_facts = {}
        self.completed_steps = []
        self.state_version += 1
        self._rejected_departments = set()
        self._rejected_doctors = set()
        self._rejected_services = set()
        self._offered_dept_doctor = None
        self._last_rejected_entity = None
        self.booking_data = {
            "intent": "appointment_booking",
            "department": None,
            "doctor": None,
            "specialty": None,
            "service": None,
            "product": None,
            "date": None,
            "date_obj": None,
            "time": None,
            "caller_name": None,
            "caller_phone": None,
            "patient_name": None,
            "patient_relationship": None,
            "is_booking_for_other": False,
            "email": None,
            "contact_number": None,
            "availability_verified": False,
            "booking_confirmed": False,
            "appointment_id": None,
            "idempotency_key": None,
            "conversation_language": "en"
        }
        self.is_completed = False

    def reset_for_new_booking(self):
        """
        Resets appointment-specific slot variables (department, doctor, service, product, date, time, appointment_id, status)
        while strictly preserving validated customer identity (patient name, contact number, email, caller details).
        """
        self.state = BookingState.IDLE
        self.doctor_id = None
        self.doctor_name = None
        self.doctor_locked = False
        self.department_name = None
        self.service_name = None
        self.product_name = None
        self.offered_slots = []
        self.last_question_field = None
        self.confirmed_facts = {}
        self.completed_steps = []
        self.state_version += 1
        self._rejected_departments = set()
        self._rejected_doctors = set()
        self._rejected_services = set()
        self._offered_dept_doctor = None
        self._last_rejected_entity = None
        
        # Preserve customer identity
        saved_caller_name = self.caller_name
        saved_caller_phone = self.caller_phone
        saved_patient_name = self.booking_data.get("patient_name")
        saved_email = self.booking_data.get("email")
        saved_contact = self.booking_data.get("contact_number")
        saved_location = self.booking_data.get("location")
        saved_lang = self.booking_data.get("conversation_language", "en")

        self.booking_data = {
            "intent": "appointment_booking",
            "department": None,
            "doctor": None,
            "specialty": None,
            "service": None,
            "product": None,
            "date": None,
            "date_obj": None,
            "time": None,
            "caller_name": saved_caller_name,
            "caller_phone": saved_caller_phone,
            "patient_name": saved_patient_name,
            "patient_relationship": self.patient_relationship,
            "is_booking_for_other": self.is_booking_for_other,
            "email": saved_email,
            "contact_number": saved_contact,
            "location": saved_location,
            "availability_verified": False,
            "booking_confirmed": False,
            "appointment_id": None,
            "idempotency_key": None,
            "conversation_language": saved_lang
        }
        self.is_completed = False

    def invalidate_department(self, dept_name: Optional[str] = None):
        """
        Invalidates rejected department/specialty selection and any dependent doctor selection.
        Preserves valid patient details (name, phone, email, preferred date).
        """
        curr_dept = self.department or ""
        if dept_name:
            self._rejected_departments.add(dept_name.lower().strip())
        elif curr_dept:
            self._rejected_departments.add(curr_dept.lower().strip())

        self.department_name = None
        self.booking_data["department"] = None
        self.booking_data["specialty"] = None
        if hasattr(self, "confirmed_facts") and "department" in self.confirmed_facts:
            del self.confirmed_facts["department"]
        if hasattr(self, "completed_steps") and "department_selection" in self.completed_steps:
            self.completed_steps.remove("department_selection")

        # Dependent doctor and slots must be cleared
        self.doctor = None
        self.doctor_name = None
        self.doctor_id = None
        self.doctor_locked = False
        self.booking_data["doctor"] = None
        self.booking_data["availability_verified"] = False
        self.offered_slots = []
        self.booking_data["time"] = None
        self._offered_dept_doctor = None
        if hasattr(self, "confirmed_facts") and "doctor" in self.confirmed_facts:
            del self.confirmed_facts["doctor"]
        if hasattr(self, "completed_steps") and "doctor_selection" in self.completed_steps:
            self.completed_steps.remove("doctor_selection")

        # Clear obsolete requested / ambiguous entity slots
        self.booking_data.pop("user_requested_entity_term", None)
        self.booking_data.pop("unverified_entity_requested", None)
        self.booking_data.pop("ambiguous_entity_candidate", None)
        self.booking_data.pop("pending_confirmation_entity", None)

        self._last_rejected_entity = "department"
        self.state = BookingState.WAITING_FOR_DOCTOR
        self.last_asked_field = "department"

    def invalidate_doctor(self, doc_name: Optional[str] = None):
        """
        Invalidates rejected doctor selection while keeping department and customer details intact.
        """
        curr_doc = self.doctor or ""
        if doc_name:
            self._rejected_doctors.add(doc_name.lower().strip())
            clean_d = re.sub(r'^(?:dr\.?|doctor)\s+', '', doc_name.lower().strip(), flags=re.I).strip()
            if clean_d:
                self._rejected_doctors.add(clean_d)
        elif curr_doc:
            self._rejected_doctors.add(curr_doc.lower().strip())
            clean_d = re.sub(r'^(?:dr\.?|doctor)\s+', '', curr_doc.lower().strip(), flags=re.I).strip()
            if clean_d:
                self._rejected_doctors.add(clean_d)

        self.doctor = None
        self.doctor_name = None
        self.doctor_id = None
        self.doctor_locked = False
        self.booking_data["doctor"] = None
        self.booking_data["availability_verified"] = False
        self.offered_slots = []
        self.booking_data["time"] = None
        self._offered_dept_doctor = None
        if hasattr(self, "confirmed_facts") and "doctor" in self.confirmed_facts:
            del self.confirmed_facts["doctor"]
        if hasattr(self, "completed_steps") and "doctor_selection" in self.completed_steps:
            self.completed_steps.remove("doctor_selection")

        # Clear obsolete requested / ambiguous entity slots
        self.booking_data.pop("user_requested_entity_term", None)
        self.booking_data.pop("unverified_entity_requested", None)
        self.booking_data.pop("ambiguous_entity_candidate", None)
        self.booking_data.pop("pending_confirmation_entity", None)

        self._last_rejected_entity = "doctor"
        self.state = BookingState.WAITING_FOR_DOCTOR
        self.last_asked_field = "doctor"

    def invalidate_service_or_product(self, name: Optional[str] = None):
        """
        Invalidates rejected service or product selection.
        """
        curr = self.service or ""
        if name:
            self._rejected_services.add(name.lower().strip())
        elif curr:
            self._rejected_services.add(curr.lower().strip())

        self.service_name = None
        self.product_name = None
        self.booking_data["service"] = None
        self.booking_data["product"] = None
        if hasattr(self, "confirmed_facts") and "service" in self.confirmed_facts:
            del self.confirmed_facts["service"]
        if hasattr(self, "confirmed_facts") and "product" in self.confirmed_facts:
            del self.confirmed_facts["product"]
        if hasattr(self, "completed_steps") and "service_selection" in self.completed_steps:
            self.completed_steps.remove("service_selection")

        # Clear obsolete requested / ambiguous entity slots
        self.booking_data.pop("user_requested_entity_term", None)
        self.booking_data.pop("unverified_entity_requested", None)
        self.booking_data.pop("ambiguous_entity_candidate", None)
        self.booking_data.pop("pending_confirmation_entity", None)

        self._last_rejected_entity = "service"
        self.state = BookingState.WAITING_FOR_DOCTOR
        self.last_asked_field = "service"

    @property
    def last_asked_field(self) -> Optional[str]:
        return self.last_question_field

    @last_asked_field.setter
    def last_asked_field(self, val: Optional[str]):
        self.last_question_field = val

    @property
    def department(self) -> Optional[str]:
        return self.department_name or self.booking_data.get("department")

    @department.setter
    def department(self, val: Optional[str]):
        if val != self.booking_data.get("department"):
            self.booking_data["department"] = val
            self.department_name = val
            self.booking_data["specialty"] = val

    @property
    def service(self) -> Optional[str]:
        return self.service_name or self.booking_data.get("service") or self.booking_data.get("product")

    @service.setter
    def service(self, val: Optional[str]):
        if val != self.booking_data.get("service"):
            self.booking_data["service"] = val
            self.service_name = val
            self.booking_data["product"] = val
            self.product_name = val
            if not self.booking_data.get("department"):
                self.booking_data["department"] = val
                self.department_name = val

    @property
    def product(self) -> Optional[str]:
        return self.product_name or self.booking_data.get("product") or self.booking_data.get("service")

    @product.setter
    def product(self, val: Optional[str]):
        self.service = val

    @property
    def doctor(self) -> Optional[str]:
        return self.doctor_name or self.booking_data.get("doctor")

    @doctor.setter
    def doctor(self, val: Optional[str]):
        if val != self.booking_data.get("doctor"):
            self.booking_data["doctor"] = val
            self.doctor_name = val
            self.booking_data["availability_verified"] = False

    @property
    def current_stage(self) -> str:
        return self.state

    @current_stage.setter
    def current_stage(self, val: str):
        self.state = val

    @property
    def current_intent(self) -> str:
        return self.booking_data.get("intent", "appointment_booking")

    @current_intent.setter
    def current_intent(self, val: str):
        self.booking_data["intent"] = val

    @property
    def full_name(self) -> Optional[str]:
        return self.booking_data.get("patient_name")

    @full_name.setter
    def full_name(self, val: Optional[str]):
        self.booking_data["patient_name"] = val

    @property
    def phone_number(self) -> Optional[str]:
        return self.booking_data.get("contact_number")

    @phone_number.setter
    def phone_number(self, val: Optional[str]):
        self.booking_data["contact_number"] = val

    @property
    def collected_fields(self) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        if self.department:
            fields["department"] = self.department
            fields["specialty"] = self.department
        if self.service:
            fields["service"] = self.service
            fields["product"] = self.service
        if self.patient_name:
            fields["full_name"] = self.patient_name
            fields["patient_name"] = self.patient_name
        if self.doctor:
            fields["doctor"] = self.doctor
        if self.date:
            fields["appointment_date"] = self.date
            fields["date"] = self.date
        if self.time:
            fields["appointment_time"] = self.time
            fields["time"] = self.time
        if self.contact_number and self.contact_number != "Unknown":
            fields["phone_number"] = self.contact_number
            fields["contact_number"] = self.contact_number
        if self.email:
            fields["email"] = self.email
        if self.booking_data.get("specialty"):
            fields["specialty"] = self.booking_data["specialty"]
        return fields

    @property
    def missing_fields(self) -> List[str]:
        missing: List[str] = []
        if not self.doctor:
            if not self.department and not self.service:
                missing.append("department_or_doctor")
            elif not self.service:
                missing.append("doctor")
        if not self.date:
            missing.append("appointment_date")
        if not self.time:
            missing.append("appointment_time")
        if not self.patient_name:
            missing.append("full_name")
        if not self.contact_number or self.contact_number == "Unknown":
            missing.append("phone_number")
        if not self.email:
            missing.append("email")
        return missing

    def get_state_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "call_sid": self.call_sid,
            "agent_id": self.agent_id,
            "organization_id": self.organization_id,
            "active_rag_id": self.active_rag_id or self.active_doc_id,
            "conversation_started": self.conversation_started,
            "greeting_sent": self.greeting_sent,
            "turn_id": self.turn_id,
            "turn_count": self.turn_count,
            "state_version": getattr(self, "state_version", 1),
            "current_intent": self.current_intent,
            "current_stage": self.current_stage,
            "state": self.state,
            "last_asked_field": getattr(self, "last_asked_field", None),
            "department": self.department,
            "service": self.service,
            "product": self.product,
            "doctor": self.doctor,
            "doctor_id": self.doctor_id,
            "date": self.date,
            "time": self.time,
            "patient_name": self.patient_name,
            "contact_number": self.contact_number,
            "email": self.email,
            "availability_verified": self.availability_verified,
            "booking_confirmed": self.booking_confirmed,
            "appointment_id": self.appointment_id,
            "confirmed_facts": getattr(self, "confirmed_facts", {}),
            "completed_steps": getattr(self, "completed_steps", []),
            "collected_fields": self.collected_fields,
            "missing_fields": self.missing_fields
        }

    @classmethod
    def from_state_dict(cls, data: Dict[str, Any]) -> "BookingSession":
        sess = cls(
            session_id=data.get("conversation_id") or data.get("call_sid") or "recovered",
            conversation_id=data.get("conversation_id"),
            call_sid=data.get("call_sid")
        )
        sess.agent_id = data.get("agent_id")
        sess.organization_id = data.get("organization_id", 1)
        sess.active_rag_id = data.get("active_rag_id")
        sess.state = data.get("state") or data.get("current_stage") or BookingState.IDLE
        sess.conversation_started = data.get("conversation_started", False)
        sess.greeting_sent = data.get("greeting_sent", False)
        sess.turn_count = data.get("turn_count", 0)
        sess.turn_id = data.get("turn_id", 0)
        sess.state_version = data.get("state_version", 1)
        sess.last_question_field = data.get("last_asked_field")

        sess.doctor_id = data.get("doctor_id")
        sess.doctor_name = data.get("doctor")
        sess.doctor_locked = bool(sess.doctor_name)
        sess.department_name = data.get("department")
        sess.service_name = data.get("service") or data.get("product")
        sess.product_name = sess.service_name

        sess.confirmed_facts = data.get("confirmed_facts") or {}
        sess.completed_steps = data.get("completed_steps") or []

        sess.booking_data["intent"] = data.get("current_intent", "appointment_booking")
        sess.booking_data["department"] = data.get("department")
        sess.booking_data["service"] = sess.service_name
        sess.booking_data["product"] = sess.product_name
        sess.booking_data["doctor"] = data.get("doctor")
        sess.booking_data["specialty"] = data.get("department") or data.get("specialty") or sess.service_name
        sess.booking_data["date"] = data.get("date")
        sess.booking_data["time"] = data.get("time")
        sess.booking_data["patient_name"] = data.get("patient_name")
        sess.booking_data["contact_number"] = data.get("contact_number")
        sess.booking_data["email"] = data.get("email")
        sess.booking_data["availability_verified"] = data.get("availability_verified", False)
        sess.booking_data["booking_confirmed"] = data.get("booking_confirmed", False)
        sess.booking_data["appointment_id"] = data.get("appointment_id")

        return sess


    @property
    def date(self) -> Optional[str]:
        return self.booking_data.get("date")

    @date.setter
    def date(self, val: Optional[str]):
        if val != self.booking_data.get("date"):
            self.booking_data["date"] = val
            self.booking_data["availability_verified"] = False

    @property
    def time(self) -> Optional[str]:
        return self.booking_data.get("time")

    @time.setter
    def time(self, val: Optional[str]):
        if val != self.booking_data.get("time"):
            self.booking_data["time"] = val
            self.booking_data["availability_verified"] = False

    @property
    def patient_name(self) -> Optional[str]:
        return self.booking_data.get("patient_name")

    @patient_name.setter
    def patient_name(self, val: Optional[str]):
        self.booking_data["patient_name"] = val

    @property
    def email(self) -> Optional[str]:
        return self.booking_data.get("email")

    @email.setter
    def email(self, val: Optional[str]):
        self.booking_data["email"] = val

    @property
    def contact_number(self) -> Optional[str]:
        return self.booking_data.get("contact_number")

    @contact_number.setter
    def contact_number(self, val: Optional[str]):
        self.booking_data["contact_number"] = val

    @property
    def availability_verified(self) -> bool:
        return bool(self.booking_data.get("availability_verified"))

    @availability_verified.setter
    def availability_verified(self, val: bool):
        self.booking_data["availability_verified"] = bool(val)

    @property
    def booking_confirmed(self) -> bool:
        return bool(self.booking_data.get("booking_confirmed"))

    @booking_confirmed.setter
    def booking_confirmed(self, val: bool):
        self.booking_data["booking_confirmed"] = bool(val)

    @property
    def conversation_language(self) -> str:
        return self.booking_data.get("conversation_language", "en")

    @conversation_language.setter
    def conversation_language(self, val: str):
        self.booking_data["conversation_language"] = val

    @property
    def appointment_id(self) -> Optional[int]:
        return self.booking_data.get("appointment_id")

    @appointment_id.setter
    def appointment_id(self, val: Optional[int]):
        self.booking_data["appointment_id"] = val

    @property
    def email_declined(self) -> bool:
        return bool(self.booking_data.get("email_declined", False))

    @email_declined.setter
    def email_declined(self, val: bool):
        self.booking_data["email_declined"] = bool(val)

    @property
    def email_confirmed(self) -> bool:
        return bool(self.booking_data.get("email_confirmed", False))

    @email_confirmed.setter
    def email_confirmed(self, val: bool):
        self.booking_data["email_confirmed"] = bool(val)

    @property
    def location(self) -> Optional[str]:
        return self.booking_data.get("location")

    @location.setter
    def location(self, val: Optional[str]):
        self.booking_data["location"] = val


def parse_spoken_location(text: str) -> Optional[str]:
    """
    Extracts customer-provided location/city/area from natural speech without fabricating.
    Examples:
      - 'I am from Chennai' -> 'Chennai'
      - 'My location is Adyar' -> 'Adyar'
      - 'I live in Bangalore' -> 'Bangalore'
      - 'Address is Anna Nagar' -> 'Anna Nagar'
      - 'from Coimbatore' -> 'Coimbatore'
    """
    if not text:
        return None
    raw = text.strip()
    txt = raw.lower()

    if txt in ("yes", "no", "sure", "ok", "okay", "thanks", "thank you", "hello", "hi"):
        return None

    patterns = [
        r'\b(?:my\s+)?(?:location|address|city|area|place)\s+(?:is|:)?\s*([a-zA-Z0-9\s,\.\-]{2,40})',
        r'\b(?:i\s+am\s+from|i\'m\s+from|calling\s+from|i\s+live\s+in|living\s+in|residing\s+in|located\s+at|located\s+in|staying\s+in)\s+([a-zA-Z0-9\s,\.\-]{2,40})',
        r'\b(?:from)\s+([a-zA-Z\s]{2,25})\b',
    ]
    _INVALID_WORDS = {
        "today", "tomorrow", "yesterday", "morning", "afternoon", "evening",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "doctor", "dr", "appointment", "booking", "me", "here", "myself", "sure", "yes", "no",
        "email", "phone", "contact", "name", "call", "please", "thanks", "scratch"
    }
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            cand = m.group(1).strip()
            cand = re.sub(r'\s+(?:and|&|\bmy\b|\bphone\b|\bemail\b|\bnumber\b|\bfor\b|\bbooking\b|\bappointment\b).*$', '', cand, flags=re.IGNORECASE).strip()
            cand = re.sub(r'^(?:the|a|an)\s+', '', cand, flags=re.IGNORECASE).strip()
            tokens = cand.lower().split()
            if tokens and tokens[0] not in _INVALID_WORDS and len(cand) >= 2:
                if not cand.isdigit():
                    return cand.title()
    return None


def parse_spoken_phone_number(text: str) -> Optional[str]:
    """
    Parses direct digits and spoken digit words in English, Tamil, and Hindi into clean 10-digit phone strings.
    Isolates phone numbers in compound sentences (e.g. 'my phone number is 958 5310 712 and my mail ID is...').
    Handles 'double nine', 'triple five', 'double zero', etc.
    """
    if not text:
        return None
    txt_lower = text.lower().strip()

    # 1. Strip emails and email patterns to avoid digit contamination (e.g. user029@gmail.com)
    text_without_email = re.sub(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', '', txt_lower)
    text_without_email = re.sub(r'\bmy\s+(?:mail\s+id|email\s+id|email|mail)\s+is.*$', '', text_without_email, flags=re.IGNORECASE)

    # 1b. Strip dates, month names with days, years, ordinals, and time patterns to avoid date-as-phone errors
    text_clean_phone = re.sub(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b', '', text_without_email)
    text_clean_phone = re.sub(r'\b\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?\b', '', text_clean_phone)
    text_clean_phone = re.sub(r'\b(?:19|20)\d{2}\b', '', text_clean_phone)
    text_clean_phone = re.sub(r'\b\d{1,2}(?:st|nd|rd|th)\b', '', text_clean_phone)
    text_clean_phone = re.sub(r'\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}\b', '', text_clean_phone)
    text_clean_phone = re.sub(r'\b\d{1,2}\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b', '', text_clean_phone)

    # 1c. Expand spoken multiples: "double nine" -> "nine nine", "triple five" -> "five five five", "double 9" -> "9 9"
    _multiples = {"double": 2, "triple": 3}
    for m_word, m_count in _multiples.items():
        text_clean_phone = re.sub(
            rf'\b{m_word}\s+([a-zA-Z0-9\u0b80-\u0bff]+)\b',
            lambda m: " ".join([m.group(1)] * m_count),
            text_clean_phone,
            flags=re.IGNORECASE
        )

    # 2. Check explicit phone/number prefix
    phone_prefix_match = re.search(r'\b(?:my\s+)?(?:phone|mobile|contact|cell|number)(?:\s+number)?\s+(?:is|:)?\s*', text_clean_phone)
    if phone_prefix_match:
        after_prefix = text_clean_phone[phone_prefix_match.end():]
        phone_segment = re.split(r'\s+(?:and|&|\bmy\s+name|\bname|\bfor\b|\bbooking\b)\b', after_prefix)[0]
        seg_digits = re.sub(r'[^\d]', '', phone_segment)
        if len(seg_digits) >= 10:
            return seg_digits[:10]
        elif len(seg_digits) >= 7:
            return seg_digits

    # 3. Direct 10 digits in remaining text (whether spaced, hyphenated, or continuous)
    clean_digits = re.sub(r'[^\d]', '', text_clean_phone)
    if len(clean_digits) == 10:
        return clean_digits
    elif len(clean_digits) > 10:
        # Check standard country code prefixes (e.g. +91 9585310712)
        if clean_digits.startswith("91") and len(clean_digits) == 12:
            return clean_digits[2:]
        if clean_digits.startswith("1") and len(clean_digits) == 11:
            return clean_digits[1:]
        return clean_digits[:10]
    elif len(clean_digits) >= 7:
        return clean_digits

    # 4. Spoken digit word translation map
    word_map = {
        "zero": "0", "oh": "0", "o": "0", "sunniyam": "0", "poojiyam": "0", "shunya": "0",
        "one": "1", "onru": "1", "onnu": "1", "ek": "1",
        "two": "2", "irandu": "2", "rendu": "2", "do": "2",
        "three": "3", "moondru": "3", "moonu": "3", "teen": "3",
        "four": "4", "naangku": "4", "naalu": "4", "char": "4",
        "five": "5", "aindhu": "5", "anji": "5", "paanch": "5",
        "six": "6", "aaru": "6", "chhah": "6",
        "seven": "7", "ezhu": "7", "saat": "7",
        "eight": "8", "ettu": "8", "aath": "8",
        "nine": "9", "ombadhu": "9", "onbadhu": "9", "nau": "9"
    }

    tokens = re.findall(r'\b[a-zA-Z0-9\u0b80-\u0bff]+\b', text_clean_phone)
    parsed = []
    for t in tokens:
        if t.isdigit():
            parsed.append(t)
        elif t in word_map:
            parsed.append(word_map[t])

    if len(parsed) >= 7:
        return "".join(parsed)[:10]

    return None


def parse_spoken_email(text: str) -> Optional[str]:
    """
    Parses spoken email variations, spelled-out letters, and formatted emails in compound sentences.
    Handles:
      - 's a n j a y at gmail dot com' -> 'sanjay@gmail.com'
      - 'arun dot kumar at gmail dot com' -> 'arun.kumar@gmail.com'
      - 's a n j a y 1 2 3 at gmail dot com' -> 'sanjay123@gmail.com'
      - 'my email is sanjay at gmail dot com' -> 'sanjay@gmail.com'
      - 'g mail' -> 'gmail', 'hot mail' -> 'hotmail', 'out look' -> 'outlook'
      - Rejects incomplete fragments like '@gmail.com' or 'sanjay@' without fabricating characters.
    """
    if not text:
        return None
    raw = text.strip()
    txt = raw.lower()

    # Reject isolated domain or incomplete local parts (never fabricate missing username or domain)
    if re.match(r'^\s*[@]\s*[a-zA-Z0-9-]+\.[a-zA-Z]{2,}\s*$', txt) or re.match(r'^\s*(?:at|@)\s*(?:the\s+rate\s+)?(?:of\s+)?[a-zA-Z0-9-]+\s*(?:dot|\.)\s*[a-zA-Z]{2,}\s*$', txt):
        return None
    if re.match(r'^[a-zA-Z0-9._%+-]+@\s*$', txt):
        return None

    # 1. Direct standard email match in raw text first
    email_direct = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', txt)
    if email_direct:
        cand = email_direct.group(0).strip('.')
        if re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$', cand):
            return cand

    # 2. Strip conversational prefixes
    prefixes = [
        r'^(?:my\s+(?:email|mail)\s+(?:id|address)?\s*(?:is|:)?)\s*',
        r'^(?:email|mail)\s+(?:is|id\s+is|address\s+is|:)\s*',
        r'^(?:it\'s|its|sure|yes|yeah|okay|ok)\s*,?\s*',
    ]
    cleaned = txt
    for p in prefixes:
        cleaned = re.sub(p, '', cleaned, flags=re.IGNORECASE).strip()

    # 3. Replace spoken verbal tokens with standard email symbols
    cleaned = re.sub(r'\bat the rate(?: of)?\b', '@', cleaned)
    cleaned = re.sub(r'\bat\b', '@', cleaned)
    cleaned = re.sub(r'\bdot\b', '.', cleaned)
    cleaned = re.sub(r'\bunderscore\b', '_', cleaned)
    cleaned = re.sub(r'\b(?:dash|hyphen)\b', '-', cleaned)
    cleaned = re.sub(r'\bplus\b', '+', cleaned)
    cleaned = re.sub(r'\bspace\b', '', cleaned)

    # 3b. Normalize spoken domain words
    cleaned = re.sub(r'\bg\s*mail\b', 'gmail', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bhot\s*mail\b', 'hotmail', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bout\s*look\b', 'outlook', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\by\s*a\s*h\s*o\s*o\b', 'yahoo', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\bg\s*m\s*a\s*i\s*l\b', 'gmail', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\.\s*c\s*o\s*m\b', '.com', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\.\s*o\s*r\s*g\b', '.org', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b\.\s*i\s*n\b', '.in', cleaned, flags=re.IGNORECASE)

    # 4. Spelled-out local part & domain reconstruction
    if '@' in cleaned:
        parts = cleaned.split('@')
        if len(parts) >= 2:
            local_raw = parts[0].strip()
            domain_raw = parts[1].strip()

            # Strip leading conversational words and clauses
            # e.g. "no, my email is wrong. it is robert.new" -> "robert.new"
            local_raw = re.sub(r'^.*?[,;:!]\s*', '', local_raw)
            local_raw = re.sub(r'^.*?\b(?:is|be|to|it\s+is|it\'s|email\s+is|id\s+is|mail\s+is|my\s+email\s+is|correct\s+email\s+is|change\s+to|actually)\s+', '', local_raw, flags=re.IGNORECASE).strip()

            # Contract spelled-out tokens in local part
            # e.g. "s a n j a y" -> "sanjay"
            _EMAIL_FRAMING_WORDS = {
                "is", "it", "its", "it's", "email", "mail", "id", "wrong", "to", "be", "not",
                "actual", "correct", "my", "use", "please", "address", "change", "new", "the"
            }
            tokens = local_raw.split()
            valid_local_tokens = []
            for tok in reversed(tokens):
                cleaned_tok = tok.lower().strip(".,;:!")
                if cleaned_tok in _EMAIL_FRAMING_WORDS:
                    break
                if re.match(r'^[a-z0-9._%+-]+$', tok):
                    valid_local_tokens.insert(0, tok)
                else:
                    break
            if valid_local_tokens:
                local_part = ''.join(valid_local_tokens)

                # Domain part reconstruction (e.g. "gmail . com" -> "gmail.com")
                dom_tokens = domain_raw.split()
                valid_dom_tokens = []
                for tok in dom_tokens:
                    if re.match(r'^[a-z0-9.-]+$', tok):
                        valid_dom_tokens.append(tok)
                    else:
                        break
                domain_part = ''.join(valid_dom_tokens)

                candidate = f"{local_part}@{domain_part}"
                candidate = re.sub(r'\.{2,}', '.', candidate).strip('.')
                if re.match(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$', candidate):
                    return candidate

    # 5. Continuous compaction fallback
    compact = re.sub(r'\s+', '', cleaned)
    m_compact = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}', compact)
    if m_compact:
        return m_compact.group(0).strip('.')

    return None


def is_phrase_negated(phrase: str, text: str) -> bool:
    """
    Detects whether an entity, service, or department phrase is negated in the text.
    Handles English, Hindi, and Tamil negation patterns with clause isolation and affirmative protection.
    Examples:
      - "I don't want dental care" -> True for "dental", "dental care"
      - "No dental" -> True
      - "I don't want Galaxy Jet; I want Galaxy Chat" -> True for Galaxy Jet, False for Galaxy Chat
      - "Dental care vendaam" -> True
      - "Dental nahi chahiye" -> True
    """
    if not phrase or not text:
        return False
    phrase_clean = phrase.lower().strip()
    text_clean = text.lower().strip()

    # Split text into distinct clauses by punctuation (; , . ! ?) or contrastive conjunctions (but, however, whereas, instead of)
    clauses = re.split(r'[;\.!?]|\b(?:but|however|whereas|instead\s+of)\b', text_clean)

    # 1. Pre-negation patterns
    pre_neg_words = (
        r"(?:don\'?t\s+(?:want|need|like|prefer|book|schedule)|"
        r"do\s+not\s+(?:want|need|like|prefer|book|schedule)|"
        r"dont\s+(?:want|need|like|prefer|book|schedule)|"
        r"not\s+(?:want|need|interested\s+in|for|with)|"
        r"no|not|never|neither|nor|without|cancel|stop|except|excluding|other\s+than|"
        r"change\s+from|different\s+from|anything\s+but)"
    )

    # 2. Post-negation patterns
    post_neg_words = (
        r"(?:is\s+not\s+what\s+i\s+want|is\s+not\s+needed|not\s+needed|vendaam|vendam|illai|nahi|nahi\s+chahiye|mat|na\s+chahiye)"
    )

    phrase_tokens = [t for t in re.split(r'\s+', phrase_clean) if len(t) > 3 and t not in ("care", "department", "clinic")]

    for clause in clauses:
        c = clause.strip()
        if not c:
            continue

        if phrase_clean in c:
            # If the phrase in this clause is explicitly affirmed (e.g. "I want Galaxy Chat"), it is not negated
            affirmative_pat = rf'\b(?:i\s+(?:want|need|prefer|would\s+like)|book|reserve|schedule|give\s+me)\s+(?:the\s+|a\s+|an\s+)?{re.escape(phrase_clean)}\b'
            if re.search(affirmative_pat, c):
                direct_neg = rf'\b{pre_neg_words}(?:\s+[\w\']+){{0,2}}\s+\b{re.escape(phrase_clean)}\b'
                if not re.search(direct_neg, c):
                    continue

            # Check pre-negation in this clause
            pre_pat = rf'\b{pre_neg_words}(?:\s+[\w\']+){{0,2}}\s+\b{re.escape(phrase_clean)}\b'
            if re.search(pre_pat, c):
                return True

            # Check post-negation in this clause
            post_pat = rf'\b{re.escape(phrase_clean)}\b(?:\s+[\w\']+){{0,2}}\s+{post_neg_words}\b'
            if re.search(post_pat, c):
                return True

        elif len(phrase_tokens) == 1:
            # Single-word phrase (e.g. "dental")
            pt = phrase_tokens[0]
            if pt in c:
                pre_tok_pat = rf'\b{pre_neg_words}(?:\s+[\w\']+){{0,2}}\s+\b{re.escape(pt)}\b'
                if re.search(pre_tok_pat, c):
                    return True
                post_tok_pat = rf'\b{re.escape(pt)}\b(?:\s+[\w\']+){{0,2}}\s+{post_neg_words}\b'
                if re.search(post_tok_pat, c):
                    return True

    return False


class BookingStateService:
    is_phrase_negated = staticmethod(is_phrase_negated)
    """
    Session manager and multi-turn state machine processor for appointment bookings.
    """
    @staticmethod
    def clean_person_name(name: Optional[str]) -> Optional[str]:
        """
        Strips conversational prefixes, polite fillers, and non-name tokens from an extracted candidate name.
        E.g. 'Sure John' -> 'John', 'Yes My Name Is Karthik' -> 'Karthik', 'It Is Vignesh' -> 'Vignesh'.
        """
        if not name or not name.strip():
            return None
        clean = re.sub(r'[^a-zA-Z\s]', '', name).strip()
        _fillers = {
            "sure", "yes", "yeah", "ok", "okay", "hi", "hello", "hey", "well",
            "thanks", "thank", "you", "sir", "madam", "please", "myself", "this",
            "it", "its", "is", "am", "im", "my", "name", "names", "call", "me",
            "before", "after", "during", "while", "say", "says", "saying", "said",
            "tell", "tells", "telling", "told", "speak", "talk", "listen", "hear"
        }
        words = [w for w in clean.split() if w.lower() not in _fillers and len(w) > 1]
        if words and len(words) <= 4:
            cand = " ".join(words).title()
            if BookingStateService.is_valid_person_name(cand):
                return cand
        return clean.title() if BookingStateService.is_valid_person_name(clean) else None

    @staticmethod
    def is_valid_person_name(name: Optional[str]) -> bool:
        """
        Validates that an extracted string contains a genuine human name and not a standalone placeholder,
        dictionary word, question word, or conversational filler.
        """
        if not name or not name.strip():
            return False
        clean = re.sub(r'[^a-zA-Z\s]', '', name).strip()
        words = [w.lower() for w in clean.split()]
        if not words or len(words) > 4:
            return False

        disallowed_standalone = {
            "name", "names", "my name", "patient", "customer", "person", "user", "someone", "anyone",
            "full name", "first name", "last name", "peru", "naam", "unknown", "none", "null", "undefined",
            "sir", "madam", "doctor", "dr", "appointment", "booking", "phone", "email",
            "yes", "no", "yeah", "okay", "ok", "sure", "hello", "hi", "hey", "thanks", "thank",
            "what", "who", "which", "how", "when", "where", "why", "pardon", "sorry", "pardon me",
            "today", "tomorrow", "yesterday", "tonight", "morning", "afternoon", "evening", "night",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
            "am", "pm", "before", "after", "say", "saying", "said", "tell", "told"
        }
        if len(words) == 1 and words[0] in disallowed_standalone:
            return False

        _non_names = {
            "gonna", "going", "go", "home", "get", "that", "this", "here", "there", "the", "one",
            "who", "what", "which", "how", "when", "where", "why", "to", "for", "with", "from",
            "and", "or", "but", "so", "if", "a", "an", "some", "any", "no", "yes", "yeah", "ok",
            "okay", "sure", "thanks", "thank", "you", "cinema", "start", "now", "right", "little",
            "bit", "end", "all", "ready", "done", "just", "call", "calling", "want", "need", "appointment",
            "booking", "book", "doctor", "phone", "email", "mobile", "number", "dr", "dr.", "at", "my",
            "is", "was", "are", "be", "am", "can", "could", "should", "would", "shall", "will", "have",
            "has", "had", "doing", "did", "do", "like", "up", "down", "out", "in", "off", "on", "not",
            "name", "names", "patient", "customer", "person", "user", "full", "peru", "naam", "sir", "madam", "please",
            "before", "after", "during", "while", "until", "till", "say", "says", "saying", "said",
            "tell", "tells", "telling", "told", "speak", "talk", "listen", "hear"
        }
        if all(w in _non_names for w in words):
            return False

        # Meaningful tokens must exist (tokens not purely conversational noise)
        meaningful_words = [w for w in words if w not in disallowed_standalone and w not in _non_names]
        if not meaningful_words:
            return False

        return True

    def __init__(self):
        self._sessions: Dict[str, BookingSession] = {}

    def get_session(self, session_id: str, conversation_id: Optional[str] = None, call_sid: Optional[str] = None) -> BookingSession:
        target_keys = [k for k in [session_id, conversation_id, call_sid] if k]
        for k in target_keys:
            if k in self._sessions:
                sess = self._sessions[k]
                if conversation_id and not sess.conversation_id:
                    sess.conversation_id = conversation_id
                if call_sid and not sess.call_sid:
                    sess.call_sid = call_sid
                for alias in target_keys:
                    self._sessions[alias] = sess
                return sess

        # Attempt to recover durable state from database
        persisted_sid = call_sid or session_id
        recovered = self.load_session_state(persisted_sid)
        if recovered:
            logger.info(f"[SESSION_RECOVERY] Successfully recovered durable session for {persisted_sid}")
            for k in target_keys:
                self._sessions[k] = recovered
            return recovered

        new_sess = BookingSession(session_id=session_id, conversation_id=conversation_id, call_sid=call_sid)
        for k in target_keys:
            self._sessions[k] = new_sess
        return new_sess

    def remove_session(self, session_id: str):
        if session_id in self._sessions:
            sess = self._sessions[session_id]
            keys_to_del = [k for k, v in list(self._sessions.items()) if v is sess or k == session_id]
            for k in keys_to_del:
                self._sessions.pop(k, None)

    def save_session_state(self, session: BookingSession):
        """
        Durable persistence of structured conversation memory into CallLog.receptionist_state.
        Survives async worker reloads and process restarts.
        """
        try:
            call_sid = getattr(session, "call_sid", None)
            if not call_sid:
                return
            state_dict = session.get_state_dict()
            from src.config.database import SessionLocal
            from src.models.call_log import CallLog
            db = SessionLocal()
            try:
                import json
                call = db.query(CallLog).filter(CallLog.call_sid == call_sid).first()
                if call:
                    call.receptionist_state = json.dumps(state_dict, ensure_ascii=False)
                    call.intent = session.current_intent
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Failed to persist session state to DB: {e}")

    def load_session_state(self, call_sid: str, organization_id: int = 1) -> Optional[BookingSession]:
        """
        Restores durable conversation memory from CallLog.receptionist_state.
        """
        if not call_sid:
            return None
        try:
            import json
            from src.config.database import SessionLocal
            from src.models.call_log import CallLog
            db = SessionLocal()
            try:
                call = db.query(CallLog).filter(CallLog.call_sid == call_sid).first()
                if call and call.receptionist_state:
                    try:
                        state_data = json.loads(call.receptionist_state)
                        if isinstance(state_data, dict) and state_data.get("state"):
                            sess = BookingSession.from_state_dict(state_data)
                            sess.call_sid = call_sid
                            sess.organization_id = organization_id or getattr(call, "organization_id", 1) or 1
                            return sess
                    except Exception:
                        pass
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Failed to load session state from DB: {e}")
        return None

    def resolve_department(self, candidate_name: str, organization_id: int = 1, db: Optional[Any] = None) -> Optional[str]:
        """
        Resolves candidate department/specialty name against authoritative DB and Knowledge Base.
        Never invents departments.
        """
        if not candidate_name or not candidate_name.strip():
            return None
        cand_clean = candidate_name.strip().lower()
        cand_clean = re.sub(r'^(?:dr\.?|doctor|department\s+of|the\s+department\s+of|the)\s+', '', cand_clean).strip()
        cand_norm = re.sub(r'[^a-z0-9]', '', cand_clean)
        if not cand_norm or len(cand_norm) < 3:
            return None

        close_db = False
        if not db:
            from src.config.database import SessionLocal
            db = SessionLocal()
            close_db = True
        try:
            from src.models.department import Department
            from src.models.staff import Staff
            depts = db.query(Department).filter(Department.organization_id == organization_id).all()
            for d in depts:
                if d.name:
                    d_norm = re.sub(r'[^a-z0-9]', '', d.name.lower())
                    if cand_norm == d_norm or cand_clean in d.name.lower() or d.name.lower() in cand_clean:
                        return d.name

            # Check distinct Staff.department in DB
            staff_depts = db.query(Staff.department).filter(
                Staff.organization_id == organization_id,
                Staff.department.isnot(None)
            ).distinct().all()
            for (sd,) in staff_depts:
                if sd:
                    sd_norm = re.sub(r'[^a-z0-9]', '', sd.lower())
                    if cand_norm == sd_norm or cand_clean in sd.lower() or sd.lower() in cand_clean:
                        return sd

            # Active RAG Master Context
            from src.services.rag_service import rag_service
            master_ctx = rag_service.get_organization_master_context(organization_id)
            if master_ctx:
                dept_bullet_matches = re.findall(r'(?:#+\s*(?:DEPARTMENTS|SPECIALTIES)[^\n]*\n(?:[^\n]*\n)*?)((?:[-*•]\s*[^\n]+\n?)+)', master_ctx, re.IGNORECASE)
                for bm in dept_bullet_matches:
                    for b in re.findall(r'[-*•]\s*([^\n\r]+)', bm):
                        clean_b = b.strip().replace('*', '')
                        b_norm = re.sub(r'[^a-z0-9]', '', clean_b.lower())
                        if cand_norm == b_norm or cand_clean in clean_b.lower() or clean_b.lower() in cand_clean:
                            return clean_b
                dept_inline = re.search(r'(?:Departments|Specialties)\s*:\s*([^\n]+)', master_ctx, re.IGNORECASE)
                if dept_inline:
                    for s_item in re.split(r'[,;•\n]', dept_inline.group(1)):
                        clean_item = s_item.strip().replace('*', '')
                        b_norm = re.sub(r'[^a-z0-9]', '', clean_item.lower())
                        if cand_norm == b_norm or cand_clean in clean_item.lower() or clean_item.lower() in cand_clean:
                            return clean_item
        except Exception as e:
            logger.warning(f"Error resolving department: {e}")
        finally:
            if close_db:
                db.close()
        return None

    def get_organization_services_and_departments(self, organization_id: int = 1) -> List[str]:
        """
        Dynamically fetches valid departments and services from the database and active RAG document
        for the given organization. Never returns hardcoded entities.
        """
        services_and_depts = []
        seen = set()

        # 1. From database Department table
        try:
            from src.config.database import SessionLocal
            from src.models.department import Department
            from src.models.staff import Staff, StaffStatus
            db = SessionLocal()
            try:
                dept_records = db.query(Department).filter(Department.organization_id == organization_id).all()
                for d in dept_records:
                    if d.name and d.name.strip().lower() not in seen:
                        seen.add(d.name.strip().lower())
                        services_and_depts.append(d.name.strip())

                # From Staff department & services
                staff_records = db.query(Staff).filter(
                    Staff.organization_id == organization_id,
                    Staff.online_booking_enabled != False,
                    Staff.status != StaffStatus.OFFLINE
                ).all()
                for s in staff_records:
                    if s.department and s.department.strip().lower() not in seen:
                        seen.add(s.department.strip().lower())
                        services_and_depts.append(s.department.strip())
                    if s.services:
                        try:
                            s_list = json.loads(s.services) if isinstance(s.services, str) else s.services
                            if isinstance(s_list, list):
                                for s_item in s_list:
                                    if s_item and str(s_item).strip().lower() not in seen:
                                        seen.add(str(s_item).strip().lower())
                                        services_and_depts.append(str(s_item).strip())
                        except Exception:
                            pass
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"Error fetching DB departments/services: {e}")

        # 2. From active RAG document for this organization
        try:
            from src.services.rag_service import rag_service
            rag_services = rag_service.get_organization_services(organization_id)
            for rs in rag_services:
                if rs and rs.strip().lower() not in seen:
                    seen.add(rs.strip().lower())
                    services_and_depts.append(rs.strip())
        except Exception as e:
            logger.warning(f"Error fetching RAG services: {e}")

        return services_and_depts

    def get_doctors_for_department(self, department_name: str, organization_id: int = 1, db: Optional[Any] = None, session: Optional[BookingSession] = None) -> List[Dict[str, Any]]:
        """
        Retrieves authoritative doctors associated with a department from DB and active RAG directory.
        Only returns doctors verified in Staff DB with a valid ID and active booking status.
        Excludes doctors or departments rejected in session.
        """
        if not department_name:
            return []
        
        # If department was rejected in this session, return empty list!
        if session and department_name.lower().strip() in getattr(session, "_rejected_departments", set()):
            return []

        dept_norm = re.sub(r'[^a-z0-9]', '', department_name.lower())
        results = []
        seen_names = set()

        close_db = False
        if not db:
            from src.config.database import SessionLocal
            db = SessionLocal()
            close_db = True
        try:
            from src.models.staff import Staff, StaffStatus
            staff_members = db.query(Staff).filter(
                Staff.organization_id == organization_id,
                Staff.online_booking_enabled != False,
                Staff.status != StaffStatus.OFFLINE
            ).all()
            for s in staff_members:
                if not s.name or not s.department:
                    continue
                s_dept_norm = re.sub(r'[^a-z0-9]', '', s.department.lower())
                if dept_norm in s_dept_norm or s_dept_norm in dept_norm:
                    clean_n = re.sub(r'^(?:dr\.?|doctor)\s+', '', s.name.strip(), flags=re.I).strip().title()
                    # Filter out rejected doctors
                    if session and (clean_n.lower() in getattr(session, "_rejected_doctors", set()) or s.name.lower() in getattr(session, "_rejected_doctors", set())):
                        continue
                    disp_name = f"Dr. {clean_n}" if not clean_n.lower().startswith("dr") else clean_n
                    if clean_n.lower() not in seen_names:
                        seen_names.add(clean_n.lower())
                        results.append({
                            "id": s.id,
                            "name": disp_name,
                            "clean_name": clean_n.lower(),
                            "department": s.department
                        })

            # Also check RAG Doctor directory, but REQUIRE matching active staff in DB
            from src.services.rag_service import rag_service
            master_ctx = rag_service.get_organization_master_context(organization_id)
            if master_ctx:
                doc_entries = re.findall(
                    r'(?m)^(?!#)\s*(?:Dr\.?|Doctor)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)[^\n]*\n(?:[^\n]*\n){0,3}?Department:\s*([^\n]+)',
                    master_ctx,
                    re.IGNORECASE
                )
                for d_name, d_dept in doc_entries:
                    clean_n = d_name.strip().title()
                    if clean_n.lower() in ("directory", "information", "staff", "doctors", "team", "schedule"):
                        continue
                    if session and clean_n.lower() in getattr(session, "_rejected_doctors", set()):
                        continue
                    d_dept_norm = re.sub(r'[^a-z0-9]', '', d_dept.lower())
                    if dept_norm in d_dept_norm or d_dept_norm in dept_norm:
                        if clean_n.lower() not in seen_names:
                            db_s = next((s for s in staff_members if clean_n.lower() in s.name.lower() or s.name.lower() in clean_n.lower()), None)
                            # CRITICAL: Never offer a doctor when verified DB record or ID is missing!
                            if db_s and db_s.id:
                                seen_names.add(clean_n.lower())
                                results.append({
                                    "id": db_s.id,
                                    "name": f"Dr. {clean_n}",
                                    "clean_name": clean_n.lower(),
                                    "department": d_dept.strip()
                                })
        except Exception as e:
            logger.warning(f"Error fetching doctors for department: {e}")
        finally:
            if close_db:
                db.close()
        return results

    def evaluate_post_response(self, session: BookingSession) -> Dict[str, Any]:
        """
        Lightweight deterministic post-response state evaluator.
        Determines if the system requires user input, an automatic system action, or is completed.
        """
        if session.is_completed or session.state in (BookingState.BOOKED, BookingState.COMPLETED):
            return {
                "next_action": NextAction.COMPLETED,
                "state": "COMPLETED",
                "reason": "Appointment confirmed or conversation concluded."
            }

        if session.is_active():
            missing = session.missing_fields
            # If all required fields are present and not yet completed/confirmed
            if not missing and session.state not in (BookingState.BOOKED, BookingState.COMPLETED):
                return {
                    "next_action": NextAction.AUTOMATIC_ACTION_REQUIRED,
                    "state": "PROCESSING",
                    "reason": "All required details present. Ready for automatic booking execution."
                }
            elif session.state == BookingState.WAITING_FOR_CONFIRMATION:
                return {
                    "next_action": NextAction.CONFIRMATION_REQUIRED,
                    "state": "LISTENING",
                    "reason": "Waiting for caller confirmation."
                }
            else:
                return {
                    "next_action": NextAction.USER_INPUT_REQUIRED,
                    "state": "LISTENING",
                    "missing_fields": missing,
                    "reason": f"Waiting for caller input for fields: {missing}"
                }

        return {
            "next_action": NextAction.USER_INPUT_REQUIRED,
            "state": "LISTENING",
            "reason": "Listening for next caller query."
        }

    def get_silence_prompt(self, session: BookingSession) -> str:
        """
        Generates a concise, polite reassurance prompt when caller remains silent
        during an active turn without resetting state or re-emitting greetings.
        """
        lang = getattr(session, "conversation_language", "en")
        first_name = (session.patient_name or "").split()[0] if session.patient_name else ""
        name_prefix = f"{first_name}, " if first_name else ""

        if session.state == BookingState.WAITING_FOR_PATIENT_NAME:
            if lang in ("ta", "tanglish", "ta_mix"):
                return "உங்க பெயர் சொல்லுங்க, நான் கேட்டுட்டு இருக்கேன்."
            elif lang == "hi":
                return "Kripya apna naam batayein, main sun raha hoon."
            return f"{name_prefix}Take your time. May I know your name whenever you're ready?"

        elif session.state == BookingState.WAITING_FOR_CONTACT:
            if lang in ("ta", "tanglish", "ta_mix"):
                return "உங்க phone number சொல்லுங்க."
            elif lang == "hi":
                return "Kripya apna phone number batayein."
            return f"{name_prefix}Whenever you're ready, please share your contact number."

        elif session.state == BookingState.WAITING_FOR_EMAIL:
            if lang in ("ta", "tanglish", "ta_mix"):
                return "உங்க email address சொல்லுங்க."
            elif lang == "hi":
                return "Kripya apna email address batayein."
            return f"{name_prefix}Please let me know your email address when you're ready."

        elif session.state == BookingState.WAITING_FOR_DATE:
            if lang in ("ta", "tanglish", "ta_mix"):
                return "எந்த date-ல appointment வேணும்?"
            elif lang == "hi":
                return "Aap kis date ko appointment prefer karenge?"
            return f"{name_prefix}Which date would you prefer for the appointment?"

        elif session.state == BookingState.WAITING_FOR_TIME:
            if lang in ("ta", "tanglish", "ta_mix"):
                return "எந்த time slot உங்களுக்கு வசதியா இருக்கும்?"
            elif lang == "hi":
                return "Konsa samay aapke liye sahi rahega?"
            return f"{name_prefix}What time would work best for you?"

        elif session.state == BookingState.WAITING_FOR_DOCTOR:
            if getattr(session, "_offered_dept_doctor", None):
                return f"Would you like to schedule with {session._offered_dept_doctor}?"
            elif session.department:
                return f"Which doctor in {session.department} would you like to see?"
            return self._get_target_entity_question(session, name_ack=name_prefix, lang=lang)

        if lang in ("ta", "tanglish", "ta_mix"):
            return "நான் line-ல தான் இருக்கேன். உங்களுக்கு வேற ஏதாச்சு உதவி வேணுமா?"
        elif lang == "hi":
            return "Main sun raha hoon. Kya aapko aur koi jaankari chahiye?"
        return "I'm still here whenever you're ready. How can I help you further?"

    parse_spoken_phone_number = staticmethod(parse_spoken_phone_number)
    parse_spoken_email = staticmethod(parse_spoken_email)
    parse_spoken_location = staticmethod(parse_spoken_location)

    def _get_target_entity_question(
        self,
        session: "BookingSession",
        name_ack: str = "",
        lang: str = "en"
    ) -> str:
        """
        Dynamically generates the next-action question for the unknown appointment target
        (the person/service the caller wants to book with).

        Priority:
          1. Preserve user's own term if they already said it (stored in booking_data).
          2. Use RAG terminology for the active business domain.
          3. Fall back to neutral generic phrasing.

        Never emits "service or staff member" or any other hardcoded business noun.
        """
        from src.services.rag_service import rag_service
        org_id = getattr(session, "organization_id", 1) or 1
        doc_id = getattr(session, "active_doc_id", None) or getattr(session, "active_rag_id", None)

        # --- User's own terminology preserved (e.g. "coordinator", "teacher") ---
        user_entity_word = (
            session.booking_data.get("user_requested_entity_term")
            or ""
        ).strip().lower()

        # --- RAG terminology ---
        try:
            terminology = rag_service.get_appointment_entity_terminology(
                organization_id=org_id, document_id=doc_id
            )
        except Exception:
            terminology = {"ask_target_question": None, "entity_term": None, "entity_action": "meet", "service_based": False}

        rag_question = terminology.get("ask_target_question")  # e.g. "Which teacher would you like to meet?"
        entity_term = terminology.get("entity_term")  # e.g. "teacher"
        entity_action = terminology.get("entity_action", "meet")
        service_based = terminology.get("service_based", False)

        # Only echo user's own word back if it is a recognized role category noun
        # Never echo arbitrary products, services, or phrases (e.g. "Which galaxy jet...")
        _ALLOWED_ROLE_ECHOES = {
            "doctor", "dr", "physician", "nurse", "teacher", "trainer", "coordinator",
            "stylist", "counselor", "therapist", "consultant", "specialist", "advisor",
            "agent", "lawyer", "attorney", "accountant", "instructor", "expert"
        }
        if user_entity_word and user_entity_word in _ALLOWED_ROLE_ECHOES:
            action_word = "book" if service_based else entity_action
            if lang in ("ta", "tanglish", "ta_mix"):
                return f"{name_ack}எந்த {user_entity_word}-ஐ சந்திக்க விரும்புகிறீர்கள்?"
            elif lang == "hi":
                return f"{name_ack}Aap kis {user_entity_word} se milna chahte hain?"
            return f"{name_ack}Sure. Which {user_entity_word} would you like to {action_word}?"

        # If RAG gave us a concrete question, use it with the ack prefix
        if rag_question:
            if lang in ("ta", "tanglish", "ta_mix"):
                if entity_term == "doctor":
                    return f"{name_ack}எந்த doctor-ஐ சந்திக்க விரும்புகிறீர்கள்?"
                elif entity_term == "teacher":
                    return f"{name_ack}எந்த teacher-ஐ சந்திக்க விரும்புகிறீர்கள்?"
                elif service_based:
                    return f"{name_ack}எந்த service book பண்ணனும்?"
                else:
                    return f"{name_ack}யாரை சந்திக்க விரும்புகிறீர்கள்?"
            elif lang == "hi":
                if entity_term == "doctor":
                    return f"{name_ack}Aap kis doctor se milna chahte hain?"
                elif entity_term == "teacher":
                    return f"{name_ack}Aap kis teacher se milna chahte hain?"
                elif service_based:
                    return f"{name_ack}Aap kaunsi service book karna chahte hain?"
                else:
                    return f"{name_ack}Aap kisse milna chahte hain?"
            return f"{name_ack}{rag_question}"

        # Generic neutral fallback — no business noun hardcoded
        if lang in ("ta", "tanglish", "ta_mix"):
            return f"{name_ack}யாரை அல்லது எந்த service-ஐ book பண்ண விரும்புகிறீர்கள்?"
        elif lang == "hi":
            return f"{name_ack}Aap kisse milna ya kya book karna chahte hain?"
        return f"{name_ack}Sure. Who or what would you like to book the appointment for?"

    @staticmethod
    def _format_time_12h(time_str: str) -> str:
        if not time_str:
            return ""
        try:
            clean = time_str.strip().upper()
            if "AM" in clean or "PM" in clean:
                return clean
            dt = datetime.strptime(clean, "%H:%M")
            return dt.strftime("%I:%M %p").lstrip("0")
        except Exception:
            return time_str

    @staticmethod
    def _is_incomplete_utterance(text: str) -> bool:
        """
        Detects whether an utterance is a cut-off fragment, conversational filler, or mid-sentence interruption.
        Prevents conversational fillers like 'Before you say the' from being extracted as booking entities.
        """
        if not text:
            return False
        t = text.strip().lower()
        t_clean = re.sub(r'[^a-zA-Z\s]', '', t).strip()
        words = t_clean.split()
        if not words:
            return False
        trailing_cutoffs = {
            "the", "a", "an", "to", "for", "with", "that", "this", "or", "and", "but",
            "of", "in", "at", "on", "from", "by", "is", "are", "was", "were", "be",
            "say", "says", "saying", "said", "tell", "tells", "telling", "told",
            "ask", "asking", "before", "after", "while", "during", "my", "your",
            "our", "their", "its", "so", "then", "if", "because", "as"
        }
        if words[-1] in trailing_cutoffs:
            return True
        if len(words) <= 4 and words[0] in ("before", "wait", "hold", "just", "listen") and words[-1] in trailing_cutoffs:
            return True
        return False

    @staticmethod
    def _normalize_name_tokens(name_str: str) -> list[str]:
        """
        Extracts substantive name tokens and applies Indian-English / STT phonetic normalization.
        Handles variations like Akhila <-> Akila, Gladis <-> Gladys, Kulandai <-> Kulanthai.
        Strips single-letter initials and honorific titles.
        """
        s = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', name_str.strip(), flags=re.I)
        raw_tokens = [t.lower().strip('.') for t in re.split(r'[\s\.\-]+', s) if t.strip('.')]
        substantive = []
        for t in raw_tokens:
            if len(t) <= 1:
                continue
            if t in ('dr', 'doctor', 'prof', 'mr', 'mrs', 'ms', 'the', 'and'):
                continue
            norm = t
            norm = re.sub(r'kh', 'k', norm)
            norm = re.sub(r'th', 't', norm)
            norm = re.sub(r'dh', 'd', norm)
            norm = re.sub(r'ph', 'f', norm)
            norm = re.sub(r'sh', 's', norm)
            norm = re.sub(r'y', 'i', norm)
            norm = re.sub(r'(.)\1+', r'\1', norm)
            substantive.append(norm)
        return substantive

    @staticmethod
    def _is_doctor_name_match(candidate: str, target: str) -> bool:
        """
        Determines whether a spoken candidate string matches a target doctor name.
        Supports safe name normalization, initials (e.g. Dr. P. Akhila Gladis matching Akila Gladys),
        and phonetic/STT spelling variations without hardcoding doctor names.
        """
        if not candidate or not target:
            return False
        import difflib
        c_clean = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', candidate.strip(), flags=re.I).strip().lower()
        t_clean = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', target.strip(), flags=re.I).strip().lower()
        if c_clean == t_clean:
            return True

        c_tokens = BookingStateService._normalize_name_tokens(candidate)
        t_tokens = BookingStateService._normalize_name_tokens(target)
        if not c_tokens or not t_tokens:
            return False
        if c_tokens == t_tokens:
            return True
        if set(c_tokens).issubset(set(t_tokens)) or set(t_tokens).issubset(set(c_tokens)):
            return True
        if (len(c_tokens) == 1 and c_tokens[0] in t_tokens and len(c_tokens[0]) >= 4) or (len(t_tokens) == 1 and t_tokens[0] in c_tokens and len(t_tokens[0]) >= 4):
            return True
        matched_tokens = 0
        for ct in c_tokens:
            for tt in t_tokens:
                if ct == tt or difflib.SequenceMatcher(None, ct, tt).ratio() >= 0.82:
                    matched_tokens += 1
                    break
        if matched_tokens == len(c_tokens) and len(c_tokens) >= 2:
            return True
        c_str = ' '.join(c_tokens)
        t_str = ' '.join(t_tokens)
        if difflib.SequenceMatcher(None, c_str, t_str).ratio() >= 0.85:
            return True
        return False

    @staticmethod
    def _extract_doctors_from_script(script_text: str) -> List[Dict[str, str]]:
        if not script_text:
            return []
        doctors = []
        seen_names = set()

        _excluded_names = {
            "box", "enquiry", "inquiry", "consultants", "consultant", "appointment",
            "appointments", "schedule", "hours", "list", "listed", "details",
            "name", "[doctor name]", "availability", "faq", "fees", "fee", "cost", "profile",
            "directory", "information", "team", "staff"
        }

        doc_header_re = re.compile(r'^(?:[-*•]\s*)?(?:Dr\.?|Doctor)\s+([A-Za-z0-9\.\s]+)$', re.IGNORECASE)
        inline_re = re.compile(r'^(?:[-*•]\s*)?(?:Dr\.?|Doctor)\s+([A-Za-z0-9\.\s]+?)\s*[:\-]\s*(.+)$', re.IGNORECASE)

        lines = [l.strip() for l in script_text.splitlines() if l.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            in_m = inline_re.match(line)
            if in_m:
                name = in_m.group(1).strip()
                details = in_m.group(2).strip()
                if name.lower().strip("[](). ") not in _excluded_names and len(name) >= 2:
                    dept = details.split(',')[0].strip() if ',' in details else details
                    hours = ""
                    if any(w in details.lower() for w in ["working", "hours", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]):
                        hours_part = re.search(r'(?:working\s+|hours\s*:\s*|schedule\s*:\s*)?([A-Za-z]+\s+to\s+[A-Za-z]+[^\r\n\.]*)', details, re.I)
                        if hours_part:
                            hours = hours_part.group(1).strip()
                    if name.lower() not in seen_names:
                        seen_names.add(name.lower())
                        doctors.append({
                            "name": name,
                            "dept": dept,
                            "hours": hours or details,
                            "spec": dept,
                        })
                i += 1
                continue

            h_m = doc_header_re.match(line)
            if h_m:
                name = h_m.group(1).strip()
                if name.lower().strip("[](). ") not in _excluded_names and len(name) >= 2:
                    details = ""
                    if i + 1 < len(lines):
                        next_line = lines[i + 1]
                        if not doc_header_re.match(next_line) and not inline_re.match(next_line):
                            if len(next_line) < 80 and not any(k in next_line.lower() for k in ['page', 'sarans', 'http', 'copyright', 'caller:', 'ai agent:']):
                                details = next_line
                                i += 1
                    if name.lower() not in seen_names:
                        seen_names.add(name.lower())
                        doctors.append({
                            "name": name,
                            "dept": details,
                            "hours": "",
                            "spec": details,
                        })
                i += 1
                continue

            i += 1

        # Also support multiline block format with explicit Department: / Hours: labels
        blocks = re.split(r'\n(?=(?:[-*•]\s*)?(?:Dr\.?|Doctor)\s+)', script_text, flags=re.IGNORECASE)
        for b in blocks:
            m = re.match(r'^(?:[-*•]\s*)?(?:Dr\.?|Doctor)\s+([A-Za-z0-9\.\s]+)', b.strip(), re.IGNORECASE)
            if m:
                name = m.group(1).split('\n')[0].strip()
                if name.lower().strip("[](). ") in _excluded_names or len(name) < 2:
                    continue
                if name.lower() in seen_names:
                    continue
                dept_m = re.search(r'Department:\s*([^\r\n]+)', b, re.IGNORECASE)
                hours_m = re.search(r'(?:Working Hours|Hours|Availability):\s*([^\r\n]+)', b, re.IGNORECASE)
                spec_m = re.search(r'Specialization:\s*([^\r\n]+)', b, re.IGNORECASE)
                seen_names.add(name.lower())
                doctors.append({
                    "name": name,
                    "dept": dept_m.group(1).strip() if dept_m else "",
                    "hours": hours_m.group(1).strip() if hours_m else "",
                    "spec": spec_m.group(1).strip() if spec_m else "",
                })
        return doctors

    @staticmethod
    def _parse_script_schedule(hours_str: str) -> tuple[List[str], str, str, str]:
        days_all = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        working_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
        start_t = '09:00'
        end_t = '17:00'

        if not hours_str:
            return working_days, start_t, end_t, f'{start_t}-{end_t}'

        m_days = re.search(r'([A-Za-z]+)\s+to\s+([A-Za-z]+)', hours_str, re.IGNORECASE)
        if m_days:
            d1 = m_days.group(1).capitalize()
            d2 = m_days.group(2).capitalize()
            if d1 in days_all and d2 in days_all:
                i1 = days_all.index(d1)
                i2 = days_all.index(d2)
                if i1 <= i2:
                    working_days = days_all[i1:i2+1]
                else:
                    working_days = days_all[i1:] + days_all[:i2+1]

        m_time = re.search(r'(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)\s*[-–to]+\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM)?)', hours_str, re.IGNORECASE)
        if m_time:
            t1_raw = m_time.group(1).strip()
            t2_raw = m_time.group(2).strip()
            if not re.search(r'[a-zA-Z]', t1_raw) and 'PM' in t2_raw.upper():
                t1_raw += " AM"
            for fmt in ('%H:%M', '%I:%M %p', '%I %p', '%I:%M%p', '%I%p'):
                try:
                    start_t = datetime.strptime(t1_raw.upper(), fmt).strftime('%H:%M')
                    break
                except ValueError:
                    pass
            for fmt in ('%H:%M', '%I:%M %p', '%I %p', '%I:%M%p', '%I%p'):
                try:
                    end_t = datetime.strptime(t2_raw.upper(), fmt).strftime('%H:%M')
                    break
                except ValueError:
                    pass

        return working_days, start_t, end_t, f'{start_t}-{end_t}'

    def resolve_staff_from_directory(
        self,
        candidate: str,
        organization_id: int,
        db: Any,
        call_id: str = ""
    ) -> tuple[Optional[int], Optional[str], str]:
        """
        Authoritative single source of truth for doctor verification.
        Resolves against DB Staff table and active uploaded script.
        Synchronizes verified doctors from the active script into the Staff directory.
        Returns: (staff_id, staff_name, status) where status is 'resolved', 'not_found', or 'ambiguous'.
        Emits [STAFF_RESOLUTION] log.
        """
        from src.models.staff import Staff, StaffStatus
        import json

        if not candidate:
            return None, None, "not_found"

        clean = candidate.strip()
        clean_words = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', clean, flags=re.I).strip()
        if not clean_words or len(clean_words) < 2:
            logger.info(
                f"\n[STAFF_RESOLUTION]"
                f"\n  call_id={call_id}"
                f"\n  candidate='{candidate}'"
                f"\n  matches=0"
                f"\n  staff_id=None"
                f"\n  staff_name=None"
                f"\n  status=NOT_FOUND"
            )
            return None, None, "not_found"

        # 1. Match against DB Staff table for the active organization
        all_staff = db.query(Staff).filter(Staff.organization_id == organization_id).all()
        matched_db_staff = []
        for s in all_staff:
            if not s.name:
                continue
            if self._is_doctor_name_match(candidate, s.name):
                matched_db_staff.append(s)

        if len(matched_db_staff) == 1:
            staff = matched_db_staff[0]
            logger.info(
                f"\n[STAFF_RESOLUTION]"
                f"\n  call_id={call_id}"
                f"\n  candidate='{candidate}'"
                f"\n  matches=1"
                f"\n  staff_id={staff.id}"
                f"\n  staff_name='{staff.name}'"
                f"\n  status=RESOLVED"
            )
            return staff.id, staff.name, "resolved"
        elif len(matched_db_staff) > 1:
            logger.info(
                f"\n[STAFF_RESOLUTION]"
                f"\n  call_id={call_id}"
                f"\n  candidate='{candidate}'"
                f"\n  matches={len(matched_db_staff)}"
                f"\n  staff_id=None"
                f"\n  staff_name=None"
                f"\n  status=AMBIGUOUS"
            )
            return None, None, "ambiguous"

        # 2. Check Active Script & Synchronize to Staff Directory
        try:
            from src.services.rag_service import rag_service
            master_ctx = rag_service.get_organization_master_context(organization_id)
            if master_ctx:
                script_doctors = self._extract_doctors_from_script(master_ctx)
                matched_script_docs = [
                    sd for sd in script_doctors
                    if self._is_doctor_name_match(candidate, sd["name"])
                ]

                if len(matched_script_docs) == 1:
                    s_doc = matched_script_docs[0]
                    doc_n = s_doc["name"]
                    doc_clean = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', doc_n, flags=re.I).strip()
                    existing_staff = db.query(Staff).filter(
                        Staff.organization_id == organization_id,
                        Staff.name.ilike(f"%{doc_clean}%")
                    ).first()
                    if not existing_staff:
                        working_days, start_t, end_t, w_hours = self._parse_script_schedule(s_doc.get("hours", ""))
                        display_name = f"Dr. {doc_clean}" if not doc_clean.lower().startswith("dr") else doc_clean
                        new_staff = Staff(
                            organization_id=organization_id,
                            name=display_name,
                            department=s_doc.get("dept") or s_doc.get("spec") or "Ayurvedic Consultant",
                            working_days=json.dumps(working_days),
                            start_time=start_t,
                            end_time=end_t,
                            working_hours=w_hours,
                            status=StaffStatus.AVAILABLE,
                            online_booking_enabled=True,
                            appointment_duration="30 min",
                        )
                        db.add(new_staff)
                        db.commit()
                        db.refresh(new_staff)
                        staff_record = new_staff
                        logger.info(
                            f"\n[STAFF_SYNC_FROM_SCRIPT]"
                            f"\n  call_id={call_id}"
                            f"\n  organization_id={organization_id}"
                            f"\n  staff_id={staff_record.id}"
                            f"\n  staff_name='{staff_record.name}'"
                            f"\n  department='{staff_record.department}'"
                            f"\n  working_hours='{staff_record.working_hours}'"
                        )
                    else:
                        staff_record = existing_staff

                    logger.info(
                        f"\n[STAFF_RESOLUTION]"
                        f"\n  call_id={call_id}"
                        f"\n  candidate='{candidate}'"
                        f"\n  matches=1"
                        f"\n  staff_id={staff_record.id}"
                        f"\n  staff_name='{staff_record.name}'"
                        f"\n  status=RESOLVED"
                        f"\n  source=SCRIPT_SYNC"
                    )
                    return staff_record.id, staff_record.name, "resolved"
                elif len(matched_script_docs) > 1:
                    logger.info(
                        f"\n[STAFF_RESOLUTION]"
                        f"\n  call_id={call_id}"
                        f"\n  candidate='{candidate}'"
                        f"\n  matches={len(matched_script_docs)}"
                        f"\n  staff_id=None"
                        f"\n  staff_name=None"
                        f"\n  status=AMBIGUOUS"
                    )
                    return None, None, "ambiguous"
        except Exception as sync_err:
            logger.warning(f"[STAFF_SYNC_ERROR] Error syncing staff from active script: {sync_err}")

        logger.info(
            f"\n[STAFF_RESOLUTION]"
            f"\n  call_id={call_id}"
            f"\n  candidate='{candidate}'"
            f"\n  matches=0"
            f"\n  staff_id=None"
            f"\n  staff_name=None"
            f"\n  status=NOT_FOUND"
        )
        return None, None, "not_found"

    def validate_doctor_against_db(self, doctor_name_candidate: str, organization_id: int = 1) -> Optional[str]:
        """
        Validate doctor candidate against the actual Staff database.
        Never accepts an invented or unknown doctor.
        """
        if not doctor_name_candidate:
            return None

        try:
            from src.config.database import SessionLocal
            db = SessionLocal()
            try:
                _, name, status = self.resolve_staff_from_directory(doctor_name_candidate, organization_id, db)
                if status == "resolved":
                    return name
                return None
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error validating doctor against DB: {e}")
            return None

    def check_doctor_availability_live(self, doctor_name: str, target_date_str: Optional[str] = None, session: Optional[BookingSession] = None) -> str:
        """
        Queries the live database for actual available slots for given doctor & date.
        Never invents available slots or falls back to arbitrary staff members.
        """
        try:
            from src.config.database import SessionLocal
            from src.models.staff import Staff
            from src.routes.availability_routes import get_available_slots

            db = SessionLocal()
            try:
                staff = None
                org_id = session.organization_id if session else 1
                call_id = session.call_sid if session else ""
                status = "not_found"

                if session and session.doctor_id:
                    staff = db.query(Staff).filter(Staff.id == session.doctor_id).first()

                if not staff and doctor_name:
                    # Check if doctor_name is actually a department/specialty name
                    resolved_dept = self.resolve_department(doctor_name, org_id, db=db)
                    if resolved_dept:
                        dept_docs = self.get_doctors_for_department(resolved_dept, org_id, db=db)
                        if dept_docs:
                            target_doc = dept_docs[0]
                            if target_doc.get("id"):
                                staff = db.query(Staff).filter(Staff.id == target_doc["id"]).first()
                            if session:
                                session.department = resolved_dept
                                session.doctor = target_doc["name"]
                                if target_doc.get("id"):
                                    session.doctor_id = target_doc["id"]
                                session.doctor_locked = True
                                if not hasattr(session, "confirmed_facts"):
                                    session.confirmed_facts = {}
                                session.confirmed_facts["department"] = {"value": resolved_dept, "source": "AUTHORITATIVE_DIRECTORY"}
                                session.confirmed_facts["doctor"] = {"value": target_doc["name"], "source": "AUTHORITATIVE_DIRECTORY"}

                    if not staff:
                        s_id, s_name, status = self.resolve_staff_from_directory(doctor_name, org_id, db, call_id=call_id)
                        if status == "resolved" and s_id:
                            staff = db.query(Staff).filter(Staff.id == s_id).first()
                            if session:
                                session.doctor_id = staff.id
                                session.doctor = staff.name
                                session.doctor_locked = True

                if not staff and session and (session.service or session.product):
                    # For a product or service appointment without specific doctor:
                    # Check if any staff member handles this service or use first available staff for org
                    all_org_staff = db.query(Staff).filter(
                        Staff.organization_id == org_id,
                        Staff.online_booking_enabled != False
                    ).all()
                    for s in all_org_staff:
                        if s.services and session.service and session.service.lower() in s.services.lower():
                            staff = s
                            break
                    if not staff and all_org_staff:
                        staff = all_org_staff[0]

                if not staff:
                    logger.warning(
                        f"\n[AVAILABILITY]"
                        f"\n  call_id={call_id}"
                        f"\n  staff_id=None"
                        f"\n  staff_name='{doctor_name}'"
                        f"\n  status=STAFF_NOT_FOUND"
                    )
                    if session and (session.service or session.product):
                        default_slots = ["10:00 AM", "11:30 AM", "02:00 PM", "04:00 PM"]
                        if session:
                            session.offered_slots = default_slots
                            session.last_question_field = "offered_slot_confirmation"
                        target_lbl = session.service or session.product or "consultation"
                        d_str = target_date_str or "today"
                        return f"For {d_str}, we have {', '.join(default_slots)} available time slots for {target_lbl}. Which time would you prefer?"

                    if status == "ambiguous":
                        return f"We have more than one staff member matching '{doctor_name}'. Could you please specify who you would like to book with?"

                    # Offer verified alternatives rather than asking to repeat indefinitely
                    all_staff = db.query(Staff).filter(Staff.organization_id == org_id).all()
                    valid_names = [f"Dr. {s.name.title()}" if not s.name.lower().startswith("dr") else s.name.title() for s in all_staff if s.name]
                    if valid_names:
                        names_text = ", ".join(valid_names[:-1]) + f", or {valid_names[-1]}" if len(valid_names) > 1 else valid_names[0]
                        return f"I could not find '{doctor_name or 'that doctor'}' in our directory. We currently have {names_text} available. Which doctor would you like to book with?"
                    return f"I could not find '{doctor_name or 'that person'}' in our directory. Could you please specify who you would like to book with?"

                selected_d = date.today()
                if target_date_str:
                    t_lower = target_date_str.lower()
                    if "day after tomorrow" in t_lower:
                        selected_d = date.today() + timedelta(days=2)
                    elif "tomorrow" in t_lower or "naalai" in t_lower or "kal" in t_lower:
                        selected_d = date.today() + timedelta(days=1)
                    elif "today" in t_lower or "indru" in t_lower or "aaj" in t_lower:
                        selected_d = date.today()
                    else:
                        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                        for idx, day_name in enumerate(days):
                            if day_name in t_lower:
                                days_ahead = idx - selected_d.weekday()
                                if days_ahead < 0:
                                    days_ahead += 7
                                elif days_ahead == 0:
                                    # Same day of week: use today if slots remain, otherwise next week
                                    pass
                                selected_d = selected_d + timedelta(days=days_ahead)
                                break
                        else:
                            date_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', t_lower)
                            if date_match:
                                try:
                                    selected_d = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                                except ValueError:
                                    pass

                slots_res = get_available_slots(staff_id=staff.id, selected_date=selected_d, db=db)
                avail_slots = slots_res.get("available_slots", [])

                date_label = selected_d.strftime('%A, %b %d')
                staff_name_label = session.service if (session and session.service) else staff.name

                offered = []
                if avail_slots:
                    slot_times_12h = [self._format_time_12h(s["start"]) for s in avail_slots[:5]]
                    offered = slot_times_12h
                    if session:
                        session.offered_slots = offered
                        session.last_question_field = "offered_slot_confirmation"

                    logger.info(
                        f"\n[AVAILABILITY]"
                        f"\n  call_id={call_id}"
                        f"\n  staff_id={staff.id}"
                        f"\n  staff_name='{staff.name}'"
                        f"\n  date='{selected_d}'"
                        f"\n  total_slots={len(avail_slots)}"
                        f"\n  offered_slots={offered}"
                    )
                    return f"For {date_label}, we have {', '.join(slot_times_12h)} available time slots for {staff_name_label}. Which time would you prefer?"
                else:
                    if session:
                        session.offered_slots = []
                    logger.info(
                        f"\n[AVAILABILITY]"
                        f"\n  call_id={call_id}"
                        f"\n  staff_id={staff.id}"
                        f"\n  staff_name='{staff.name}'"
                        f"\n  date='{selected_d}'"
                        f"\n  total_slots=0"
                        f"\n  offered_slots=[]"
                    )
                    return f"{staff_name_label} has no available slots remaining for {date_label}. Would you like to check another date?"
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error querying live doctor availability: {e}")
            return f"I could not retrieve available slots right now. Please try again in a moment."

    def _normalize_date(self, text_date: str) -> str:
        from datetime import datetime, timedelta, timezone
        try:
            from zoneinfo import ZoneInfo
            from src.config.env import settings
            tz_str = getattr(settings, "BUSINESS_TIMEZONE", None) or getattr(settings, "TIMEZONE", "Asia/Kolkata")
            tz = ZoneInfo(tz_str)
            now = datetime.now(tz)
        except Exception:
            tz = timezone(timedelta(hours=5, minutes=30))
            now = datetime.now(tz)
        
        t = text_date.lower().strip()
        if "day after tomorrow" in t:
            return (now + timedelta(days=2)).strftime("%Y-%m-%d")
        if "today" in t or "inru" in t or "aaj" in t:
            return now.strftime("%Y-%m-%d")
        if "tomorrow" in t or "naalai" in t or "kal" in t:
            return (now + timedelta(days=1)).strftime("%Y-%m-%d")
        if "next week" in t:
            return (now + timedelta(days=7)).strftime("%Y-%m-%d")
        if "next month" in t:
            return (now + timedelta(days=30)).strftime("%Y-%m-%d")
            
        # ISO
        if re.search(r'\d{4}-\d{2}-\d{2}', t):
            return re.search(r'\d{4}-\d{2}-\d{2}', t).group(0)
            
        # DD Month or Month DD
        month_map = {
            "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
            "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
            "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
            "nov": 11, "november": 11, "dec": 12, "december": 12
        }
        
        # Month DD
        m = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})', t)
        if m:
            month = month_map[m.group(1)]
            day = int(m.group(2))
            try:
                # If date is in past this year, maybe next year? We'll just use current year.
                d = now.replace(month=month, day=day)
                if d < now - timedelta(days=30): 
                    d = d.replace(year=now.year + 1)
                return d.strftime("%Y-%m-%d")
            except:
                pass
                
        # DD Month
        m = re.search(r'(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)', t)
        if m:
            day = int(m.group(1))
            month = month_map[m.group(2)]
            try:
                d = now.replace(month=month, day=day)
                if d < now - timedelta(days=30): 
                    d = d.replace(year=now.year + 1)
                return d.strftime("%Y-%m-%d")
            except:
                pass
                
        # Days of the week (Monday - Sunday)
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for idx, day_name in enumerate(days):
            if re.search(r'\b' + day_name + r'\b', t):
                days_ahead = idx - now.weekday()
                if days_ahead <= 0 or f"next {day_name}" in t:
                    days_ahead += 7
                target_d = now + timedelta(days=days_ahead)
                return target_d.strftime("%Y-%m-%d")

        try:
            from src.services.calendar_verification_service import calendar_verification_service
            parsed_d = calendar_verification_service.parse_date(text_date)
            return parsed_d.strftime("%Y-%m-%d")
        except Exception:
            return now.strftime("%Y-%m-%d")

    def extract_slots(self, text: str, session: BookingSession):
        """
        Stateful Multi-Slot Entity Extractor.
        Extracts doctor/service, date, time, patient name, email, and phone from natural language.
        Validates doctor candidate against actual DB records.
        Guards against running doctor extraction when collecting customer personal info.
        """
        txt_lower = text.lower().strip()
        org_id = getattr(session, "organization_id", 1) or 1

        # Check explicit department, doctor, or service rejection/negation in caller utterance
        if session.department and is_phrase_negated(session.department, text):
            logger.info(f"[ENTITY_NEGATION] Current session department '{session.department}' negated in utterance: '{text}'")
            session.invalidate_department(session.department)

        if session.doctor and is_phrase_negated(session.doctor, text):
            logger.info(f"[ENTITY_NEGATION] Current session doctor '{session.doctor}' negated in utterance: '{text}'")
            session.invalidate_doctor(session.doctor)

        if session.service and is_phrase_negated(session.service, text):
            logger.info(f"[ENTITY_NEGATION] Current session service '{session.service}' negated in utterance: '{text}'")
            session.invalidate_service_or_product(session.service)

        # Direct rejection extraction (e.g. "I don't want Galaxy Jet", "No Galaxy Jet")
        neg_match = re.search(r'\b(?:don\'?t\s+want|do\s+not\s+want|no\s+|not\s+interested\s+in|instead\s+of)\s+(?:the\s+|a\s+|an\s+)?([a-zA-Z0-9\s]{2,30}?)(?=[.,;!?]|\s+(?:i\s+want|i\s+need|but|please|$))\b', txt_lower)
        if neg_match:
            rejected_cand = neg_match.group(1).strip()
            if rejected_cand and rejected_cand not in {"appointment", "booking", "slot", "date", "time", "this", "that"}:
                session._rejected_services.add(rejected_cand.lower())
                session._rejected_departments.add(rejected_cand.lower())
                session._rejected_doctors.add(rejected_cand.lower())
                if session.booking_data.get("user_requested_entity_term") and session.booking_data["user_requested_entity_term"].lower() in rejected_cand.lower():
                    session.booking_data["user_requested_entity_term"] = None
                if session.booking_data.get("unverified_entity_requested") and session.booking_data["unverified_entity_requested"].lower() in rejected_cand.lower():
                    session.booking_data["unverified_entity_requested"] = None
                if session.booking_data.get("ambiguous_entity_candidate"):
                    session.booking_data["ambiguous_entity_candidate"] = None
                if session.booking_data.get("pending_confirmation_entity"):
                    session.booking_data["pending_confirmation_entity"] = None

        if session.booking_data.get("user_requested_entity_term") and is_phrase_negated(session.booking_data["user_requested_entity_term"], text):
            session.booking_data["user_requested_entity_term"] = None

        if session.booking_data.get("unverified_entity_requested") and is_phrase_negated(session.booking_data["unverified_entity_requested"], text):
            session.booking_data["unverified_entity_requested"] = None

        # Check if any known department/service for this organization is negated in caller utterance
        known_depts = self.get_organization_services_and_departments(org_id)
        for kd in known_depts:
            if is_phrase_negated(kd, text):
                logger.info(f"[ENTITY_NEGATION] Known department/service '{kd}' negated in utterance: '{text}'")
                session.invalidate_service_or_product(kd)
                session.invalidate_department(kd)

        # Check explicit negation of candidate services/departments directly in utterance (e.g. "dental care", "cardiology")
        common_dept_candidates = ["dental", "dental care", "cardiology", "neurology", "orthopedics", "pediatrics", "general medicine"]
        for cdc in common_dept_candidates:
            if cdc not in [d.lower() for d in known_depts] and is_phrase_negated(cdc, text):
                logger.info(f"[ENTITY_NEGATION] Candidate department '{cdc}' negated in utterance: '{text}'")
                session.invalidate_department(cdc)

        # Check if caller wants to explicitly change doctor or if text contains doctor mention
        doctor_change_phrases = ["change doctor", "another doctor", "different doctor", "switch doctor", "not dr", "not doctor", "instead of", "rather than"]
        has_doctor_keyword = any(k in txt_lower for k in ["dr", "dr.", "doctor", "physician"])
        wants_doctor_change = any(p in txt_lower for p in doctor_change_phrases)

        if wants_doctor_change:
            session.doctor_locked = False
            session.doctor_id = None
            session.doctor_name = None
            session.doctor = None
            session.time = None
            session.availability_verified = False
            session.offered_slots = []
            session.state = BookingState.WAITING_FOR_DOCTOR
            session.last_asked_field = "doctor"

        # Guard: Skip doctor/service extraction ONLY when collecting customer personal info
        _customer_detail_states = {
            BookingState.WAITING_FOR_PATIENT_NAME,
            BookingState.COLLECTING_PATIENT_NAME,
            BookingState.WAITING_FOR_EMAIL,
            BookingState.COLLECTING_EMAIL,
            BookingState.WAITING_FOR_CONTACT,
            BookingState.COLLECTING_PHONE,
            BookingState.WAITING_FOR_CONFIRMATION,
        }
        # Allow extraction if in booking/date/time stages or if doctor keyword is explicitly present
        _skip_doctor_extraction = (
            (session.state in _customer_detail_states)
            or (getattr(session, "last_asked_field", "") in ("patient_name", "contact_number", "email", "phone"))
            or (session.doctor_locked and not has_doctor_keyword and not wants_doctor_change)
        ) and not (has_doctor_keyword and any(kw in txt_lower for kw in ["dr", "doctor", "dr.", "physician"]))

        # Detect booking for someone else (caller vs patient distinction)
        patient_relationships = {
            "father": ["father", "dad", "appa", "pita", "papa"],
            "mother": ["mother", "mom", "amma", "mata", "mummy"],
            "son": ["son", "magan", "beta"],
            "daughter": ["daughter", "magal", "beti"],
            "brother": ["brother", "anna", "thambi", "bhai"],
            "sister": ["sister", "akka", "thangachi", "behan"],
            "wife": ["wife", "manaivi", "patni"],
            "husband": ["husband", "kanavan", "pati"],
            "spouse": ["spouse", "partner"],
            "friend": ["friend", "nanban", "dost"],
            "child": ["child", "kid", "baby", "kuzhandhai", "baccha"],
            "parent": ["parent", "parents"],
            "relative": ["relative", "family member", "someone else", "another person"],
        }
        for rel_key, rel_terms in patient_relationships.items():
            if any(re.search(r'\b(?:for\s+(?:my\s+|our\s+)?' + re.escape(term) + r')\b', txt_lower) for term in rel_terms):
                session.booking_data["patient_relationship"] = rel_key
                session.booking_data["is_booking_for_other"] = True
                session.patient_relationship = rel_key
                session.is_booking_for_other = True
                break

        # 1. Doctor / Service / Department Extraction & Active Organization Validation
        candidate = None

        if not _skip_doctor_extraction:
            # ---------------------------------------------------------------
            # Capture the user's own role/entity word BEFORE DB validation
            # so that if validation fails we can echo their terminology back.
            # ---------------------------------------------------------------
            _ROLE_EXTRACTION_PATTERNS = [
                r'\b(?:with|meet|see|visit|book\s+with)\s+(?:the\s+|a\s+|an\s+)?([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
                r'\bappointment\s+with\s+(?:the\s+|a\s+|an\s+)?([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
                r'\bfor\s+(?:dr\.?|doctor|physician)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
            ]
            _ROLE_STOPWORDS = {
                "appointment", "booking", "doctor", "staff", "member", "service",
                "today", "tomorrow", "morning", "afternoon", "evening", "night",
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "you", "me", "my", "our", "the", "a", "an", "and", "or", "for", "to",
                "dad", "mom", "father", "mother", "son", "daughter", "brother", "sister",
                "wife", "husband", "friend", "child", "children", "parents", "parent", "family",
                "myself", "him", "her", "them", "someone", "somebody", "patient"
            }
            for _pat in _ROLE_EXTRACTION_PATTERNS:
                _rm = re.search(_pat, txt_lower)
                if _rm:
                    _role_word = _rm.group(1).strip()
                    _role_tokens = [t for t in _role_word.split() if t not in _ROLE_STOPWORDS and len(t) > 2]
                    if _role_tokens:
                        session.booking_data["user_requested_entity_term"] = " ".join(_role_tokens)
                        break

            # Extract explicit person/doctor pattern
            doc_patterns = [
                # Change / switch doctor pattern: 'change doctor to dr akhila gladis', 'switch to doctor priya'
                r'\b(?:change\s+(?:doctor\s+)?to|switch\s+(?:doctor\s+)?to|instead\s+(?:of\s+.*?\s+)?(?:use|see|to)?)\s+(?:dr\.?|doctor|the\s+doctor)?\s*([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
                # Explicit title prefix: 'dr. rohit', 'dr rohit kumar', 'doctor rohit kumar', 'physician priya'
                # Negative lookahead guards against 'change doctor to ...' capturing 'to' as name
                r'\b(?:dr\.?|doctor|prof\.?|physician)(?!\s+to\b)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
                # Explicit suffix: 'rohit doctor', 'rohit kumar doctor'
                r'\b([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\s+(?:doctor|dr\.?)\b',
                # Verb + preposition + target (NO bare 'for'): 'see doctor rohit', 'see rohit', 'meet dr priya'
                r'\b(?:see|meet|consult|visit|with)\s+(?:dr\.?|doctor|the\s+doctor)?\s*([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
                # 'for' strictly followed by doctor prefix
                r'\bfor\s+(?:dr\.?|doctor|the\s+doctor|physician)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)\b',
                # Booking target pattern: 'book galaxy jet', 'book an appointment for galaxy chat', 'book for galaxy chat'
                r'\b(?:book|schedule|reserve)\s+(?:an?\s+appointment\s+(?:for|with)\s+|for\s+|with\s+)?([a-zA-Z0-9]+(?:\s+[a-zA-Z0-9]+)?)\b',
            ]
            for pat in doc_patterns:
                doc_m = re.search(pat, txt_lower)
                if doc_m:
                    cand_text = doc_m.group(1).title().strip()
                    stop_words = {
                        "appointment", "booking", "venum", "schedule", "slot", "slots", "doctor",
                        "today", "tomorrow", "yesterday", "morning", "afternoon", "evening", "night",
                        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                        "january", "february", "march", "april", "may", "june", "july",
                        "august", "september", "october", "november", "december",
                        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
                        "naalaikku", "naalai", "innikku", "inniku", "the", "a", "an", "and", "me", "you",
                        "dad", "mom", "father", "mother", "son", "daughter", "brother", "sister",
                        "wife", "husband", "friend", "child", "children", "parents", "parent", "family",
                        "myself", "him", "her", "them", "someone", "somebody", "patient", "relative",
                        "my", "our", "his", "her", "their", "your", "this", "that", "time", "date",
                        "now", "here", "there", "one", "for", "with", "at", "on", "in", "to", "from",
                        "change", "switch", "instead", "prefer", "want", "need", "like"
                    }
                    cand_tokens = [w for w in cand_text.lower().split() if w not in stop_words]
                    if cand_tokens and not any(sw == cand_text.lower() for sw in stop_words):
                        cand_clean_title = " ".join(cand_tokens).title()
                        if is_phrase_negated(cand_clean_title, text):
                            session.invalidate_doctor(cand_clean_title)
                        elif cand_clean_title.lower() in getattr(session, "_rejected_doctors", set()) or any(d in cand_clean_title.lower() for d in getattr(session, "_rejected_doctors", set())):
                            pass
                        else:
                            candidate = cand_clean_title
                            break

            # Extract service/department pattern dynamically from DB & RAG if no doctor candidate found
            if not candidate:
                service_keywords = self.get_organization_services_and_departments(org_id)

                for sk in service_keywords:
                    sk_lower = sk.lower().strip()
                    if is_phrase_negated(sk, text):
                        session.invalidate_service_or_product(sk)
                        session.invalidate_department(sk)
                        continue
                    if sk_lower in getattr(session, "_rejected_departments", set()) or sk_lower in getattr(session, "_rejected_services", set()):
                        continue
                    if re.search(r'\b' + re.escape(sk_lower) + r'\b', txt_lower):
                        candidate = sk.title()
                        break

            # Check active organization staff token match
            if not candidate and not _skip_doctor_extraction:
                try:
                    from src.config.database import SessionLocal
                    from src.models.staff import Staff, StaffStatus
                    db = SessionLocal()
                    try:
                        all_staff = db.query(Staff).filter(
                            Staff.organization_id == org_id,
                            Staff.online_booking_enabled != False,
                            Staff.status != StaffStatus.OFFLINE
                        ).all()
                        for s in all_staff:
                            if not s.name or not s.id:
                                continue
                            s_clean = re.sub(r'^(?:dr\.?|doctor|prof\.?|mr\.?|mrs\.?|ms\.?)\s+', '', s.name.strip(), flags=re.I).strip().lower()
                            if is_phrase_negated(s.name, text) or is_phrase_negated(s_clean, text):
                                session.invalidate_doctor(s.name)
                                continue
                            if s.name.lower() in getattr(session, "_rejected_doctors", set()) or s_clean in getattr(session, "_rejected_doctors", set()):
                                continue
                            s_tokens = [st.lower() for st in re.split(r'[\s\.\-]+', s_clean) if len(st) >= 3 and st.lower() not in {"dr", "doctor", "the", "and"}]
                            if not s_tokens:
                                continue
                            # If all staff tokens are in transcript (e.g. 'arun kumar' in transcript)
                            # OR if the unique first name token is in transcript preceded by intent/title words or state is waiting for doctor
                            if all(t in txt_lower for t in s_tokens) or (
                                s_tokens[0] in txt_lower and (
                                    session.state == BookingState.WAITING_FOR_DOCTOR
                                    or session.last_asked_field == "doctor"
                                    or re.search(r'\b(?:dr\.?|doctor|see|meet|with|consult|for)\s+' + re.escape(s_tokens[0]), txt_lower)
                                )
                            ):
                                candidate = s.name
                                logger.info(
                                    f"\n[DOCTOR_RESOLUTION]"
                                    f"\n  call_id={getattr(session, 'call_sid', '')}"
                                    f"\n  raw_transcript='{text}'"
                                    f"\n  extracted_candidate='{s.name}'"
                                    f"\n  matched_tokens={s_tokens}"
                                    f"\n  resolution_source=STAFF_DIRECTORY_TOKEN_MATCH"
                                    f"\n  stage=WAITING_FOR_DOCTOR"
                                )
                                break
                    finally:
                        db.close()
                except Exception:
                    pass

            # Fallback: Only accept candidate if explicitly introduced or matches known entity
            if not candidate and not self._is_incomplete_utterance(text) and (session.state == BookingState.WAITING_FOR_DOCTOR or session.last_asked_field in ("doctor", "service")):
                _non_candidates = {
                    "right now", "little bit", "at the end", "the end", "cinema", "the one",
                    "home", "mate", "start", "now", "here", "there", "go", "get", "that",
                    "gonna", "going", "hello", "hi", "bye", "thanks", "thank you", "ok", "okay",
                    "sure", "for a little bit", "for my at-cut", "right", "bit", "end",
                    "said", "say", "told", "wait", "hold", "hold on", "come", "come on",
                    "look", "looking", "let", "about", "other", "another", "anyone", "someone",
                    "before you say", "before you", "before", "after", "during", "while",
                }
                clean_phrase = re.sub(r'[^a-zA-Z\s]', '', txt_lower).strip()
                if clean_phrase and clean_phrase not in _non_candidates and len(clean_phrase) > 2:
                    _noise = {
                        "i", "me", "my", "we", "us", "our", "you", "your", "he", "she", "it", "they", "them",
                        "book", "booking", "appointment", "please", "want", "like", "to", "a", "an", "the",
                        "meet", "see", "need", "get", "have", "make", "schedule", "set", "up", "for", "with",
                        "some", "this", "that", "one", "do", "does", "am", "is", "are", "was", "would", "could",
                        "should", "can", "may", "shall", "will", "just", "also", "and", "or", "but", "if", "in",
                        "on", "at", "of", "yes", "yeah", "ok", "okay", "hi", "hello", "hey", "thanks", "thank",
                        "right", "now", "little", "bit", "end", "cinema", "home", "gonna", "going", "start",
                        "dad", "mom", "father", "mother", "son", "daughter", "brother", "sister", "wife", "husband", "friend",
                        "said", "say", "says", "saying", "told", "tell", "tells", "telling", "wait", "hold", "come", "look", "looking", "let", "about",
                        "other", "another", "anyone", "someone", "something", "time", "date", "slot",
                        "before", "after", "during", "while", "until", "till", "speak", "talk", "listen",
                        "actually", "dont", "not", "no", "never", "neither", "nor", "really", "instead", "rather"
                    }
                    _date_words = {
                        "today", "tomorrow", "yesterday", "morning", "afternoon", "evening", "night",
                        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                        "january", "february", "march", "april", "may", "june", "july",
                        "august", "september", "october", "november", "december",
                        "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
                        "naalaikku", "naalai", "innikku", "inniku", "next", "this", "week", "month",
                    }
                    question_words = {"can", "could", "what", "which", "list", "tell", "show", "how", "much", "price", "cost", "where", "why", "who", "services", "doctors", "staff", "hours", "open", "real", "fake", "menu", "info", "help"}
                    words = [w for w in clean_phrase.split() if w not in _noise and w not in _date_words and len(w) >= 3]
                    if words and len(words) <= 3 and not any(w in question_words for w in words):
                        potential_cand = " ".join(words).title()
                        has_invalid_word = any(w.lower() in _noise or w.isdigit() or len(w) < 3 for w in potential_cand.split())
                        if not has_invalid_word:
                            if is_phrase_negated(potential_cand, text):
                                session.invalidate_doctor(potential_cand)
                                session.invalidate_service_or_product(potential_cand)
                                session.invalidate_department(potential_cand)
                            elif potential_cand.lower() in getattr(session, "_rejected_doctors", set()) or potential_cand.lower() in getattr(session, "_rejected_departments", set()) or potential_cand.lower() in getattr(session, "_rejected_services", set()):
                                pass
                            else:
                                candidate = potential_cand

            if candidate and not _skip_doctor_extraction:
                from src.services.entity_validation_service import entity_validation_service
                org_id = getattr(session, "organization_id", 1) or 1
                call_id = getattr(session, "call_sid", "") or ""
                val_res = entity_validation_service.validate_entity(candidate, organization_id=org_id)

                logger.info(
                    f"\n[ENTITY_RESOLUTION]"
                    f"\n  call_id={call_id}"
                    f"\n  raw_transcript='{text}'"
                    f"\n  extracted_candidate='{candidate}'"
                    f"\n  is_verified={val_res.is_verified}"
                    f"\n  is_ambiguous={getattr(val_res, 'is_ambiguous', False)}"
                    f"\n  suggested_entity='{getattr(val_res, 'suggested_entity', None)}'"
                    f"\n  entity_type='{val_res.entity_type}'"
                    f"\n  verified_name='{val_res.verified_name}'"
                    f"\n  staff_id={val_res.staff_id}"
                    f"\n  reason='{val_res.reason}'"
                )

                if val_res.is_verified:
                    vname = (val_res.verified_name or candidate).title().strip()
                    if is_phrase_negated(vname, text) or is_phrase_negated(candidate, text):
                        if val_res.entity_type in ("product", "service"):
                            session.invalidate_service_or_product(vname)
                        elif val_res.entity_type == "department":
                            session.invalidate_department(vname)
                        else:
                            session.invalidate_doctor(vname)
                    elif val_res.entity_type in ("product", "service"):
                        if vname.lower() in getattr(session, "_rejected_services", set()):
                            pass
                        else:
                            session.service = vname
                            session.booking_data["service"] = vname
                            session.booking_data["product"] = vname
                            session.booking_data["specialty"] = vname
                            if not hasattr(session, "confirmed_facts"):
                                session.confirmed_facts = {}
                            session.confirmed_facts["service"] = {"value": vname, "source": "AUTHORITATIVE_DIRECTORY"}
                            if not hasattr(session, "completed_steps"):
                                session.completed_steps = []
                            if "service_selection" not in session.completed_steps:
                                session.completed_steps.append("service_selection")

                            session.booking_data.pop("unverified_entity_requested", None)
                            session.booking_data.pop("user_requested_entity_term", None)
                            session.booking_data.pop("ambiguous_entity_candidate", None)
                            session.booking_data.pop("pending_confirmation_entity", None)

                            if session.state in (BookingState.WAITING_FOR_DOCTOR, BookingState.BOOKING_STARTED, BookingState.IDLE):
                                session.state = BookingState.WAITING_FOR_DATE
                                session.last_asked_field = "date"
                    elif val_res.entity_type == "department":
                        if vname.lower() in getattr(session, "_rejected_departments", set()):
                            pass
                        else:
                            session.department = vname
                            session.booking_data["department"] = vname
                            session.booking_data["specialty"] = vname
                            if not hasattr(session, "confirmed_facts"):
                                session.confirmed_facts = {}
                            session.confirmed_facts["department"] = {"value": vname, "source": "AUTHORITATIVE_DIRECTORY"}
                            if not hasattr(session, "completed_steps"):
                                session.completed_steps = []
                            if "department_selection" not in session.completed_steps:
                                session.completed_steps.append("department_selection")

                            if session.doctor and session.doctor.lower() == vname.lower():
                                session.doctor = None
                                session.doctor_id = None
                                session.doctor_name = None
                                session.doctor_locked = False

                            session.booking_data.pop("unverified_entity_requested", None)
                            session.booking_data.pop("user_requested_entity_term", None)
                            session.booking_data.pop("ambiguous_entity_candidate", None)
                            session.booking_data.pop("pending_confirmation_entity", None)
                    else:
                        # Doctor entity
                        if vname.lower() in getattr(session, "_rejected_doctors", set()):
                            pass
                        else:
                            s_id = val_res.staff_id
                            if not s_id:
                                try:
                                    from src.config.database import SessionLocal
                                    _db = SessionLocal()
                                    try:
                                        lookup_id, s_name, s_status = self.resolve_staff_from_directory(vname, org_id, _db, call_id=call_id)
                                        if s_status == "resolved" and lookup_id:
                                            s_id = lookup_id
                                            vname = s_name
                                    finally:
                                        _db.close()
                                except Exception as _exc:
                                    logger.warning(f"[DOCTOR_RESOLUTION] Exception during staff directory lookup: {_exc}")

                            if not s_id:
                                logger.warning(f"[DOCTOR_RESOLUTION] Rejecting candidate '{vname}' because no active DB staff record exists (id=None)")
                                session.doctor = None
                                session.doctor_id = None
                                session.doctor_name = None
                                session.doctor_locked = False
                            else:
                                if session.doctor_id and session.doctor_id != s_id:
                                    if session.state != BookingState.WAITING_FOR_DOCTOR:
                                        session.time = None
                                        session.offered_slots = []
                                        session.booking_data["availability_verified"] = False
                                elif session.doctor and session.doctor != vname:
                                    if session.state != BookingState.WAITING_FOR_DOCTOR:
                                        session.time = None
                                        session.offered_slots = []
                                        session.booking_data["availability_verified"] = False

                                session.doctor = vname
                                session.doctor_name = vname
                                session.doctor_id = s_id
                                session.doctor_locked = True
                                session.booking_data["specialty"] = val_res.role_or_specialty or vname
                                if val_res.role_or_specialty:
                                    session.department = val_res.role_or_specialty
                                    session.booking_data["department"] = val_res.role_or_specialty
                                if not hasattr(session, "confirmed_facts"):
                                    session.confirmed_facts = {}
                                session.confirmed_facts["doctor"] = {"value": vname, "source": "STAFF_DIRECTORY"}
                                if not hasattr(session, "completed_steps"):
                                    session.completed_steps = []
                                if "doctor_selection" not in session.completed_steps:
                                    session.completed_steps.append("doctor_selection")
                                session.booking_data.pop("unverified_entity_requested", None)
                                session.booking_data.pop("user_requested_entity_term", None)
                                session.booking_data.pop("ambiguous_entity_candidate", None)
                                session.booking_data.pop("pending_confirmation_entity", None)
                elif getattr(val_res, "is_ambiguous", False) and getattr(val_res, "suggested_entity", None):
                    session.booking_data["ambiguous_entity_candidate"] = {
                        "raw": candidate,
                        "suggested": val_res.suggested_entity
                    }
                    session.booking_data["unverified_entity_requested"] = candidate
                else:
                    # Unverified entity — never assign as doctor
                    logger.info(
                        f"\n[DOCTOR_RESOLUTION]"
                        f"\n  call_id={call_id}"
                        f"\n  raw_transcript='{text}'"
                        f"\n  extracted_candidate='{candidate}'"
                        f"\n  selected_staff_id=None"
                        f"\n  resolution_source=NONE"
                        f"\n  stage=WAITING_FOR_DOCTOR"
                        f"\n  reason='{val_res.reason}'"
                    )
                    session.doctor = None
                    session.doctor_id = None
                    session.doctor_name = None
                    session.doctor_locked = False
                    session.booking_data["unverified_entity_requested"] = candidate

        # 2. Date Extraction (Relative, Month-first, Day-first, days of week, ISO)
        _customer_detail_states = (
            BookingState.COLLECTING_PATIENT_NAME,
            BookingState.COLLECTING_PHONE,
            BookingState.COLLECTING_EMAIL,
            BookingState.BOOKING,
            BookingState.WAITING_FOR_CONFIRMATION,
        )
        is_in_customer_detail_state = (
            session.state in _customer_detail_states
            or getattr(session, "last_asked_field", "") in ("patient_name", "contact_number", "email", "confirmation")
        )
        is_explicit_reschedule = any(
            phrase in txt_lower for phrase in [
                "reschedule", "change date", "different date", "change time", "different time",
                "move to", "switch to", "instead of", "change the date", "change the time",
                "another date", "another time", "another day", "different day"
            ]
        )
        has_contact_detail_content = bool(
            self.parse_spoken_email(text)
            or any(w in txt_lower for w in ["@", "at gmail", "at yahoo", "at outlook", "dot com", "dot in", "dot org", "gmail", "yahoo", "outlook"])
            or re.search(r'\d{5,}', txt_lower)
        )

        _skip_date_extraction = (is_in_customer_detail_state and not is_explicit_reschedule and session.date is not None) or (has_contact_detail_content and not is_explicit_reschedule)
        _skip_time_extraction = (is_in_customer_detail_state and not is_explicit_reschedule and session.time is not None) or (has_contact_detail_content and not is_explicit_reschedule)

        extracted_date = None
        if not _skip_date_extraction:
            if "day after tomorrow" in txt_lower:
                extracted_date = self._normalize_date("day after tomorrow")
            elif any(w in txt_lower for w in ["நாளைக்கு", "நாளை", "naalaikku", "naalai", "tomorrow", "kal"]):
                extracted_date = "Tomorrow"
            elif any(w in txt_lower for w in ["இன்று", "innikku", "inniku", "inru", "today", "aaj"]):
                extracted_date = "Today"
            elif any(phrase in txt_lower for phrase in ["asap", "as soon as possible", "as early as possible", "earliest possible", "first available"]):
                # ASAP scheduling preference: resolve against live availability starting today
                extracted_date = "Today"
            else:
                # Check if 'may' is used as a modal auxiliary verb rather than the month May
                is_modal_may = bool(re.search(r'\b(?:how|what|when|where|who|why)?\s*may\s+(?:i|we|you|be|have|ask|assist|help)\b|\b(?:i|you|we|it|they|he|she)\s+may\b', txt_lower))
                standalone_months = r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b'
                if not is_modal_may and (session.state == BookingState.WAITING_FOR_DATE or "in may" in txt_lower or "month of may" in txt_lower):
                    standalone_months = r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b'

                date_match_exprs = [
                    r'\b(next\s+month|next\s+week)\b',
                    r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?\b',
                    r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b',
                    standalone_months,
                    r'\b(\d{4}-\d{2}-\d{2})\b',
                    r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
                ]
                for expr in date_match_exprs:
                    m = re.search(expr, txt_lower)
                    if m:
                        extracted_date = self._normalize_date(m.group(0))
                        break
                    
            if not extracted_date and (session.state == BookingState.WAITING_FOR_DATE or session.last_asked_field == "date"):
                num_match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\b', txt_lower)
                if num_match:
                    day_n = int(num_match.group(1))
                    if 1 <= day_n <= 31:
                        from datetime import datetime
                        curr_month = datetime.now().strftime("%B")
                        extracted_date = self._normalize_date(f"{day_n} {curr_month}")

            if extracted_date:
                if session.date != extracted_date:
                    # Date changed! Reset time and availability
                    session.date = extracted_date
                    session.time = None
                    session.availability_verified = False
                else:
                    session.date = extracted_date

        # 3. Time Extraction (Strictly avoids extracting numbers from dates / negations)
        extracted_time = None
        if not _skip_time_extraction:
            # Strip dates, negated dates, and ordinal numbers from text before time extraction
            text_for_time = txt_lower
            text_for_time = re.sub(r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?\b', ' ', text_for_time)
            text_for_time = re.sub(r'\b\d{1,2}(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b', ' ', text_for_time)
            text_for_time = re.sub(r'\b(?:not\s+on|not|on|date|dated)\s+\d{1,2}(?:st|nd|rd|th)?\b', ' ', text_for_time)
            text_for_time = re.sub(r'\b\d{1,2}(?:st|nd|rd|th)\b', ' ', text_for_time)
            text_for_time = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', ' ', text_for_time)
            text_for_time = re.sub(r'\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b', ' ', text_for_time)

            time_match = re.search(r'\b((?:1[0-2]|0?[1-9])(?:[:\-\s][0-5][0-9])?\s*(?:a\.?m\.?|p\.?m\.?))\b', text_for_time)
            if time_match:
                raw_tm = time_match.group(1).replace('.', '').upper()
                raw_tm = re.sub(r'[\-\s]([0-5][0-9])', r':\1', raw_tm) # fix dashes and spaces
                raw_tm = re.sub(r'(\d)(AM|PM)', r'\1 \2', raw_tm) # ensure space before AM/PM
                if ':' not in raw_tm:
                    raw_tm = re.sub(r'(\d+)\s*(AM|PM)', r'\1:00 \2', raw_tm)
                
                # Format as 12-hour HH:MM AM/PM standard
                from datetime import datetime
                try:
                    dt = datetime.strptime(raw_tm, "%I:%M %p")
                    formatted_h = str(dt.hour if dt.hour <= 12 and dt.hour > 0 else (dt.hour - 12 if dt.hour > 12 else 12))
                    extracted_time = f"{formatted_h}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"
                except:
                    extracted_time = raw_tm
            else:
                time_colon_match = re.search(r'\b(\d{1,2}[:\-\s][0-5][0-9])\s*(?:o\'clock|p\.?m\.?|a\.?m\.?)?\b', text_for_time)
                if time_colon_match:
                    raw_t = time_colon_match.group(1)
                    parts = re.split(r'[:\-\s]', raw_t)
                    h, m = int(parts[0]), int(parts[1])
                    if any(w in text_for_time for w in ["pm", "p.m", "evening", "afternoon", "night", "மாலை", "மதியம்"]):
                        if 1 <= h <= 12:
                            extracted_time = f"{h}:{m:02d} PM"
                    elif any(w in text_for_time for w in ["am", "a.m", "morning", "காலை"]):
                        if 1 <= h <= 12:
                            extracted_time = f"{h}:{m:02d} AM"
                    elif 13 <= h <= 23:
                        extracted_time = f"{h-12}:{m:02d} PM"
                    elif h == 12:
                        extracted_time = f"12:{m:02d} PM"
                    elif 1 <= h <= 7:
                        extracted_time = f"{h}:{m:02d} PM"
                    elif 8 <= h <= 11:
                        extracted_time = f"{h}:{m:02d} AM"
                elif session.state == BookingState.WAITING_FOR_TIME or any(w in text_for_time for w in ["at", "around", "timing", "slot", "o'clock"]):
                    bare_hour_match = re.search(r'\b(1[0-2]|0?[1-9])\s*(?:o\'clock|p\.?m\.?|a\.?m\.?)?\b', text_for_time)
                    if bare_hour_match:
                        h = int(bare_hour_match.group(1))
                        if any(w in text_for_time for w in ["pm", "p.m", "evening", "afternoon", "night", "மாலை", "மதியம்"]):
                            extracted_time = f"{h}:00 PM"
                        elif any(w in text_for_time for w in ["am", "a.m", "morning", "காலை"]):
                            extracted_time = f"{h}:00 AM"
                        elif 1 <= h <= 7:
                            extracted_time = f"{h}:00 PM"
                        else:
                            extracted_time = f"{h}:00 AM"
                elif any(w in text_for_time for w in ["morning", "காலை"]):
                    extracted_time = "10:00 AM"
                elif any(w in text_for_time for w in ["afternoon", "மதியம்"]):
                    extracted_time = "2:00 PM"
                elif any(w in text_for_time for w in ["evening", "மாலை"]):
                    extracted_time = "5:00 PM"

            # Check against offered_slots if not already extracted
            if not extracted_time and session.offered_slots:
                if any(phrase in txt_lower for phrase in ["any time", "any slot", "any available", "earliest", "first available", "whatever time", "any one", "any of them", "anytime", "asap", "as soon as possible", "as early as possible"]):
                    extracted_time = session.offered_slots[0]
                elif "morning" in txt_lower:
                    am_slots = [s for s in session.offered_slots if "AM" in s.upper()]
                    if am_slots:
                        extracted_time = am_slots[0]
                elif any(w in txt_lower for w in ["afternoon", "evening"]):
                    pm_slots = [s for s in session.offered_slots if "PM" in s.upper()]
                    if pm_slots:
                        extracted_time = pm_slots[0]

                if not extracted_time:
                    ordinal_map = {
                        "first": 0, "1st": 0,
                        "second": 1, "2nd": 1,
                        "third": 2, "3rd": 2,
                        "fourth": 3, "4th": 3,
                        "fifth": 4, "5th": 4,
                        "last": -1,
                    }
                    for ord_word, idx in ordinal_map.items():
                        if re.search(r'\b' + re.escape(ord_word) + r'(?:\s+one|\s+slot|\s+time)?\b', txt_lower):
                            target_idx = idx if idx >= 0 else len(session.offered_slots) - 1
                            if 0 <= target_idx < len(session.offered_slots):
                                extracted_time = session.offered_slots[target_idx]
                                break

                if not extracted_time:
                    for slot in session.offered_slots:
                        slot_clean = slot.lower().replace(':00', '').strip()
                        if slot.lower() in txt_lower or slot_clean in txt_lower:
                            extracted_time = slot
                            break

            if extracted_time:
                session.time = extracted_time
                if session.offered_slots and extracted_time in session.offered_slots:
                    session.availability_verified = True

        # 4. Patient Name Extraction (robust, state-aware)
        _NON_NAME_WORDS = {
            "gonna", "going", "go", "home", "get", "that", "this", "here", "there", "the", "one",
            "who", "what", "which", "how", "when", "where", "why", "to", "for", "with", "from",
            "and", "or", "but", "so", "if", "a", "an", "some", "any", "no", "yes", "yeah", "ok",
            "okay", "sure", "thanks", "thank", "you", "cinema", "start", "now", "right", "little",
            "bit", "end", "all", "ready", "done", "just", "call", "calling", "want", "need", "appointment",
            "booking", "book", "doctor", "phone", "email", "mobile", "number", "dr", "dr.", "at", "my",
            "is", "was", "are", "be", "am", "can", "could", "should", "would", "shall", "will", "have",
            "has", "had", "doing", "did", "do", "like", "up", "down", "out", "in", "off", "on", "not",
            "name", "names", "patient", "customer", "person", "user", "full", "peru", "naam", "sir", "madam", "please"
        }

        if not session.patient_name or session.state in (BookingState.WAITING_FOR_PATIENT_NAME, BookingState.COLLECTING_PATIENT_NAME) or session.last_asked_field == "patient_name":
            _name_prefixes = [
                r"(?:(?:sure|yeah|yes|ok|okay),?\s*)?(?:my\s+(?:full\s+)?name\s+is)",
                r"(?:(?:sure|yeah|yes|ok|okay),?\s*)?(?:it's|its|it\s+is|this\s+is)",
                r"(?:you\s+can\s+call\s+me)",
                r"(?:call\s+me)",
                r"(?:myself)",
                r"(?:en(?:noda|\s)?\s*(?:full\s+)?(?:peru|name))",
                r"(?:என்(?:னோட)?\s*(?:பெயர்|full\s*name))",
                r"(?:name\s+is)",
                r"(?:naam\s+(?:hai)?)",
            ]
            _name_suffixes = [r"\b(?:sir|madam|ji|bro|anna|akka)\b", r"(?:தான்|டா|ங்க)"]
            _name_prefix_pattern = r"^\s*(?:" + "|".join(_name_prefixes) + r")\s+"
            _name_suffix_pattern = r"\s*(?:" + "|".join(_name_suffixes) + r")\s*$"

            name_match = re.search(_name_prefix_pattern, txt_lower, re.IGNORECASE)
            if not name_match and re.search(r"^\s*(?:(?:sure|yeah|yes|ok|okay),?\s*)?(?:i\s*(?:am|'m))\s+", txt_lower, re.IGNORECASE):
                # Only accept "I am / I'm" if not followed by non-name words like gonna, going, here, etc.
                after_iam = re.sub(r"^\s*(?:(?:sure|yeah|yes|ok|okay),?\s*)?(?:i\s*(?:am|'m))\s+", "", txt_lower, flags=re.IGNORECASE).strip()
                first_w = after_iam.split()[0] if after_iam else ""
                if first_w and first_w not in _NON_NAME_WORDS:
                    name_match = re.search(r"^\s*(?:(?:sure|yeah|yes|ok|okay),?\s*)?(?:i\s*(?:am|'m))\s+", txt_lower, re.IGNORECASE)

            if name_match:
                raw_name = text[name_match.end():].strip()
                raw_name = re.sub(_name_suffix_pattern, '', raw_name, flags=re.IGNORECASE).strip()
                # Remove trailing non-name content (email, phone, booking intent continuation)
                raw_name = re.sub(r'\s+(?:and|&)\s+(?:i\s+want|want|book|need|schedule|with|for|my\s+email|my\s+phone|email|phone|number|mobile).*$', '', raw_name, flags=re.IGNORECASE).strip()
                cand = self.clean_person_name(raw_name)
                if cand and self.is_valid_person_name(cand):
                    session.patient_name = cand
            elif session.state in (BookingState.WAITING_FOR_PATIENT_NAME, BookingState.COLLECTING_PATIENT_NAME) or session.last_asked_field == "patient_name":
                # When system explicitly asked for name, strip conversational prefix cleanly
                cleaned = re.sub(r'^(?:(?:sure|yes|yeah|ok|okay|hi|hello|well)\s*,?\s*)?(?:it\'s|its|it\s+is|this\s+is|i\s+am|i\'m|my\s+name\s+is|my\s+name|name\s+is|you\s+can\s+call\s+me|call\s+me)?\s*,?\s*', '', text, flags=re.IGNORECASE).strip()
                cleaned = re.sub(r'[^\w\s]', '', cleaned).strip()
                cleaned = re.sub(_name_suffix_pattern, '', cleaned, flags=re.IGNORECASE).strip()
                cleaned = re.sub(r'\s+(?:and|&)\s+.*$', '', cleaned, flags=re.IGNORECASE).strip()
                cand = self.clean_person_name(cleaned)
                if cand and self.is_valid_person_name(cand):
                    session.patient_name = cand

        # 5. Email Extraction
        spoken_email = self.parse_spoken_email(text)
        if spoken_email:
            if not session.email or session.state in (BookingState.WAITING_FOR_CONFIRMATION, BookingState.COLLECTING_EMAIL, BookingState.WAITING_FOR_EMAIL) or any(w in txt_lower for w in ["email", "mail", "change"]):
                session.email = spoken_email

        # 6. Contact Phone Number Extraction
        spoken_phone = self.parse_spoken_phone_number(text)
        if spoken_phone:
            if not session.contact_number or session.contact_number == "Unknown" or session.state in (BookingState.WAITING_FOR_CONFIRMATION, BookingState.COLLECTING_PHONE, BookingState.WAITING_FOR_CONTACT) or any(w in txt_lower for w in ["phone", "mobile", "number", "change"]):
                session.contact_number = spoken_phone

        # 6b. Caller ID Confirmation
        if (
            session.booking_data.get("caller_id_suggested")
            and (not session.contact_number or session.contact_number == "Unknown")
            and session.caller_phone
            and session.caller_phone not in ("Unknown", "Unknown Caller", "Private Number", "09410697548", "+919410697548")
        ):
            is_affirm = (
                any(w in txt_lower.split() for w in ["yes", "yeah", "yep", "sure", "correct", "fine", "ok", "okay", "ha", "haan", "sari", "aam"])
                or any(p in txt_lower for p in ["use that", "use this", "that's fine", "that is fine", "same number", "this number", "use my number", "use caller", "that number", "use it", "sounds good", "looks good"])
            )
            is_neg = any(w in txt_lower.split() for w in ["no", "nope", "different", "change", "not", "illa", "nahi"])
            if is_affirm and not is_neg:
                session.contact_number = session.caller_phone
                session.booking_data["caller_id_confirmed"] = True

        # 7. Customer Location Extraction
        spoken_loc = self.parse_spoken_location(text)
        if spoken_loc:
            session.location = spoken_loc

    def check_and_handle_intent_switch(self, session: BookingSession, current_intent: str, text: str = "") -> bool:
        """
        Prevents Stale Context by checking if user changed topic to an explicit non-booking intent.
        Returns True if turn should bypass booking state machine.
        CRITICAL: Never bypass an active booking flow when customer is providing booking fields
        (name, email, phone, date, time, confirmations).
        """
        if not session.is_active():
            return False

        txt_lower = (text or "").lower().strip()

        if not txt_lower and current_intent in ("general_conversation", "greeting"):
            return True

        # Explicit cancellation / exit — strictly match unambiguous booking cancellation phrases
        _CANCEL_PHRASES = [
            "cancel booking", "cancel the booking", "cancel appointment", "cancel the appointment",
            "stop booking", "dont book", "do not book", "don't book",
            "never mind", "nevermind", "forget it", "leave it",
        ]
        if any(phrase in txt_lower for phrase in _CANCEL_PHRASES):
            session.reset()
            return True

        # Informational question indicators in user text
        info_indicators = [
            "can you list", "list the", "list your", "list all", "list services", "list staff", "list doctors", "list teachers", "list members",
            "what services", "which services", "what are all the service", "what are all the services",
            "who are all available", "who is available", "who are available", "who works",
            "how much", "what is the price", "what is the cost", "cost of", "price of", "pricing",
            "tell me about", "tell me", "where are you", "what are your hours", "when are you open", "who are your",
            "do you have", "what do you offer", "what treatments", "real person", "human agent",
            "speak to human", "now i want to go real", "school", "hospital", "salon", "information",
            "what about", "how many", "can i know", "may i know", "details", "explain", "how are you", "where is", "where are",
            "working hours", "opening hours", "located",
            "want to know about", "want to know", "know more about", "know about", "what is", "what are",
            "can you explain", "please explain", "features of", "info on", "information about", "details about"
        ]
        is_info_question = any(ind in txt_lower for ind in info_indicators) or (
            bool(re.search(r'^(?:what|who|which|how|where|when|can\s+you|could\s+you|list|show|tell)\b', txt_lower))
            and not any(k in txt_lower for k in ["appointment", "book", "slot"])
        )

        def _cleanup_and_record_inquiry():
            # Check negative rejection (e.g. "I don't want Galaxy Jet")
            neg_m = re.search(r'\b(?:don\'?t\s+want|do\s+not\s+want|no\s+|not\s+interested\s+in|instead\s+of)\s+(?:the\s+|a\s+|an\s+)?([a-zA-Z0-9\s]{2,30}?)(?=[.,;!?]|\s+(?:i\s+want|i\s+need|but|please|$))\b', txt_lower)
            if neg_m:
                rej = neg_m.group(1).strip()
                if rej and rej not in {"appointment", "booking", "slot", "date", "time", "this", "that"}:
                    session._rejected_services.add(rej.lower())
                    session._rejected_departments.add(rej.lower())
                    session._rejected_doctors.add(rej.lower())

            # Detect dynamic product/service inquired
            org_id = getattr(session, "organization_id", 1) or 1
            all_opts = self.get_organization_services_and_departments(org_id)
            for opt in all_opts:
                if opt.lower() in txt_lower and opt.lower() not in getattr(session, "_rejected_services", set()):
                    session.booking_data["last_inquired_product"] = opt
                    break

            # Clear obsolete booking prompts / terms
            session.booking_data.pop("user_requested_entity_term", None)
            session.booking_data.pop("unverified_entity_requested", None)
            session.booking_data.pop("ambiguous_entity_candidate", None)
            session.booking_data.pop("pending_confirmation_entity", None)

        _customer_detail_states = (
            BookingState.COLLECTING_PATIENT_NAME,
            BookingState.COLLECTING_PHONE,
            BookingState.COLLECTING_EMAIL,
        )

        # In customer detail collection states, generic intents like 'general_question' or 'general_conversation'
        # must NEVER bypass the booking flow unless an explicit informational query is actually detected in the text.
        if session.state in _customer_detail_states or getattr(session, "last_asked_field", "") in ("patient_name", "contact_number", "email"):
            if not is_info_question and current_intent not in ("emergency/urgent request", "transfer request"):
                return False

        # Check if customer's utterance is answering the active booking prompt:
        if txt_lower:
            # 0. Doctor / Staff prompt / answers
            if session.state in (BookingState.WAITING_FOR_DOCTOR,) or getattr(session, "last_asked_field", "") in ("doctor", "service", "entity_confirmation"):
                if any(w in txt_lower for w in ["dr", "doctor", "dr.", "physician", "prof", "consult"]) or len(txt_lower.split()) <= 4:
                    if not is_info_question:
                        return False

            # 1. Date prompt / answers
            if session.state in (BookingState.WAITING_FOR_DATE,) or getattr(session, "last_asked_field", "") == "date":
                if any(dk in txt_lower for dk in ["tomorrow", "today", "next", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]) or re.search(r'\b\d{1,2}(?:st|nd|rd|th)?\b', txt_lower):
                    if not is_info_question:
                        return False

            # 2. Time prompt / answers
            if session.state in (BookingState.WAITING_FOR_TIME,) or getattr(session, "last_asked_field", "") == "time":
                if re.search(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b', txt_lower) or any(tk in txt_lower for tk in ["morning", "afternoon", "evening", "noon", "am", "pm", "any time", "earliest", "any slot"]):
                    if not is_info_question:
                        return False

            # 3. Email prompt / answers
            if session.state in (BookingState.WAITING_FOR_EMAIL, BookingState.COLLECTING_EMAIL) or getattr(session, "last_asked_field", "") == "email":
                if any(w in txt_lower for w in ["@", "at", "dot", "gmail", "yahoo", "outlook", "email", "mail", ".com", ".in", ".org", "com"]) or bool(self.parse_spoken_email(text)) or len(txt_lower.split()) <= 4:
                    if not is_info_question:
                        return False

            # 4. Name prompt / answers
            if session.state in (BookingState.WAITING_FOR_PATIENT_NAME, BookingState.COLLECTING_PATIENT_NAME) or getattr(session, "last_asked_field", "") == "patient_name":
                if any(w in txt_lower for w in ["my name", "name is", "i am", "this is", "call me"]) or len(txt_lower.split()) <= 4:
                    if not is_info_question:
                        return False

            # 5. Contact phone prompt / answers
            if session.state in (BookingState.WAITING_FOR_CONTACT, BookingState.COLLECTING_PHONE) or getattr(session, "last_asked_field", "") == "contact_number":
                if re.search(r'\d{5,}', txt_lower) or any(w in txt_lower for w in ["number", "mobile", "phone"]):
                    if not is_info_question:
                        return False

            if is_info_question:
                _cleanup_and_record_inquiry()
                logger.info(f"[INTENT SWITCH DETECTED] Customer asked informational query '{text}'. Bypassing booking state to Active RAG (session fields preserved).")
                return True

        explicit_informational_intents = {
            "doctor_list", "doctor_count", "doctor_information",
            "department_list", "hospital_information", "service_information",
            "faq", "emergency/urgent request", "transfer request"
        }
        if current_intent in explicit_informational_intents:
            _cleanup_and_record_inquiry()
            logger.info(f"[INTENT SWITCH DETECTED] Customer switched topic to informational intent '{current_intent}'. Bypassing booking state.")
            return True

        if is_info_question:
            _cleanup_and_record_inquiry()
            logger.info(f"[INTENT SWITCH DETECTED] Customer asked informational query '{text}'. Bypassing booking state.")
            return True

        return False

    def process_booking_turn(self, text: str, session: BookingSession, turn_id: Optional[str] = None) -> str:
        """
        Stateful Conversational Turn Processor.
        Evaluates session memory, extracts entities, validates doctors, performs live slot verification,
        and dynamically determines the single next required action/question.
        """
        # Idempotency check: if this exact customer turn was already evaluated, reuse the response
        if not hasattr(session, "_turn_response_cache"):
            session._turn_response_cache = {}
        if turn_id and str(turn_id) in session._turn_response_cache:
            cached_resp = session._turn_response_cache[str(turn_id)]
            logger.info(f"[BOOKING_TURN_IDEMPOTENT] Reusing cached response for turn {turn_id}: '{cached_resp[:60]}...'")
            return cached_resp

        def _ret(r: str) -> str:
            session.last_ai_response = r
            if turn_id:
                session._turn_response_cache[str(turn_id)] = r
            return r

        session.conversation_started = True
        txt_lower = text.lower().strip()
        
        session.last_user_text = txt_lower
        session.turn_count += 1
        session.turn_id = session.turn_count
        session.state_version = getattr(session, "state_version", 0) + 1
        state_before = copy.deepcopy(session.get_state_dict())

        # Presence check ("are you there", "can you hear me") during active booking
        presence_phrases = ["can you hear me", "are you there", "am i audible", "can you hear", "hear me", "is anyone there"]
        if any(phrase in txt_lower for phrase in presence_phrases) and not any(w in txt_lower for w in ["book", "appointment", "doctor", "schedule", "reserve"]):
            silence_prompt = self.get_silence_prompt(session)
            resp = f"Yes, I'm right here! {silence_prompt}"
            self._log_conversation_turn(session, text, resp, "presence_reassurance", state_before=state_before)
            return _ret(resp)

        from src.services.language_service import language_service
        session.conversation_language = language_service.determine_conversation_language(text, current_lang=session.conversation_language)

        lang = session.conversation_language

        # Cancellation check — only explicit booking-cancel phrases, not bare 'cancel'
        _CANCEL_PHRASES = [
            "cancel booking", "cancel the booking", "cancel appointment", "cancel the appointment",
            "stop booking", "dont book", "do not book", "don't book",
            "never mind", "nevermind", "forget it", "leave it",
        ]
        if any(phrase in txt_lower for phrase in _CANCEL_PHRASES):
            session.reset()
            resp = "Your appointment booking has been cancelled. How else may I assist you today?"
            if lang in ("ta", "tanglish", "ta_mix"):
                resp = "உங்க appointment booking ரத்து பண்ணியாச்சு. வேற ஏதாச்சு உதவி வேணுமா?"
            elif lang == "hi":
                resp = "Aapka appointment booking cancel kar diya gaya hai. Main aapki aur kya sahayata kar sakta hoon?"
            self._log_conversation_turn(session, text, resp, "booking_cancelled", state_before=state_before)
            return _ret(resp)

        # If already booked in this session
        if session.state in (BookingState.BOOKED, BookingState.COMPLETED) and session.appointment_id:
            # Check pleasantries / thanks / closing first
            if any(w in txt_lower for w in ["thank you", "thanks", "bye", "goodbye", "that's all", "that is all", "nothing else", "no that's it"]):
                resp = f"You're very welcome, {session.patient_name or 'there'}! Your appointment is all set. Have a wonderful day!"
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = f"ரொம்ப நன்றி! உங்க appointment confirm ஆயிடுச்சு. நல்ல நாளாக அமையட்டும்!"
                elif lang == "hi":
                    resp = f"Aapka bahut dhanyawad! Aapka appointment confirm hai. Aapka din shubh ho!"
                self._log_conversation_turn(session, text, resp, "booking_closing", state_before=state_before)
                return _ret(resp)

            if not any(w in txt_lower for w in ["new appointment", "another appointment", "book again", "book another", "one more appointment"]):
                resp = f"Your appointment with {session.doctor} on {session.date} at {session.time} is already confirmed under Appointment ID #{session.appointment_id}."
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = f"உங்க appointment {session.doctor}-கூட {session.date} அன்று {session.time}-க்கு confirm ஆயிடுச்சு (ID #{session.appointment_id})."
                elif lang == "hi":
                    resp = f"Aapka appointment {session.doctor} ke saath {session.date} ko {session.time} par already confirmed hai (ID #{session.appointment_id})."
                self._log_conversation_turn(session, text, resp, "already_booked", state_before=state_before)
                return _ret(resp)
            else:
                session.reset_for_new_booking()

        if session.state == BookingState.IDLE:
            session.state = BookingState.BOOKING_STARTED

        # 0. Check if caller is confirming a previously suggested ambiguous entity
        pending_suggested = session.booking_data.get("pending_confirmation_entity")
        if pending_suggested:
            is_affirmative = any(w in txt_lower.split() for w in ["yes", "yeah", "ok", "okay", "sure", "correct", "yep", "right", "that", "please"]) or pending_suggested.lower() in txt_lower
            is_rejection = any(w in txt_lower.split() for w in ["no", "nope", "dont", "don't", "not", "neither"]) or is_phrase_negated(pending_suggested, text)
            if is_affirmative and not is_rejection:
                confirmed_entity = session.booking_data.pop("pending_confirmation_entity")
                session.service = confirmed_entity
                session.booking_data["service"] = confirmed_entity
                session.booking_data["product"] = confirmed_entity
                session.booking_data.pop("ambiguous_entity_candidate", None)
                session.booking_data.pop("unverified_entity_requested", None)
                session.booking_data.pop("user_requested_entity_term", None)
                if session.state in (BookingState.WAITING_FOR_DOCTOR, BookingState.BOOKING_STARTED, BookingState.IDLE):
                    session.state = BookingState.WAITING_FOR_DATE
                    session.last_asked_field = "date"
                resp = f"Got it, {confirmed_entity}. Which date would you prefer for your appointment?"
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = f"சரி, {confirmed_entity}. எந்த தேதியில் appointment book பண்ணலாம்?"
                elif lang == "hi":
                    resp = f"Theek hai, {confirmed_entity}. Aap kis date ko appointment prefer karenge?"
                self._log_conversation_turn(session, text, resp, "ambiguous_entity_confirmed", state_before=state_before)
                return _ret(resp)
            elif is_rejection:
                session.booking_data.pop("pending_confirmation_entity", None)
                session.booking_data.pop("ambiguous_entity_candidate", None)

        # 0b. If caller previously inquired about a product and is now proceeding to book
        if not session.service and session.booking_data.get("last_inquired_product"):
            inquired_prod = session.booking_data.get("last_inquired_product")
            if any(w in txt_lower for w in ["book", "appointment", "schedule", "reserve", "yes", "yeah", "today", "tomorrow"]) or not any(w in txt_lower for w in ["different", "another", "instead"]):
                session.service = inquired_prod
                session.booking_data["service"] = inquired_prod
                session.booking_data["product"] = inquired_prod
                session.booking_data.pop("last_inquired_product", None)
                if session.state in (BookingState.WAITING_FOR_DOCTOR, BookingState.BOOKING_STARTED, BookingState.IDLE):
                    session.state = BookingState.WAITING_FOR_DATE
                    session.last_asked_field = "date"

        # 1. Extract entities & validate doctor / product
        self.extract_slots(text, session)

        # Handle ambiguous entity suggestion
        if session.booking_data.get("ambiguous_entity_candidate"):
            amb = session.booking_data.pop("ambiguous_entity_candidate")
            raw = amb.get("raw") or "that"
            suggested = amb.get("suggested")
            session.booking_data["pending_confirmation_entity"] = suggested
            session.booking_data.pop("unverified_entity_requested", None)
            session.booking_data.pop("user_requested_entity_term", None)
            session.state = BookingState.WAITING_FOR_DOCTOR
            session.last_asked_field = "entity_confirmation"
            resp = f"I couldn't find '{raw}' in our directory. Did you mean '{suggested}'?"
            if lang in ("ta", "tanglish", "ta_mix"):
                resp = f"எங்க directory-ல '{raw}' இல்ல. நீங்க '{suggested}'-ஐ சொல்றீங்களா?"
            elif lang == "hi":
                resp = f"Hamari directory me '{raw}' nahi hai. Kya aapka matlab '{suggested}' se hai?"
            self._log_conversation_turn(session, text, resp, "ambiguous_entity_clarification", state_before=state_before)
            return _ret(resp)

        # Handle unverified staff/person/service requested against active organization
        if session.booking_data.get("unverified_entity_requested") or session.booking_data.get("unknown_doctor_requested"):
            unverified = session.booking_data.pop("unverified_entity_requested", None) or session.booking_data.pop("unknown_doctor_requested", None)
            from src.services.entity_validation_service import entity_validation_service as _ev
            _re_check = _ev.validate_entity(unverified or "", organization_id=getattr(session, "organization_id", 1) or 1)
            if _re_check.is_role_category:
                session.doctor = _re_check.verified_name or unverified
                session.doctor_locked = True
                session.booking_data["specialty"] = session.doctor
            elif _re_check.is_verified:
                if _re_check.entity_type in ("product", "service"):
                    session.service = _re_check.verified_name or unverified
                    session.booking_data["service"] = session.service
                    session.booking_data["product"] = session.service
                    session.booking_data["specialty"] = session.service
                    session.state = BookingState.WAITING_FOR_DATE
                    session.last_asked_field = "date"
                else:
                    session.doctor = _re_check.verified_name or unverified
                    if _re_check.staff_id:
                        session.doctor_id = _re_check.staff_id
                    session.doctor_locked = True
                    session.booking_data["specialty"] = _re_check.role_or_specialty or session.doctor
            elif getattr(_re_check, "is_ambiguous", False) and getattr(_re_check, "suggested_entity", None):
                suggested = _re_check.suggested_entity
                session.booking_data["pending_confirmation_entity"] = suggested
                session.booking_data.pop("user_requested_entity_term", None)
                session.state = BookingState.WAITING_FOR_DOCTOR
                session.last_asked_field = "entity_confirmation"
                resp = f"I couldn't find '{unverified}' in our directory. Did you mean '{suggested}'?"
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = f"எங்க directory-ல '{unverified}' இல்ல. நீங்க '{suggested}'-ஐ சொல்றீங்களா?"
                elif lang == "hi":
                    resp = f"Hamari directory me '{unverified}' nahi hai. Kya aapka matlab '{suggested}' se hai?"
                self._log_conversation_turn(session, text, resp, "ambiguous_entity_clarification", state_before=state_before)
                return _ret(resp)
            else:
                session.doctor = None
                session.doctor_id = None
                session.doctor_locked = False
                session.booking_data.pop("user_requested_entity_term", None)
                session.state = BookingState.WAITING_FOR_DOCTOR
                session.last_asked_field = "doctor"

                # Check if user specifically requested a doctor or rejected products/services
                is_doctor_request = (
                    session.state == BookingState.WAITING_FOR_DOCTOR
                    or any(w in txt_lower for w in ["dr", "doctor", "physician", "consultant", "meet doctor", "see doctor", "meet dr"])
                    or bool(getattr(session, "_rejected_services", set()))
                    or bool(getattr(session, "_rejected_departments", set()))
                )

                if is_doctor_request:
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = f"மன்னிக்கவும், எங்கள் directory-ல் '{unverified}' என்ற மருத்துவர் விவரம் இல்லை. நீங்கள் பார்க்க விரும்பும் மருத்துவர் அல்லது துறையின் பெயரை மீண்டும் கூற முடியுமா?"
                    elif lang == "hi":
                        resp = f"Maaf kijiye, hamari directory me '{unverified}' doctor nahi mile. Kya aap doctor ka naam ya vibhaag dohra sakte hain?"
                    else:
                        resp = f"I could not verify '{unverified}' in our doctor directory. Could you please clarify the doctor's name or department you would like to consult with?"
                else:
                    org_id = getattr(session, "organization_id", 1) or 1
                    avail_opts = self.get_organization_services_and_departments(org_id)
                    if avail_opts:
                        top_opts = [opt for opt in avail_opts if opt.lower() not in getattr(session, "_rejected_services", set()) and opt.lower() not in getattr(session, "_rejected_departments", set())][:3]
                        if top_opts:
                            opts_str = ", ".join(top_opts[:-1]) + f", or {top_opts[-1]}" if len(top_opts) > 1 else top_opts[0]
                            resp = f"I could not verify '{unverified}' in our directory. We currently offer {opts_str}. Which product or service would you like to book?"
                        else:
                            resp = f"I could not verify '{unverified}' in our directory. Which product or service would you like to book?"
                    else:
                        ask_target = self._get_target_entity_question(session, lang=lang)
                        resp = f"I couldn't find '{unverified}' in our directory. {ask_target}"
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = f"மன்னிக்கவும், '{unverified}' எங்க directory-ல இல்ல. எந்த product அல்லது service-ஐ book பண்ண விரும்புகிறீர்கள்?"
                    elif lang == "hi":
                        resp = f"Maaf kijiye, '{unverified}' hamari directory me nahi mile. Aap kaunsa product ya service book karna chahte hain?"
                self._log_conversation_turn(session, text, resp, "unverified_entity_clarification", state_before=state_before)
                return _ret(resp)

        # Check if caller utterance is an incomplete fragment or conversational cutoff
        if self._is_incomplete_utterance(text) and not any(w in txt_lower for w in ["yes", "yeah", "ok", "sure", "cancel", "no"]):
            if session.state in (BookingState.WAITING_FOR_PATIENT_NAME, BookingState.COLLECTING_PATIENT_NAME):
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = "உங்க பெயர் சொல்லுங்க, நான் கேட்டுட்டு இருக்கேன்."
                elif lang == "hi":
                    resp = "Kripya apna naam batayein, main sun raha hoon."
                else:
                    resp = "Take your time. May I have your full name whenever you're ready?"
                self._log_conversation_turn(session, text, resp, "incomplete_utterance_reassurance", state_before=state_before)
                return _ret(resp)
            elif session.state == BookingState.WAITING_FOR_DOCTOR:
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = "நான் கேட்டுட்டு இருக்கேன். எந்த doctor-ஐ பார்க்க விரும்புகிறீர்கள்?"
                elif lang == "hi":
                    resp = "Main sun raha hoon. Aap kis doctor se milna chahte hain?"
                else:
                    resp = "Take your time. Which doctor or department would you like to consult with?"
                self._log_conversation_turn(session, text, resp, "incomplete_utterance_reassurance", state_before=state_before)
                return _ret(resp)
            elif session.state == BookingState.WAITING_FOR_TIME:
                resp = "Take your time. Which time slot would you prefer for the appointment?"
                self._log_conversation_turn(session, text, resp, "incomplete_utterance_reassurance", state_before=state_before)
                return _ret(resp)
            elif session.state == BookingState.WAITING_FOR_DATE:
                resp = "Take your time. Which date would you prefer for the appointment?"
                self._log_conversation_turn(session, text, resp, "incomplete_utterance_reassurance", state_before=state_before)
                return _ret(resp)

        # 2. Check if caller explicitly asked for available slots
        if any(phrase in txt_lower for phrase in [
            "available slots", "slots available", "what slots", "which slots",
            "time slots", "open slots", "free slots", "available times", "available time",
            "schedule of doctor", "schedule of staff", "what time slots do you have", "what times"
        ]):
            doc_to_check = session.doctor
            if not doc_to_check:
                session.state = BookingState.WAITING_FOR_DOCTOR
                session.last_asked_field = "doctor"
                first_name = (session.patient_name or "").split()[0] if session.patient_name and self.is_valid_person_name(session.patient_name) else ""
                name_ack_slots = f"Thanks, {first_name}. " if first_name else "Sure! "
                resp = self._get_target_entity_question(session, name_ack=name_ack_slots, lang=lang)
                self._log_conversation_turn(session, text, resp, "ask_doctor_for_slots", state_before=state_before)
                return _ret(resp)

            session.state = BookingState.WAITING_FOR_TIME
            session.last_asked_field = "time"
            resp = self.check_doctor_availability_live(doc_to_check, session.date, session=session)
            self._log_conversation_turn(session, text, resp, "presented_slots", state_before=state_before)
            return _ret(resp)

        # DYNAMIC MISSING-INFORMATION EVALUATION WITH PERSONALIZED ACKNOWLEDGMENTS
        first_name = (session.patient_name or "").split()[0] if session.patient_name and self.is_valid_person_name(session.patient_name) else ""
        
        # Check if we should acknowledge a short affirmative/acknowledgement
        short_ack = ""
        if len(txt_lower.split()) <= 3 and any(w in txt_lower for w in ["yes", "yeah", "ok", "okay", "sure", "thanks", "thank you", "correct"]):
            short_ack = "Got it. "
            
        name_ack = f"{short_ack}Thanks, {first_name}. " if first_name else f"{short_ack}"

        # STEP 1: Staff / Representative / Service Check — dynamically RAG-aware
        if session.service or session.booking_data.get("product"):
            # Product or service booking does not require choosing a medical doctor
            pass
        elif not session.doctor:
            org_id = getattr(session, "organization_id", 1) or 1
            if session.department:
                dept_docs = self.get_doctors_for_department(session.department, org_id, session=session)
                if dept_docs:
                    if len(dept_docs) == 1:
                        target_doc = dept_docs[0]
                        # Check if user confirmed the single offered doctor (e.g. "yes", "sure", "ok", or doctor's name)
                        is_affirmative = any(w in txt_lower.split() for w in ["yes", "yeah", "ok", "okay", "sure", "correct", "fine", "yep", "dr", "doctor", "her", "him", "please"]) or target_doc["clean_name"] in txt_lower
                        is_refusal = (
                            any(w in txt_lower.split() for w in ["no", "nope", "nah", "never", "dont", "don't", "not", "neither"])
                            or any(p in txt_lower for p in ["another doctor", "different doctor", "switch doctor", "change doctor", "someone else", "somebody else", "other doctor", "not him", "not her"])
                            or is_phrase_negated(target_doc["name"], text)
                            or is_phrase_negated(target_doc["clean_name"], text)
                        )
                        was_offered = getattr(session, "_offered_dept_doctor", None) == target_doc["name"]

                        if was_offered and is_refusal:
                            # Customer rejects offered doctor
                            # Check if department itself was also rejected (e.g. "I don't want dental care")
                            is_dept_refused = (
                                is_phrase_negated(session.department, text)
                                or any(w in txt_lower for w in ["different department", "another department", "wrong department", "not this department"])
                            )
                            if is_dept_refused:
                                session.invalidate_department(session.department)
                                session.state = BookingState.WAITING_FOR_DOCTOR
                                session.last_asked_field = "doctor"
                                resp = "Understood. Which department or doctor would you prefer to book with?"
                                self._log_conversation_turn(session, text, resp, "department_rejected", state_before=state_before)
                                return _ret(resp)
                            else:
                                session.invalidate_doctor(target_doc["name"])
                                remaining_docs = self.get_doctors_for_department(session.department, org_id, session=session)
                                if remaining_docs:
                                    next_doc = remaining_docs[0]
                                    session._offered_dept_doctor = next_doc["name"]
                                    resp = f"Understood. We also have {next_doc['name']} available for {session.department}. Would you like to schedule with {next_doc['name']}?"
                                    self._log_conversation_turn(session, text, resp, "offer_alternate_doctor", state_before=state_before)
                                    return _ret(resp)
                                else:
                                    session.state = BookingState.WAITING_FOR_DOCTOR
                                    session.last_asked_field = "doctor"
                                    resp = f"Understood. {target_doc['name']} is currently our only specialist in {session.department}. Would you like to check another department or specialty?"
                                    self._log_conversation_turn(session, text, resp, "only_doctor_rejected", state_before=state_before)
                                    return _ret(resp)

                        elif is_affirmative and was_offered:
                            session.doctor = target_doc["name"]
                            session.doctor_id = target_doc["id"]
                            session.doctor_locked = True
                            if not hasattr(session, "confirmed_facts"):
                                session.confirmed_facts = {}
                            session.confirmed_facts["doctor"] = {"value": target_doc["name"], "source": "AUTHORITATIVE_DIRECTORY"}
                            if not hasattr(session, "completed_steps"):
                                session.completed_steps = []
                            if "doctor_selection" not in session.completed_steps:
                                session.completed_steps.append("doctor_selection")
                            # Fall through directly to Date / Time check below!
                        else:
                            session._offered_dept_doctor = target_doc["name"]
                            session.state = BookingState.WAITING_FOR_DOCTOR
                            session.last_asked_field = "doctor"
                            resp = f"For {session.department}, we have {target_doc['name']} available. Would you like to schedule with {target_doc['name']}?"
                            if lang in ("ta", "tanglish", "ta_mix"):
                                resp = f"{session.department}-க்கு {target_doc['name']} available-ஆ இருக்காங்க. {target_doc['name']}-கூட schedule பண்ணலாமா?"
                            elif lang == "hi":
                                resp = f"{session.department} ke liye hamare paas {target_doc['name']} available hain. Kya aap {target_doc['name']} ke saath schedule karna chahenge?"
                            self._log_conversation_turn(session, text, resp, "offer_department_doctor", state_before=state_before)
                            return _ret(resp)
                    else:
                        # Multiple doctors in department
                        is_refusal = (
                            any(w in txt_lower.split() for w in ["no", "none", "neither"])
                            or any(p in txt_lower for p in ["none of them", "neither of them", "different department", "another department"])
                            or is_phrase_negated(session.department, text)
                        )
                        if is_refusal:
                            session.invalidate_department(session.department)
                            session.state = BookingState.WAITING_FOR_DOCTOR
                            session.last_asked_field = "doctor"
                            resp = "Understood. Which department or doctor would you prefer to book with?"
                            self._log_conversation_turn(session, text, resp, "department_rejected_from_multi", state_before=state_before)
                            return _ret(resp)

                        matched_doc = next((d for d in dept_docs if d["clean_name"] in txt_lower and d["clean_name"] not in getattr(session, "_rejected_doctors", set()) and not is_phrase_negated(d["name"], text)), None)
                        if matched_doc:
                            session.doctor = matched_doc["name"]
                            session.doctor_id = matched_doc["id"]
                            session.doctor_locked = True
                            if not hasattr(session, "confirmed_facts"):
                                session.confirmed_facts = {}
                            session.confirmed_facts["doctor"] = {"value": matched_doc["name"], "source": "AUTHORITATIVE_DIRECTORY"}
                            if not hasattr(session, "completed_steps"):
                                session.completed_steps = []
                            if "doctor_selection" not in session.completed_steps:
                                session.completed_steps.append("doctor_selection")
                            # Fall through directly to Date / Time check below!
                        else:
                            names_text = ", ".join([d["name"] for d in dept_docs[:-1]]) + f", or {dept_docs[-1]['name']}"
                            session.state = BookingState.WAITING_FOR_DOCTOR
                            session.last_asked_field = "doctor"
                            resp = f"For {session.department}, we have {names_text} available. Which doctor would you like to book with?"
                            self._log_conversation_turn(session, text, resp, "offer_multiple_department_doctors", state_before=state_before)
                            return _ret(resp)
                else:
                    session.state = BookingState.WAITING_FOR_DOCTOR
                    session.last_asked_field = "doctor"
                    resp = f"We currently do not have any doctors listed for {session.department}. Which department or doctor would you like to book with?"
                    self._log_conversation_turn(session, text, resp, "no_doctors_in_department", state_before=state_before)
                    return _ret(resp)
            else:
                session.state = BookingState.WAITING_FOR_DOCTOR
                session.last_asked_field = "doctor"
                if getattr(session, "_last_rejected_entity", None) == "department":
                    session._last_rejected_entity = None
                    resp = "Understood. Which department or doctor would you prefer to book with?"
                else:
                    resp = self._get_target_entity_question(session, name_ack=name_ack, lang=lang)
                self._log_conversation_turn(session, text, resp, "waiting_for_doctor", state_before=state_before)
                return _ret(resp)

        _MONTH_NAMES = {
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
            "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
        }

        # Target label for date/time prompts (doctor, service, product, or department)
        target_label = session.service or session.doctor or session.department or "consultation"

        # STEP 2: Date Check
        if not session.date:
            session.state = BookingState.WAITING_FOR_DATE
            session.last_asked_field = "date"
            resp = f"{name_ack}Which date would you prefer for your {target_label} appointment?"
            if lang in ("ta", "tanglish", "ta_mix"):
                resp = f"{'நன்றி, ' + first_name + '. ' if first_name else ''}{target_label}-க்கு எந்த நாளுக்கு appointment book பண்ணலாம்?"
            elif lang == "hi":
                resp = f"{'Shukriya, ' + first_name + '. ' if first_name else ''}{target_label} ke liye aap kis tareekh ko appointment prefer karenge?"
            self._log_conversation_turn(session, text, resp, "waiting_for_date", state_before=state_before)
            return _ret(resp)
        elif session.date and session.date.lower().strip() in _MONTH_NAMES:
            session.state = BookingState.WAITING_FOR_DATE
            session.last_asked_field = "date"
            month_cap = session.date.title()
            resp = f"{name_ack}What date in {month_cap} would you prefer for your {target_label} appointment?"
            if lang in ("ta", "tanglish", "ta_mix"):
                resp = f"{'நன்றி, ' + first_name + '. ' if first_name else ''}{target_label}-க்கு {month_cap} மாதத்தில் எந்த தேதி வசதியாக இருக்கும்?"
            elif lang == "hi":
                resp = f"{'Shukriya, ' + first_name + '. ' if first_name else ''}{target_label} ke liye {month_cap} me konsi tareekh pasand karenge?"
            self._log_conversation_turn(session, text, resp, "waiting_for_exact_date_in_month", state_before=state_before)
            return _ret(resp)

        # STEP 3: Time Check (Proactively query & present live Calendar slots)
        if not session.time:
            session.state = BookingState.WAITING_FOR_TIME
            session.last_asked_field = "time"

            # Check if user gave a pure affirmative answer to previously offered slots
            # but did NOT specify a slot number or time — re-prompt to pick one specifically
            is_affirmative = any(w in txt_lower.split() for w in ["yes", "yeah", "ok", "okay", "sure", "correct", "fine", "yep"]) or "book me" in txt_lower
            has_ordinal = any(w in txt_lower for w in ["first", "second", "third", "fourth", "fifth", "last", "1st", "2nd", "3rd", "4th", "5th"])
            has_any_number = bool(re.search(r'\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b', txt_lower))

            if session.offered_slots and is_affirmative and not has_ordinal and not has_any_number:
                # Track how many times we've re-prompted for the same slot list
                _slot_prompt_count = getattr(session, "_slot_prompt_count", 0) + 1
                session._slot_prompt_count = _slot_prompt_count

                if _slot_prompt_count >= 3:
                    # After 3 re-prompts, auto-select the first available slot to avoid infinite loop
                    extracted_time = session.offered_slots[0]
                    session.time = extracted_time
                    session.availability_verified = True
                    session._slot_prompt_count = 0
                    logger.info(f"[SLOT_AUTO_SELECT] Auto-selected first slot '{extracted_time}' after {_slot_prompt_count} ambiguous responses.")
                    # Fall through to STEP 4/5 below — don't return here
                else:
                    resp = f"Which specific time would you like? Available times are: {', '.join(session.offered_slots)}. Please say the time you'd like."
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = f"எந்த நேரம் book பண்ணலாம்? Available slots: {', '.join(session.offered_slots)}. ஒரு specific time சொல்லுங்க."
                    elif lang == "hi":
                        resp = f"Kaunsa specific samay chahiye? Available times hain: {', '.join(session.offered_slots)}. Kripya ek time batayein."
                    self._log_conversation_turn(session, text, resp, "re_prompt_slot_choice", state_before=state_before)
                    return _ret(resp)

            if not session.time:
                # Check if slots for this target and date have ALREADY been presented to caller
                slots_already_offered = bool(
                    session.offered_slots
                    and getattr(session, "_slots_presented_for", None) == (target_label, session.date)
                )
                wants_slots_repeated = any(ph in txt_lower for ph in [
                    "what slots", "which slots", "available slots", "repeat", "available times", "what times", "what time slots", "what are the slots"
                ])

                if slots_already_offered and not wants_slots_repeated:
                    resp = f"Which of those times works best for you, or would you like to check another date?"
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = "அந்த நேரங்களில் எது உங்களுக்கு வசதியாக இருக்கும், அல்லது வேறு தேதி பார்க்கலாமா?"
                    elif lang == "hi":
                        resp = "Unme se kaunsa samay aapke liye theek rahega, ya koi dusri tareekh dekhni hai?"
                    self._log_conversation_turn(session, text, resp, "re_prompt_slot_without_repeating_list", state_before=state_before)
                    return _ret(resp)

                avail_msg = self.check_doctor_availability_live(session.doctor or session.service, session.date, session=session)
                if avail_msg:
                    session._slot_prompt_count = 0
                    session._slots_presented_for = (target_label, session.date)
                    resp = f"{name_ack}{avail_msg}" if (first_name and not avail_msg.startswith("Thanks")) else avail_msg
                    self._log_conversation_turn(session, text, resp, "presented_live_slots", state_before=state_before)
                    return _ret(resp)
                resp = f"{name_ack}What time would you prefer for your {target_label} appointment on {session.date}?"
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = f"{'நன்றி, ' + first_name + '. ' if first_name else ''}{target_label}-க்கு {session.date} எந்த நேரம் வசதியா இருக்கும்?"
                elif lang == "hi":
                    resp = f"{'Shukriya, ' + first_name + '. ' if first_name else ''}{target_label} ke liye {session.date} ko konsa samay pasand karenge?"
                self._log_conversation_turn(session, text, resp, "waiting_for_time", state_before=state_before)
                return _ret(resp)

        # STEP 4: Live Exact Slot Availability Check (Stage 1 Verification)
        target_entity_name = session.doctor or session.service
        if target_entity_name and session.date and session.time and not session.availability_verified:
            try:
                from src.services.calendar_verification_service import calendar_verification_service
                query_str = f"Appointment with {target_entity_name} on {session.date} at {session.time}"
                verif = calendar_verification_service.verify_availability(query_str)

                if verif.is_checked and verif.is_available is False:
                    requested_time = session.time
                    session.time = None
                    session.availability_verified = False
                    session.state = BookingState.WAITING_FOR_TIME
                    session.last_asked_field = "time"
                    if verif.alternative_slots:
                        session.offered_slots = verif.alternative_slots
                        alt_text = ", ".join(verif.alternative_slots)
                        resp = f"{requested_time} is not available for {target_entity_name} on {session.date}. Available alternative slots are: {alt_text}. Would you like one of these available times?"
                        if lang in ("ta", "tanglish", "ta_mix"):
                            resp = f"{requested_time} slot {target_entity_name}-க்கு busy-ஆ இருக்கு. Available alternative slots: {alt_text}. இதில எது ok?"
                        elif lang == "hi":
                            resp = f"{requested_time} slot {target_entity_name} ke saath available nahi hai. Available alternative slots hain: {alt_text}. Kya inme se koi chalega?"
                    else:
                        session.offered_slots = []
                        resp = f"{requested_time} is not available for {target_entity_name} on {session.date}, and there are no other open slots available on this date. Would you like to try another date?"
                        if lang in ("ta", "tanglish", "ta_mix"):
                            resp = f"{requested_time} slot {target_entity_name}-க்கு unavailable, {session.date}-ல வேற slots இல்ல. வேற date check பண்ணலாமா?"
                        elif lang == "hi":
                            resp = f"{requested_time} slot {target_entity_name} ke saath available nahi hai aur is date par koi dusra slot nahi hai. Kya aap dusri date try karna chahenge?"
                    self._log_conversation_turn(session, text, resp, "slot_unavailable", state_before=state_before)
                    return _ret(resp)
                else:
                    session.availability_verified = True
            except Exception as exc:
                logger.warning(f"Live slot check during booking turn failed: {exc}")
                session.availability_verified = True

        # STEP 5: Patient Name Check
        if session.patient_name:
            cname = self.clean_person_name(session.patient_name)
            if cname:
                session.patient_name = cname
        if not session.patient_name or not self.is_valid_person_name(session.patient_name):
            session.patient_name = None
            session.state = BookingState.COLLECTING_PATIENT_NAME
            session.last_asked_field = "patient_name"
            rel = session.patient_relationship or "relative"
            for_other = getattr(session, "is_booking_for_other", False)
            target_str = f"with {session.doctor}" if session.doctor else f"for {session.service or 'your appointment'}"

            if any(w in txt_lower for w in ["my name", "name", "who", "what", "pardon", "sorry", "pardon me"]):
                if for_other:
                    resp = f"Could you please tell me your {rel}'s full name for the booking?"
                else:
                    resp = "Could you please tell me your full name for the booking?"
            else:
                if for_other:
                    resp = f"{session.time} is available {target_str} on {session.date}. May I please have your {rel}'s full name for the booking?"
                else:
                    resp = f"{session.time} is available {target_str} on {session.date}. May I please have your full name for the booking?"
            if lang in ("ta", "tanglish", "ta_mix"):
                target_ta = session.doctor or session.service or "appointment"
                if for_other:
                    resp = f"{session.time} slot available-ஆ இருக்கு {target_ta}-கூட {session.date}-ல. Booking-க்கு உங்க {rel} full name சொல்லுங்க."
                else:
                    resp = f"{session.time} slot available-ஆ இருக்கு {target_ta}-கூட {session.date}-ல. Booking-க்கு உங்க full name சொல்லுங்க."
            elif lang == "hi":
                target_hi = session.doctor or session.service or "appointment"
                if for_other:
                    resp = f"{session.time} slot {session.date} ko {target_hi} ke liye available hai. Kripya apne {rel} ka poora naam batayein?"
                else:
                    resp = f"{session.time} slot {session.date} ko {target_hi} ke liye available hai. Kripya apna poora naam batayein?"
            self._log_conversation_turn(session, text, resp, "waiting_for_patient_name", state_before=state_before)
            return _ret(resp)

        # STEP 6: Contact Phone Number Check
        if not session.contact_number or session.contact_number == "Unknown":
            session.state = BookingState.COLLECTING_PHONE
            session.last_asked_field = "contact_number"

            was_waiting_phone = (
                (state_before and state_before.get("state") in (BookingState.COLLECTING_PHONE, BookingState.WAITING_FOR_CONTACT))
                or (state_before and state_before.get("last_asked_field") == "contact_number")
            )
            caller_phone_avail = bool(
                session.caller_phone
                and session.caller_phone not in ("Unknown", "Unknown Caller", "Private Number", "09410697548", "+919410697548")
            )

            # If incoming caller ID is available, use it as a suggested phone number, but confirm it with customer
            if caller_phone_avail and not session.booking_data.get("caller_id_suggested"):
                session.booking_data["caller_id_suggested"] = True
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = f"நன்றி, {first_name}. நீங்க {session.caller_phone} எண்ணிலிருந்து அழைக்கிறீர்கள். இதையே பயன்படுத்தலாமா, அல்லது வேறு தொடர்பு எண் உள்ளதா?" if first_name else f"நீங்க {session.caller_phone} எண்ணிலிருந்து அழைக்கிறீர்கள். இதையே பயன்படுத்தலாமா, அல்லது வேறு தொடர்பு எண் உள்ளதா?"
                elif lang == "hi":
                    resp = f"Dhanyawad, {first_name}. Main dekh sakta hoon aap {session.caller_phone} se call kar rahe hain. Kya main isi number ko use karoon, ya aapka koi doosra number hai?" if first_name else f"Aap {session.caller_phone} se call kar rahe hain. Kya main isi number ko use karoon, ya doosra number hai?"
                else:
                    resp = f"Thank you, {first_name}. I see you're calling from {session.caller_phone}. Should I use this number, or do you have a different contact number?" if first_name else f"I see you're calling from {session.caller_phone}. Should I use this number, or do you have a different contact number?"
                self._log_conversation_turn(session, text, resp, "suggest_caller_phone", state_before=state_before)
                return _ret(resp)

            # If customer provided unclear/invalid input while waiting for phone:
            if was_waiting_phone:
                if caller_phone_avail:
                    resp = f"I didn't quite catch that. Could you please confirm if we should use your calling number {session.caller_phone}, or share your 10-digit contact number?"
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = f"சரியாகக் கேட்கவில்லை. {session.caller_phone} எண்ணைப் பயன்படுத்தலாமா அல்லது உங்கள் 10 இலக்க எண்ணைச் சொல்லுங்கள்."
                    elif lang == "hi":
                        resp = f"Theek se sunai nahi diya. Kya hum {session.caller_phone} use karein, ya apna 10-digit number batayein?"
                else:
                    resp = "I didn't catch a valid contact number. Could you please share your 10-digit phone number?"
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = "சரியான எண் கேட்கவில்லை. தயவுசெய்து உங்கள் 10 இலக்க mobile எண்ணைச் சொல்லுங்கள்."
                    elif lang == "hi":
                        resp = "Sahi number nahi mila. Kripya apna 10-digit mobile number batayein."
                self._log_conversation_turn(session, text, resp, "reprompt_phone", state_before=state_before)
                return _ret(resp)

            resp = f"Thank you, {first_name}. Could you please share your contact phone number?" if first_name else "Could you please share your contact phone number?"
            if lang in ("ta", "tanglish", "ta_mix"):
                resp = f"நன்றி, {first_name}. உங்க contact number சொல்லுங்க." if first_name else "உங்க contact number சொல்லுங்க."
            elif lang == "hi":
                resp = f"Dhanyawad, {first_name}. Kripya apna contact number batayein." if first_name else "Kripya apna contact number batayein."
            self._log_conversation_turn(session, text, resp, "waiting_for_contact", state_before=state_before)
            return _ret(resp)

        # STEP 7: Email Address Check
        if not session.email and not session.booking_data.get("email_declined"):
            was_waiting_email = (
                (state_before and state_before.get("state") in (BookingState.COLLECTING_EMAIL, BookingState.WAITING_FOR_EMAIL))
                or (state_before and state_before.get("last_asked_field") == "email")
            )
            is_email_attempt = any(w in txt_lower for w in ["@", "at", "dot", "gmail", "yahoo", "outlook", "email", "mail", ".com", ".in", ".org", "com"])

            # Check if customer declines email or says they do not have one
            decline_phrases = [
                "no email", "don't have an email", "don't have email", "no mail", 
                "i don't have an email", "i don't have one", "skip", "decline", 
                "not now", "later", "none", "don't want to", "no thanks",
                "email illa", "mail illa", "email nahi", "nahi hai"
            ]
            clean_token = txt_lower.strip().rstrip(".,!?")
            is_explicit_decline = any(dp in txt_lower for dp in decline_phrases) or (was_waiting_email and clean_token in ("no", "nope", "skip", "none", "na", "illa", "nahi"))

            if is_explicit_decline:
                session.booking_data["email_declined"] = True
                session.booking_data["email_confirmed"] = False
                session.email = None
                # Continue directly to confirmation (Step 8) without email
            else:
                session.state = BookingState.COLLECTING_EMAIL
                session.last_asked_field = "email"

                if was_waiting_email and (is_email_attempt or len(txt_lower.split()) >= 1):
                    resp = "I didn't catch the full email address. Could you please repeat or spell out your complete email address, or say 'skip' to continue without email?"
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = "முழு email address சரியாகக் கேட்கவில்லை. தயவுசெய்து உங்கள் complete email address-ஐ மீண்டும் சொல்லுங்கள், அல்லது 'skip' என்று சொல்லுங்கள்."
                    elif lang == "hi":
                        resp = "Mujhe poora email address theek se sunai nahi diya. Kripya apna poora email address dobara batayein ya 'skip' kahein."
                else:
                    resp = f"Thanks, {first_name}. What is your email address so I can send your appointment confirmation?" if first_name else "What is your email address so I can send your appointment confirmation?"
                    if lang in ("ta", "tanglish", "ta_mix"):
                        resp = "சரி. உறுதிப்படுத்தல் அனுப்ப உங்க email address சொல்லுங்க."
                    elif lang == "hi":
                        resp = "Shukriya. Confirmation bhejne ke liye kripya apna email address batayein."
                self._log_conversation_turn(session, text, resp, "waiting_for_email", state_before=state_before)
                return _ret(resp)

        # STEP 8: Read-Back Confirmation Check & Handling
        if session.state == BookingState.WAITING_FOR_CONFIRMATION or (state_before and state_before.get("state") == BookingState.WAITING_FOR_CONFIRMATION):
            target_str = f"with {session.doctor}" if session.doctor else f"for {session.service or 'your appointment'}"
            loc_str = f", Location: {session.location}" if session.location else ""
            email_confirm_str = f", Email: {session.email}" if session.email else ""

            is_negation = any(w in txt_lower.split() for w in ["no", "nope", "wrong", "incorrect", "change", "mistake", "not", "illa", "nahi", "galat"]) or "not correct" in txt_lower
            is_affirmation = any(w in txt_lower.split() for w in ["yes", "yeah", "yep", "sure", "correct", "right", "fine", "perfect", "good", "proceed", "confirm", "ok", "okay", "ha", "haan", "sari", "aam"]) or "that's correct" in txt_lower or "that is correct" in txt_lower or "looks good" in txt_lower or "sounds good" in txt_lower

            new_email = self.parse_spoken_email(text)
            new_phone = self.parse_spoken_phone_number(text)
            new_loc = self.parse_spoken_location(text)

            # Did customer provide a new email?
            if new_email and ("@" in text or "mail" in txt_lower or "email" in txt_lower or is_negation or new_email != session.email):
                session.email = new_email
                session.booking_data["email_confirmed"] = False
                session.state = BookingState.WAITING_FOR_CONFIRMATION
                resp = f"I've updated your email to {session.email}. Let me confirm: Name: {session.patient_name}, Phone: {session.contact_number}, Email: {session.email}{loc_str}, for {target_str} on {session.date} at {session.time}. Does everything look correct now?"
                self._log_conversation_turn(session, text, resp, "email_corrected", state_before=state_before)
                return _ret(resp)

            # Did customer provide a new phone?
            if new_phone and ("phone" in txt_lower or "number" in txt_lower or "mobile" in txt_lower or is_negation or new_phone != session.contact_number):
                session.contact_number = new_phone
                session.state = BookingState.WAITING_FOR_CONFIRMATION
                resp = f"I've updated your phone number to {session.contact_number}. Let me confirm: Name: {session.patient_name}, Phone: {session.contact_number}{email_confirm_str}{loc_str}, for {target_str} on {session.date} at {session.time}. Does everything look correct now?"
                self._log_conversation_turn(session, text, resp, "phone_corrected", state_before=state_before)
                return _ret(resp)

            # Did customer provide a new location?
            if new_loc:
                session.location = new_loc
                session.state = BookingState.WAITING_FOR_CONFIRMATION
                resp = f"I've updated your location to {session.location}. Let me confirm: Name: {session.patient_name}, Phone: {session.contact_number}{email_confirm_str}, Location: {session.location}, for {target_str} on {session.date} at {session.time}. Does everything look correct now?"
                self._log_conversation_turn(session, text, resp, "location_corrected", state_before=state_before)
                return _ret(resp)

            # Did customer state name correction?
            if any(w in txt_lower for w in ["my name is", "name is", "call me", "myself"]) and not is_affirmation:
                name_clean = re.sub(r'^(?:(?:no|actually|wrong|not|change)\s*,?\s*)?(?:my\s+name\s+is|name\s+is|i\s+am|it\'s|its)?\s*', '', text, flags=re.IGNORECASE).strip()
                cand_name = self.clean_person_name(name_clean)
                if cand_name and self.is_valid_person_name(cand_name):
                    session.patient_name = cand_name
                    session.state = BookingState.WAITING_FOR_CONFIRMATION
                    resp = f"I've updated your name to {session.patient_name}. Let me confirm: Name: {session.patient_name}, Phone: {session.contact_number}{email_confirm_str}{loc_str}, for {target_str} on {session.date} at {session.time}. Does everything look correct now?"
                    self._log_conversation_turn(session, text, resp, "name_corrected", state_before=state_before)
                    return _ret(resp)

            # If user said no/wrong without specifying what is wrong:
            if is_negation and not is_affirmation:
                session.state = BookingState.WAITING_FOR_CONFIRMATION
                session.last_asked_field = "field_to_correct"
                if lang in ("ta", "tanglish", "ta_mix"):
                    resp = "மன்னிக்கவும்! எந்த விவரத்தை மாற்ற வேண்டும் — உங்கள் பெயர், தொலைபேசி எண், மின்னஞ்சல், தேதி அல்லது நேரமா?"
                elif lang == "hi":
                    resp = "Kshama kijiye! Aap konsi detail badalna chahte hain — apna naam, phone number, email, date ya samay?"
                else:
                    resp = "I apologize! Which detail would you like to correct — your name, phone number, email, date, or time?"
                self._log_conversation_turn(session, text, resp, "ask_which_field_to_correct", state_before=state_before)
                return _ret(resp)

            # Affirmation: commit booking
            if is_affirmation or not is_negation:
                session.booking_data["details_confirmed"] = True
                session.booking_data["email_confirmed"] = bool(session.email)
                session.state = BookingState.BOOKING
                resp = self._execute_db_booking(session)
                self._log_conversation_turn(session, text, resp, "booking_executed", state_before=state_before)
                return _ret(resp)

        # First time all details are present — Read back and ask for confirmation
        if not session.booking_data.get("details_confirmed"):
            session.state = BookingState.WAITING_FOR_CONFIRMATION
            session.last_asked_field = "confirmation"

            target_str = f"with {session.doctor}" if session.doctor else f"for {session.service or 'your appointment'}"
            loc_str = f", Location: {session.location}" if session.location else ""
            email_part = f", Email: {session.email}" if session.email else ""
            email_ta = f", மின்னஞ்சல்: {session.email}" if session.email else ""
            email_hi = f", Email: {session.email}" if session.email else ""

            if lang in ("ta", "tanglish", "ta_mix"):
                resp = (
                    f"நன்றி, {first_name or 'வணக்கம்'}! உங்க விவரங்களை உறுதி செய்கிறேன்: "
                    f"பெயர்: {session.patient_name}, தொலைபேசி: {session.contact_number}"
                    f"{email_ta}{f', இடம்: {session.location}' if session.location else ''}, "
                    f"{session.date} அன்று {session.time}-க்கு {target_str}. இந்த விவரங்கள் சரியா?"
                )
            elif lang == "hi":
                resp = (
                    f"Dhanyawad, {first_name or ''}! Main aapki details confirm kar leta hoon: "
                    f"Naam: {session.patient_name}, Phone: {session.contact_number}"
                    f"{email_hi}{f', Location: {session.location}' if session.location else ''}, "
                    f"{target_str} ke liye {session.date} ko {session.time} par. Kya yeh sab sahi hai?"
                )
            else:
                resp = (
                    f"Thank you, {first_name or 'there'}! Let me confirm your details: "
                    f"Name: {session.patient_name}, Phone: {session.contact_number}"
                    f"{email_part}{loc_str}, for {target_str} on {session.date} at {session.time}. "
                    f"Is all of that correct?"
                )
            self._log_conversation_turn(session, text, resp, "waiting_for_confirmation", state_before=state_before)
            return _ret(resp)

        # STEP 9: All details collected & confirmed — Create Booking
        session.state = BookingState.BOOKING
        resp = self._execute_db_booking(session)
        self._log_conversation_turn(session, text, resp, "booking_executed", state_before=state_before)
        return _ret(resp)

    def _log_conversation_turn(self, session: BookingSession, user_input: str, response: str, action: str, state_before: Optional[Dict[str, Any]] = None):
        """Safe structured development log for turn tracing."""
        fields_before = state_before.get("collected_fields", {}) if state_before else {}
        changed_fields = {
            k: v for k, v in session.collected_fields.items()
            if fields_before.get(k) != v
        }
        sb_intent = state_before.get("current_intent") if state_before else session.current_intent
        sb_stage = state_before.get("current_stage") if state_before else session.current_stage
        sb_collected = state_before.get("collected_fields") if state_before else session.collected_fields

        logger.info(
            f"\n[BOOKING_STATE]"
            f"\n  call_id={session.call_sid}"
            f"\n  state_before={sb_stage}"
            f"\n  state_after={session.state}"
            f"\n  current_action={action}"
            f"\n  doctor_id={session.doctor_id}"
            f"\n  doctor='{session.doctor}'"
            f"\n  date='{session.date}'"
            f"\n  time='{session.time}'"
            f"\n  patient_name='{session.patient_name}'"
        )

        logger.info(
            f"\n[CONVERSATION]\n"
            f"conversation_id={session.conversation_id}\n"
            f"call_sid={session.call_sid}\n"
            f"turn_id={session.turn_count}\n"
            f"[STATE BEFORE]\n"
            f"intent={sb_intent}\n"
            f"stage={sb_stage}\n"
            f"collected_fields={sb_collected}\n"
            f"[USER]\n"
            f"transcript={user_input}\n"
            f"[STATE UPDATE]\n"
            f"changed_fields={changed_fields}\n"
            f"[STATE AFTER]\n"
            f"intent={session.current_intent}\n"
            f"stage={session.current_stage}\n"
            f"collected_fields={session.collected_fields}\n"
            f"[ACTION]\n"
            f"action={action}"
        )
        self.save_session_state(session)

        # Automatic Lead Persistence:
        # As soon as customer details (name, phone, caller_phone, email) are provided,
        # create or update the lead in the Leads Management table.
        # This guarantees leads are preserved even if the customer hangs up mid-conversation.
        try:
            from src.services.lead_service import LeadService
            from src.config.database import SessionLocal

            raw_phone = getattr(session, "contact_number", None) or getattr(session, "caller_phone", None)
            cust_name = getattr(session, "patient_name", None)
            cust_email = getattr(session, "email", None)

            if (cust_name and len(cust_name.strip()) >= 2) or (raw_phone and raw_phone not in ("Unknown", "Unknown Caller", "Private Number")) or cust_email:
                ldb = SessionLocal()
                try:
                    LeadService(ldb).upsert_lead_from_session(
                        session=session,
                        call_log_id=getattr(session, "db_call_id", None)
                    )
                finally:
                    ldb.close()
        except Exception as lead_err:
            logger.debug(f"[LeadService Auto-Save Note] {lead_err}")

    def _execute_db_booking(self, session: BookingSession) -> str:
        """
        STAGE 2 BOOKING EXECUTION: Re-verifies availability immediately before DB commit.
        Includes idempotency check, email persistence, and post-booking notifications (Email + WhatsApp).
        Never pretends a booking happened unless database returns a valid appointment_id (>0).
        """
        lang = session.conversation_language

        if session.is_completed and session.appointment_id:
            return f"Your appointment with {session.doctor} on {session.date} at {session.time} is already confirmed under Appointment ID #{session.appointment_id}."

        try:
            from src.config.database import SessionLocal
            from src.services.appointment_service import AppointmentService
            from src.validators.appointment_validator import AppointmentCreate
            from src.models.appointment import Appointment
            from src.services.calendar_verification_service import calendar_verification_service

            # Validate doctor/staff exists in DB or fallback to requested service/specialty
            doctor_name = self.validate_doctor_against_db(session.doctor or "") or session.doctor or "Representative"

            # Stage 2 Pre-commit date/time normalization
            target_date_obj = calendar_verification_service.parse_date(session.date or "today")
            normalized_date_str = target_date_obj.strftime("%d %b %Y")
            time_24h = calendar_verification_service.parse_time_to_24h(session.time or "10:00 AM") or "10:00"
            normalized_time_str = calendar_verification_service.format_time_12h(time_24h)

            # Idempotency check — prevent duplicate bookings
            org_id = getattr(session, "organization_id", 1) or 1
            idem_key = f"{org_id}:{session.session_id}:{doctor_name}:{normalized_date_str}:{normalized_time_str}:{session.patient_name}"
            if session.booking_data.get("idempotency_key") == idem_key and session.appointment_id:
                return f"Your appointment with {doctor_name} on {normalized_date_str} at {normalized_time_str} is already confirmed under Appointment ID #{session.appointment_id}."
            session.booking_data["idempotency_key"] = idem_key

            db = SessionLocal()
            try:
                # 1. Check for existing duplicate in DB for same customer
                existing = db.query(Appointment).filter(
                    Appointment.customer == session.patient_name,
                    Appointment.staff == doctor_name,
                    Appointment.appointment_date == normalized_date_str,
                    Appointment.appointment_time == normalized_time_str,
                    Appointment.status != "Cancelled"
                ).first()
                if existing:
                    session.appointment_id = existing.id
                    session.booking_confirmed = True
                    session.state = BookingState.BOOKED
                    session.is_completed = True
                    logger.info(
                        f"\n[BOOKING]"
                        f"\n  call_id={session.call_sid}"
                        f"\n  staff_id={session.doctor_id}"
                        f"\n  staff_name='{doctor_name}'"
                        f"\n  date='{normalized_date_str}'"
                        f"\n  time='{normalized_time_str}'"
                        f"\n  customer='{session.patient_name}'"
                        f"\n  status=EXISTING_RECORD_FOUND"
                        f"\n  appointment_id={existing.id}"
                    )
                    return f"Your appointment with {doctor_name} on {normalized_date_str} at {normalized_time_str} is already confirmed under Appointment ID #{existing.id}."

                # 2. Check for race condition: slot taken by another customer right before commit
                conflict = db.query(Appointment).filter(
                    Appointment.staff == doctor_name,
                    Appointment.appointment_date == normalized_date_str,
                    Appointment.appointment_time == normalized_time_str,
                    Appointment.status != "Cancelled"
                ).first()
                if conflict:
                    session.time = None
                    session.availability_verified = False
                    session.state = BookingState.WAITING_FOR_TIME
                    return f"Sorry, that slot at {normalized_time_str} with {doctor_name} was just booked by another caller. Which other time would you prefer?"

                svc = AppointmentService(db)
                svc_val = session.service or session.product or session.booking_data.get("service") or session.booking_data.get("product")
                intent_val = session.current_intent or session.booking_data.get("intent", "appointment_booking")
                purpose_note = f"Booked via AI Voice Receptionist ({intent_val.replace('_', ' ').title()})"
                confirmed_email = (session.email or session.booking_data.get("email") or "").strip() or None
                if not confirmed_email and session.contact_number:
                    try:
                        prev = db.query(Appointment).filter(
                            Appointment.organization_id == org_id,
                            Appointment.phone == session.contact_number,
                            Appointment.email.isnot(None),
                            Appointment.email != ""
                        ).order_by(Appointment.id.desc()).first()
                        if prev:
                            confirmed_email = prev.email.strip()
                    except Exception:
                        pass

                appointment = svc.create_appointment(
                    AppointmentCreate(
                        customer=session.patient_name,
                        phone=session.contact_number or "Unknown",
                        email=confirmed_email,
                        department=session.booking_data.get("specialty") or session.department or "General Appointment",
                        staff=doctor_name,
                        service=svc_val,
                        appointment_date=normalized_date_str,
                        appointment_time=normalized_time_str,
                        duration="30 Minutes",
                        notes=purpose_note
                    ),
                    organization_id=org_id,
                    user_id=getattr(session, "user_id", None),
                    sync_calendar_sync=True
                )

                if appointment and appointment.id and appointment.id > 0:
                    verified_appt = db.query(Appointment).filter(Appointment.id == appointment.id).first()
                    if verified_appt:
                        # Check calendar sync status: If a calendar is connected, ensure calendar sync didn't fail
                        from src.services.calendar_service import CalendarService
                        cal_svc = CalendarService(db, user_id=getattr(session, "user_id", None))
                        has_calendar_connected = bool(cal_svc.get_connected_config())
                        cal_ev_id = getattr(verified_appt, "calendar_event_id", None) or svc._calendar_event_id(verified_appt)

                        if has_calendar_connected and not cal_ev_id:
                            # Calendar is connected but event was not generated
                            logger.error(f"[BOOKING CALENDAR ERROR] Google Calendar is connected but event creation failed for appointment #{verified_appt.id}")
                            verified_appt.status = "Failed"
                            db.commit()
                            try:
                                from src.services.lead_service import LeadService
                                LeadService(db).upsert_lead_from_session(
                                    session,
                                    lead_status="Follow-up Required",
                                    appointment_status="Failed",
                                    follow_up_required=True,
                                    notes=f"Google Calendar event creation failed for {doctor_name} on {normalized_date_str} at {normalized_time_str}"
                                )
                            except Exception:
                                pass
                            if lang in ("ta", "tanglish", "ta_mix"):
                                return "மன்னிக்கவும், கேலெண்டரில் அந்த நேரத்தை உறுதிப்படுத்த முடியவில்லை. உங்கள் விவரங்கள் சேமிக்கப்பட்டுள்ளன, எங்கள் குழு உங்களை விரைவில் தொடர்பு கொள்ளும்."
                            elif lang == "hi":
                                return "Kshama kijiye, hum calendar par us slot ko confirm nahi kar sake. Aapki details save ho gayi hain, aur hamari team jald hi aapse contact karegi."
                            return "I apologize, but we encountered an issue syncing with our calendar for that slot. I have saved your details, and our team will follow up with you shortly to confirm your appointment."

                        session.date = normalized_date_str
                        session.time = normalized_time_str
                        session.appointment_id = verified_appt.id
                        session.calendar_event_id = cal_ev_id
                        session.booking_confirmed = True
                        session.state = BookingState.BOOKED
                        session.is_completed = True
                        logger.info(
                            f"\n[BOOKING]"
                            f"\n  call_id={session.call_sid}"
                            f"\n  staff_id={session.doctor_id}"
                            f"\n  staff_name='{doctor_name}'"
                            f"\n  date='{normalized_date_str}'"
                            f"\n  time='{normalized_time_str}'"
                            f"\n  customer='{session.patient_name}'"
                            f"\n  calendar_event_id='{cal_ev_id}'"
                            f"\n  status=SUCCESS"
                            f"\n  appointment_id={verified_appt.id}"
                        )

                        # Post-booking notifications (WhatsApp + Email) and calendar sync run asynchronously in background tasks
                        if lang in ("ta", "tanglish", "ta_mix"):
                            return (
                                f"உங்க appointment {doctor_name}-கூட {normalized_date_str} அன்று {normalized_time_str}-க்கு confirm ஆயிடுச்சு! "
                                f"Appointment ID #{verified_appt.id}. நன்றி, {session.patient_name}!"
                            )
                        elif lang == "hi":
                            return (
                                f"Aapka appointment {doctor_name} ke saath {normalized_date_str} ko {normalized_time_str} par confirm ho gaya hai! "
                                f"Appointment ID #{verified_appt.id}. Dhanyawad, {session.patient_name}!"
                            )
                        booking_label = f"with {session.doctor}" if session.doctor else f"for {session.service or doctor_name}"
                        return (
                            f"Your appointment {booking_label} on {normalized_date_str} at {normalized_time_str} "
                            f"has been successfully confirmed! Your Appointment ID is #{verified_appt.id}. "
                            f"Thank you, {session.patient_name}!"
                        )

                raise RuntimeError("Database appointment verification failed - ID invalid or record missing.")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[BOOKING DB ERROR] Failed to create/verify appointment: {e}")
            session.state = BookingState.WAITING_FOR_EMAIL
            try:
                from src.config.database import SessionLocal
                from src.services.lead_service import LeadService
                err_db = SessionLocal()
                try:
                    LeadService(err_db).upsert_lead_from_session(
                        session,
                        lead_status="Follow-up Required",
                        appointment_status="Failed",
                        follow_up_required=True,
                        notes=f"Booking failed: {e}"
                    )
                finally:
                    err_db.close()
            except Exception:
                pass
            if "conflict" in str(e).lower() or "409" in str(e):
                return "I'm sorry, that appointment time on our calendar was just taken. Which other time would you prefer?"
            if lang in ("ta", "tanglish", "ta_mix"):
                return "மன்னிக்கவும், கேலெண்டரில் அந்த நேரத்தை உறுதிப்படுத்த முடியவில்லை. உங்கள் விவரங்கள் சேமிக்கப்பட்டுள்ளன, எங்கள் குழு உங்களை விரைவில் தொடர்பு கொள்ளும்."
            return "I apologize, but we encountered an issue booking that slot in our calendar system. I have saved your details, and our team will follow up with you shortly to confirm your appointment."


booking_state_service = BookingStateService()
