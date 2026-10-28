"""
Backend Function & Tool Execution Service for Grok and Receptionist Logic.

Contains controlled, verified backend functions that Grok or the receptionist
orchestrator call. Grok cannot directly mutate the database or invent information.
All data mutations and lookups pass through these validated functions.
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional

from src.config.database import SessionLocal
from src.models.staff import Staff, StaffStatus
from src.models.appointment import Appointment, AppointmentStatus
from src.services.calendar_verification_service import calendar_verification_service
from src.services.rag_service import rag_service

logger = logging.getLogger(__name__)


def get_doctors(organization_id: int = 1) -> List[Dict[str, Any]]:
    """Retrieve all verified staff, representatives, or specialists available at the organization."""
    # Check if custom active RAG document is loaded
    active_doc_id = rag_service._resolve_active_document_id(organization_id)
    if active_doc_id:
        master_ctx = rag_service.get_organization_master_context(organization_id, document_id=active_doc_id)
        ctx_lower = master_ctx.lower() if master_ctx else ""
        has_medical = any(w in ctx_lower for w in ["doctor", "dr.", "physician", "cardiologist", "hospital", "clinic"])
        if not has_medical:
            db = SessionLocal()
            try:
                staff_records = db.query(Staff).filter(
                    Staff.organization_id == organization_id,
                    Staff.status != StaffStatus.OFFLINE
                ).all()
                non_medical_staff = [s for s in staff_records if not any(dw in s.name.lower() or dw in (s.department or "").lower() for dw in ["dr.", "doctor", "cardiology", "medicine"])]
                if non_medical_staff:
                    return [{
                        "id": s.id,
                        "name": s.name,
                        "department": s.department,
                        "working_hours": s.working_hours or f"{s.start_time}-{s.end_time}",
                        "appointment_duration": s.appointment_duration,
                        "status": s.status.value if hasattr(s.status, "value") else str(s.status)
                    } for s in non_medical_staff]
                logger.info("[TOOL EXEC] get_doctors() active RAG is non-medical. Suppressing legacy hospital seed staff.")
                return []
            finally:
                db.close()

    db = SessionLocal()
    try:
        staff_records = db.query(Staff).filter(
            Staff.organization_id == organization_id,
            Staff.status != StaffStatus.OFFLINE
        ).all()

        if not staff_records:
            return []

        result = []
        for s in staff_records:
            result.append({
                "id": s.id,
                "name": s.name,
                "department": s.department,
                "working_hours": s.working_hours or f"{s.start_time}-{s.end_time}",
                "appointment_duration": s.appointment_duration,
                "status": s.status.value if hasattr(s.status, "value") else str(s.status)
            })
        logger.info(f"[TOOL EXEC] get_doctors() returned {len(result)} records for org {organization_id}.")
        return result
    except Exception as e:
        logger.error(f"[TOOL ERROR] get_doctors failed: {e}")
        return []
    finally:
        db.close()


def get_departments(organization_id: int = 1) -> List[str]:
    """Retrieve all active departments from database or RAG knowledge base."""
    active_doc_id = rag_service._resolve_active_document_id(organization_id)
    if active_doc_id:
        master_ctx = rag_service.get_organization_master_context(organization_id, document_id=active_doc_id)
        ctx_lower = master_ctx.lower() if master_ctx else ""
        has_medical = any(w in ctx_lower for w in ["doctor", "dr.", "physician", "cardiologist", "hospital", "clinic"])
        if not has_medical:
            db = SessionLocal()
            try:
                departments = db.query(Staff.department).filter(
                    Staff.organization_id == organization_id
                ).distinct().all()
                depts = [d[0] for d in departments if d[0] and not any(dw in d[0].lower() for dw in ["cardiology", "medicine"])]
                if depts:
                    return depts
                rag_depts = rag_service.retrieve_context("Departments specialties divisions services categories", n_results=2, organization_id=organization_id)
                if rag_depts and "No relevant" not in rag_depts:
                    return [rag_depts]
                return []
            finally:
                db.close()

    db = SessionLocal()
    try:
        departments = db.query(Staff.department).filter(
            Staff.organization_id == organization_id
        ).distinct().all()

        depts = [d[0] for d in departments if d[0]]
        if not depts:
            rag_depts = rag_service.retrieve_context("Departments specialties divisions", n_results=2, organization_id=organization_id)
            if rag_depts:
                logger.info("[TOOL EXEC] get_departments() retrieved from RAG context.")
                return [rag_depts]
        logger.info(f"[TOOL EXEC] get_departments() returned {depts}")
        return depts
    except Exception as e:
        logger.error(f"[TOOL ERROR] get_departments failed: {e}")
        return []
    finally:
        db.close()


def get_doctor_information(doctor_name: str, organization_id: int = 1) -> Dict[str, Any]:
    """Look up verified details for a specific staff member or doctor from DB or RAG."""
    db = SessionLocal()
    try:
        doctor_name_clean = doctor_name.strip()
        staff = db.query(Staff).filter(
            Staff.organization_id == organization_id,
            Staff.name.ilike(f"%{doctor_name_clean}%")
        ).first()

        # Strict organization isolation: only query within organization_id

        if staff:
            return {
                "found": True,
                "id": staff.id,
                "name": staff.name,
                "department": staff.department,
                "email": staff.email,
                "working_hours": staff.working_hours or f"{staff.start_time}-{staff.end_time}",
                "services": staff.services,
                "status": staff.status.value if hasattr(staff.status, "value") else str(staff.status)
            }

        # Search RAG as fallback for doctor information
        rag_info = rag_service.retrieve_context(f"Dr {doctor_name_clean} specialization department information", n_results=2, organization_id=organization_id)
        if rag_info and "No relevant" not in rag_info:
            return {
                "found": True,
                "name": doctor_name_clean,
                "details": rag_info
            }

        return {
            "found": False,
            "message": f"Staff member or doctor '{doctor_name}' was not found in our active records."
        }
    except Exception as e:
        logger.error(f"[TOOL ERROR] get_doctor_information failed: {e}")
        return {"found": False, "message": str(e)}
    finally:
        db.close()


def search_knowledge(query: str, organization_id: int = 1) -> str:
    """Search static RAG knowledge base for organization policies, services, pricing, and FAQs."""
    try:
        res = rag_service.retrieve_context(query, n_results=3, organization_id=organization_id)
        logger.info(f"[TOOL EXEC] search_knowledge('{query}') completed.")
        return res
    except Exception as e:
        logger.error(f"[TOOL ERROR] search_knowledge failed: {e}")
        return "Organization knowledge base query unavailable."


def get_available_slots(doctor_name: Optional[str] = None, date_str: Optional[str] = None, organization_id: int = 1) -> Dict[str, Any]:
    """Retrieve verified live open slots for a doctor & date from appointment DB."""
    try:
        from src.routes.availability_routes import get_available_slots as fetch_slots
        db = SessionLocal()
        try:
            staff = None
            if doctor_name:
                staff = db.query(Staff).filter(
                    Staff.organization_id == organization_id,
                    Staff.name.ilike(f"%{doctor_name.strip()}%")
                ).first()
                if not staff:
                    return {
                        "found": False,
                        "doctor_name": doctor_name,
                        "available_slots": [],
                        "message": f"I couldn't find '{doctor_name}' in our staff directory for this organization. Could you please specify who you would like to book with?"
                    }
            else:
                staff = db.query(Staff).filter(Staff.organization_id == organization_id).first()

            if not staff:
                return {
                    "doctor_name": doctor_name,
                    "available_slots": [],
                    "message": "No staff member available."
                }

            target_date = calendar_verification_service.parse_date(date_str or "today")
            res = fetch_slots(staff_id=staff.id, selected_date=target_date, db=db)
            raw_slots = res.get("available_slots", [])

            formatted_slots = [calendar_verification_service.format_time_12h(s["start"]) for s in raw_slots]
            logger.info(f"[TOOL EXEC] get_available_slots({staff.name}, {target_date}) returned {len(formatted_slots)} slots.")
            return {
                "doctor_name": staff.name,
                "department": staff.department,
                "date": target_date.strftime("%Y-%m-%d"),
                "date_formatted": target_date.strftime("%A, %b %d, %Y"),
                "available_slots": formatted_slots[:6]
            }
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[TOOL ERROR] get_available_slots failed: {e}")
        return {"doctor_name": doctor_name, "available_slots": [], "error": str(e)}


def check_availability(doctor_name: str, date_str: str, time_str: str, organization_id: int = 1) -> Dict[str, Any]:
    """Verify live exact slot availability for doctor + date + time."""
    query = f"Appointment with {doctor_name} on {date_str} at {time_str}"
    verif = calendar_verification_service.verify_availability(query)
    return {
        "is_checked": verif.is_checked,
        "is_available": verif.is_available,
        "doctor_name": verif.doctor_name or doctor_name,
        "requested_date": verif.requested_date or date_str,
        "requested_time": verif.requested_time or time_str,
        "alternative_slots": verif.alternative_slots,
        "reason": verif.reason
    }


def book_appointment(patient_name: str, phone: str, doctor_name: str, date_str: str, time_str: str, department: Optional[str] = None, organization_id: int = 1) -> Dict[str, Any]:
    """
    Executes live appointment booking in DB after doctor validation and slot re-verification.
    Never confirms unless DB insertion succeeds.
    """
    db = SessionLocal()
    try:
        # 1. Validate doctor against database staff records
        staff = db.query(Staff).filter(
            Staff.name.ilike(f"%{doctor_name.strip()}%")
        ).first()

        if not staff:
            logger.warning(f"[BOOKING REJECTED] Doctor/Staff '{doctor_name}' does not exist in DB records.")
            return {
                "success": False,
                "reason": f"Staff member or doctor '{doctor_name}' was not found in our current directory records. Please confirm the name or department.",
                "appointment_id": None
            }

        matched_doctor_name = staff.name
        matched_department = department or staff.department

        # Normalize date & time strings
        target_date = calendar_verification_service.parse_date(date_str or "today")
        formatted_date = target_date.strftime("%d %b %Y")
        time_24h = calendar_verification_service.parse_time_to_24h(time_str or "10:00 AM") or "10:00"
        formatted_time = calendar_verification_service.format_time_12h(time_24h)

        # 2. Check if identical appointment is already booked for this caller (duplicate prevention)
        existing_appt = db.query(Appointment).filter(
            Appointment.staff.ilike(f"%{matched_doctor_name}%"),
            Appointment.appointment_date == formatted_date,
            Appointment.appointment_time == formatted_time,
            Appointment.status != AppointmentStatus.CANCELLED
        ).first()

        if existing_appt and (
            (phone and existing_appt.phone and phone.strip() == existing_appt.phone.strip())
            or (patient_name and existing_appt.customer and patient_name.strip().lower() == existing_appt.customer.strip().lower())
        ):
            logger.info(f"[BOOKING NOTICE] Appointment #{existing_appt.id} already exists for {patient_name}.")
            return {
                "success": True,
                "appointment_id": existing_appt.id,
                "doctor_name": matched_doctor_name,
                "department": matched_department,
                "patient_name": patient_name,
                "date": formatted_date,
                "time": formatted_time,
                "status": str(existing_appt.status.value if hasattr(existing_appt.status, 'value') else existing_appt.status),
                "message": f"Appointment is already confirmed for {patient_name} with {matched_doctor_name} on {formatted_date} at {formatted_time}."
            }

        # 3. Check slot availability
        check_query = f"Appointment with {matched_doctor_name} on {formatted_date} at {formatted_time}"
        verif = calendar_verification_service.verify_availability(check_query)

        if verif.is_checked and verif.is_available is False:
            if verif.alternative_slots:
                alt_text = ", ".join(verif.alternative_slots)
                reason_msg = f"Slot {formatted_time} is no longer available on {formatted_date}. Available slots: {alt_text}"
            else:
                reason_msg = f"Slot {formatted_time} is no longer available on {formatted_date}, and no other slots are open on this date."
            return {
                "success": False,
                "reason": reason_msg,
                "alternative_slots": verif.alternative_slots,
                "appointment_id": None
            }

        # 3. Create appointment record in DB
        from src.services.appointment_service import AppointmentService
        from src.validators.appointment_validator import AppointmentCreate

        svc = AppointmentService(db)
        appt = svc.create_appointment(AppointmentCreate(
            customer=patient_name,
            phone=phone or "Unknown",
            department=matched_department,
            staff=matched_doctor_name,
            appointment_date=formatted_date,
            appointment_time=formatted_time,
            duration="30 Minutes",
            notes="Booked via AI Receptionist xAI Tool Pipeline"
        ))

        if appt and appt.id and appt.id > 0:
            verified_appt = db.query(Appointment).filter(Appointment.id == appt.id).first()
            if verified_appt:
                logger.info(f"[BOOKING SUCCESS] Appointment ID #{verified_appt.id} created for {patient_name} with {matched_doctor_name} on {formatted_date} at {formatted_time}")
                return {
                    "success": True,
                    "appointment_id": verified_appt.id,
                    "customer_id": verified_appt.customer_id,
                    "doctor_name": matched_doctor_name,
                    "department": matched_department,
                    "patient_name": patient_name,
                    "date": formatted_date,
                    "time": formatted_time,
                    "status": "Confirmed"
                }

        return {
            "success": False,
            "reason": "Database verification failed to commit appointment record.",
            "appointment_id": None
        }
    except Exception as e:
        logger.error(f"[BOOKING TOOL ERROR] Failed to book appointment: {e}")
        return {
            "success": False,
            "reason": f"Appointment booking failed: {str(e)}",
            "appointment_id": None
        }
    finally:
        db.close()


def cancel_appointment(appointment_id: int) -> Dict[str, Any]:
    """Cancel an appointment by ID."""
    db = SessionLocal()
    try:
        appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appt:
            return {"success": False, "message": f"Appointment #{appointment_id} not found."}
        
        appt.status = AppointmentStatus.CANCELLED
        db.commit()
        logger.info(f"[TOOL EXEC] Cancelled Appointment #{appointment_id}")
        return {"success": True, "message": f"Appointment #{appointment_id} has been successfully cancelled."}
    except Exception as e:
        db.rollback()
        logger.error(f"[TOOL ERROR] cancel_appointment failed: {e}")
        return {"success": False, "message": str(e)}
    finally:
        db.close()


def reschedule_appointment(appointment_id: int, new_date_str: str, new_time_str: str, organization_id: int = 1) -> Dict[str, Any]:
    """Reschedule an existing appointment to a new date & time after checking slot availability."""
    db = SessionLocal()
    try:
        appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if not appt:
            return {"success": False, "message": f"Appointment #{appointment_id} not found."}

        target_date = calendar_verification_service.parse_date(new_date_str or "today")
        formatted_date = target_date.strftime("%d %b %Y")
        time_24h = calendar_verification_service.parse_time_to_24h(new_time_str or "10:00 AM") or "10:00"
        formatted_time = calendar_verification_service.format_time_12h(time_24h)

        verif = check_availability(appt.staff, formatted_date, formatted_time, organization_id)
        if verif["is_available"] is False:
            alt_text = ", ".join(verif["alternative_slots"]) if verif["alternative_slots"] else "other dates"
            return {
                "success": False,
                "message": f"New slot {formatted_time} on {formatted_date} is not available. Available slots: {alt_text}"
            }

        appt.appointment_date = formatted_date
        appt.appointment_time = formatted_time
        db.commit()
        logger.info(f"[TOOL EXEC] Rescheduled Appointment #{appointment_id} to {formatted_date} {formatted_time}")
        return {
            "success": True,
            "appointment_id": appt.id,
            "new_date": formatted_date,
            "new_time": formatted_time,
            "message": f"Appointment #{appointment_id} successfully rescheduled to {formatted_date} at {formatted_time}."
        }
    except Exception as e:
        db.rollback()
        logger.error(f"[TOOL ERROR] reschedule_appointment failed: {e}")
        return {"success": False, "message": str(e)}
    finally:
        db.close()


# Definitions of OpenAI-compatible function calling schemas for xAI Grok
GROK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_doctors",
            "description": "List all staff, team members, stylists, teachers, doctors, or specialists available at the organization.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_departments",
            "description": "List all departments, divisions, specialties, or service categories available at the organization.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_doctor_information",
            "description": "Get detailed information about a specific staff member, doctor, teacher, or specialist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_name": {"type": "string", "description": "Name of the staff member, doctor, or specialist to look up"}
                },
                "required": ["doctor_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search organization knowledge base for policies, fees, services, working hours, pricing, and FAQs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Knowledge base query text"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": "Retrieve live available appointment time slots for a staff member, specialist, or service on a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_name": {"type": "string", "description": "Name of the staff member, specialist, or service"},
                    "date_str": {"type": "string", "description": "Target date (e.g., '2026-09-14', 'Tomorrow', 'Monday')"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Verify whether a specific staff member, specialist, or service is available at an exact date and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_name": {"type": "string", "description": "Name of the staff member, specialist, or service"},
                    "date_str": {"type": "string", "description": "Date of appointment"},
                    "time_str": {"type": "string", "description": "Time of appointment (e.g., '9:30 AM')"}
                },
                "required": ["doctor_name", "date_str", "time_str"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book an appointment for a customer after staff/service, date, time, and contact info are collected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {"type": "string", "description": "Full name of the customer"},
                    "phone": {"type": "string", "description": "Customer phone number"},
                    "doctor_name": {"type": "string", "description": "Name of the staff member, specialist, or service"},
                    "date_str": {"type": "string", "description": "Appointment date"},
                    "time_str": {"type": "string", "description": "Appointment time"},
                    "department": {"type": "string", "description": "Department or category name"}
                },
                "required": ["patient_name", "phone", "doctor_name", "date_str", "time_str"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancel an existing appointment by appointment ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer", "description": "Numeric ID of the appointment to cancel"}
                },
                "required": ["appointment_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Reschedule an existing appointment to a new date and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer", "description": "Numeric ID of the appointment"},
                    "new_date_str": {"type": "string", "description": "New appointment date"},
                    "new_time_str": {"type": "string", "description": "New appointment time"}
                },
                "required": ["appointment_id", "new_date_str", "new_time_str"]
            }
        }
    }
]
