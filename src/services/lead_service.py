import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import or_, desc, asc, func

from src.models.lead import Lead, LeadActivity
from src.models.call_log import CallLog, CallType
from src.models.appointment import Appointment, AppointmentStatus

logger = logging.getLogger(__name__)

GENERIC_NAMES = {
    "unknown", "unknown caller", "sip caller", "customer mobile",
    "private number", "inbound caller", "anonymous", "anonymous caller",
    "caller", "customer", "none", ""
}

def is_generic_name(name: Optional[str]) -> bool:
    if not name:
        return True
    cleaned = str(name).strip().lower()
    if cleaned in GENERIC_NAMES:
        return True
    # If the name is just digits or looks like a phone number
    if cleaned.replace("+", "").replace("-", "").replace(" ", "").isdigit():
        return True
    if cleaned.startswith("outbound"):
        return True
    return False

def clean_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    p = str(phone).strip()
    return p

class LeadService:
    def __init__(self, db: Session):
        self.db = db

    def get_leads(
        self,
        organization_id: int,
        search: Optional[str] = None,
        call_type: Optional[str] = None,
        lead_status: Optional[str] = None,
        appointment_status: Optional[str] = None,
        product_service: Optional[str] = None,
        date_range: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        sort_by: str = "call_time",
        sort_order: str = "desc",
        page: int = 1,
        limit: int = 20,
        status: Optional[str] = None,  # For backward-compatibility with dialer
    ) -> Dict[str, Any]:
        """List leads with multi-filter, search, sorting and server-side pagination."""
        query = self.db.query(Lead).filter(Lead.organization_id == organization_id)

        # Backward compatibility filter for dialer
        if status and status != "all":
            query = query.filter(Lead.status == status)

        # Lead status filter
        if lead_status and lead_status.lower() != "all":
            query = query.filter(func.lower(Lead.lead_status) == lead_status.lower())

        # Appointment status filter
        if appointment_status and appointment_status.lower() != "all":
            query = query.filter(func.lower(Lead.appointment_status) == appointment_status.lower())

        # Call type filter (Inbound / Outbound)
        if call_type and call_type.lower() != "all":
            query = query.filter(func.lower(Lead.call_type) == call_type.lower())

        # Product / Service filter
        if product_service and product_service.lower() != "all":
            s_term = f"%{product_service.strip().lower()}%"
            query = query.filter(
                or_(
                    func.lower(Lead.product_service).like(s_term),
                    func.lower(Lead.service_discussed).like(s_term),
                    func.lower(Lead.service_booked).like(s_term),
                )
            )

        # Search query across name, phone, email
        if search and search.strip():
            s = f"%{search.strip().lower()}%"
            query = query.filter(
                or_(
                    func.lower(Lead.name).like(s),
                    func.lower(Lead.phone).like(s),
                    func.lower(Lead.email).like(s),
                    func.lower(Lead.lead_number).like(s),
                )
            )

        # Date range filter
        now = datetime.now(timezone.utc)
        if date_range == "today":
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            query = query.filter(Lead.call_time >= today_start)
        elif date_range == "7days":
            seven_days_ago = now - timedelta(days=7)
            query = query.filter(Lead.call_time >= seven_days_ago)
        elif date_range == "30days":
            thirty_days_ago = now - timedelta(days=30)
            query = query.filter(Lead.call_time >= thirty_days_ago)
        elif start_date:
            try:
                s_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                query = query.filter(Lead.call_time >= s_dt)
            except Exception:
                pass
            if end_date:
                try:
                    e_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    query = query.filter(Lead.call_time <= e_dt)
                except Exception:
                    pass

        total = query.count()

        # Sorting
        sort_col = getattr(Lead, sort_by, Lead.call_time)
        if sort_col is None:
            sort_col = Lead.call_time
        
        order_fn = desc if sort_order.lower() == "desc" else asc
        query = query.order_by(order_fn(sort_col))

        # Pagination
        offset = max(0, (page - 1) * limit)
        leads = query.offset(offset).limit(limit).all()

        total_pages = (total + limit - 1) // limit if limit > 0 else 1

        formatted = [self._format_lead_summary(l) for l in leads]

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
            "results": formatted,
        }

    def get_metrics(self, organization_id: int) -> Dict[str, int]:
        """Dynamic summary metrics directly from actual database records."""
        base_q = self.db.query(Lead).filter(Lead.organization_id == organization_id)

        total_leads = base_q.count()
        new_leads = base_q.filter(func.lower(Lead.lead_status) == "new").count()
        contacted_leads = base_q.filter(func.lower(Lead.lead_status) == "contacted").count()
        qualified_leads = base_q.filter(func.lower(Lead.lead_status) == "qualified").count()
        
        appointments_booked = base_q.filter(
            or_(
                func.lower(Lead.appointment_status).in_(["booked", "confirmed", "completed"]),
                Lead.appointment_id.isnot(None),
            )
        ).count()

        follow_up_required = base_q.filter(Lead.follow_up_required.is_(True)).count()

        return {
            "totalLeads": total_leads,
            "newLeads": new_leads,
            "contactedLeads": contacted_leads,
            "qualifiedLeads": qualified_leads,
            "appointmentsBooked": appointments_booked,
            "followUpRequired": follow_up_required,
        }

    def get_services(self, organization_id: int) -> List[str]:
        """Retrieve all unique products or services referenced in this organization's leads."""
        leads = self.db.query(Lead).filter(Lead.organization_id == organization_id).all()
        services = set()
        for l in leads:
            for s in [l.product_service, l.service_discussed, l.service_booked]:
                if s and str(s).strip() and str(s).strip() != "Discussed — Not booked":
                    services.add(str(s).strip())
        return sorted(list(services))

    def get_lead_details(self, lead_id: int, organization_id: int) -> Optional[Dict[str, Any]]:
        """Fetch full lead details with linked calls, appointment, and chronological activities."""
        lead = self.db.query(Lead).filter(
            Lead.id == lead_id,
            Lead.organization_id == organization_id
        ).first()

        if not lead:
            return None

        # Fetch linked call log
        call_log = None
        if lead.call_log_id:
            call_log = self.db.query(CallLog).filter(CallLog.id == lead.call_log_id).first()
        if not call_log and lead.phone:
            # Fallback to latest call matching phone
            d10 = lead.phone[-10:] if len(lead.phone) >= 10 else lead.phone
            call_log = (
                self.db.query(CallLog)
                .filter(
                    CallLog.organization_id == organization_id,
                    CallLog.caller_phone.like(f"%{d10}")
                )
                .order_by(CallLog.created_at.desc())
                .first()
            )

        # Parse conversation transcript from call log messages
        transcript = []
        if call_log and call_log.messages:
            try:
                raw_msgs = json.loads(call_log.messages)
                if isinstance(raw_msgs, list):
                    for m in raw_msgs:
                        role = m.get("role", "ai")
                        text = m.get("text") or m.get("message") or m.get("content") or ""
                        time_str = m.get("timestamp") or m.get("time") or ""
                        if text:
                            transcript.append({
                                "role": "user" if role == "user" else "assistant",
                                "message": text,
                                "timestamp": time_str
                            })
            except Exception:
                pass
        
        # Fallback to caller_said and ai_response if messages JSON was empty
        if not transcript and call_log:
            if call_log.caller_said:
                transcript.append({
                    "role": "user",
                    "message": call_log.caller_said,
                    "timestamp": call_log.created_at.isoformat() if call_log.created_at else ""
                })
            if call_log.ai_response:
                transcript.append({
                    "role": "assistant",
                    "message": call_log.ai_response,
                    "timestamp": call_log.created_at.isoformat() if call_log.created_at else ""
                })

        # Fetch linked appointment
        appointment = None
        if lead.appointment_id:
            appointment = self.db.query(Appointment).filter(Appointment.id == lead.appointment_id).first()
        if not appointment and lead.phone:
            d10 = lead.phone[-10:] if len(lead.phone) >= 10 else lead.phone
            appointment = (
                self.db.query(Appointment)
                .filter(
                    Appointment.organization_id == organization_id,
                    Appointment.phone.like(f"%{d10}")
                )
                .order_by(Appointment.created_at.desc())
                .first()
            )

        # Fetch activities timeline
        activities = (
            self.db.query(LeadActivity)
            .filter(LeadActivity.lead_id == lead.id)
            .order_by(LeadActivity.created_at.asc())
            .all()
        )

        timeline = []
        for a in activities:
            meta = {}
            if a.metadata_json:
                try:
                    meta = json.loads(a.metadata_json)
                except Exception:
                    pass
            timeline.append({
                "id": a.id,
                "event_type": a.event_type,
                "title": a.title,
                "description": a.description or "",
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "metadata": meta,
            })

        # Build response adhering strictly to requirements:
        # "Never invent an email address or customer identity from a phone number."
        # "If customer email or name has not been collected, display Not provided."
        # "If product was only discussed but not booked, display Discussed — Not booked."
        display_name = lead.name if (lead.name and not is_generic_name(lead.name)) else "Not provided"
        display_email = lead.email if (lead.email and "@" in lead.email) else "Not provided"

        # Product / service display logic: If not booked, display Discussed — Not booked
        is_booked = (lead.appointment_status in ("Booked", "Confirmed", "Completed") or lead.appointment_id is not None)
        if is_booked:
            product_display = lead.service_booked or lead.product_service or "Booked"
        else:
            disc = lead.service_discussed or lead.product_service
            if disc and "discussed" not in disc.lower():
                product_display = f"{disc} (Discussed — Not booked)"
            else:
                product_display = disc or "Discussed — Not booked"

        # Recording URL: check call_log recording
        recording_url = None
        if call_log and call_log.recording_url:
            recording_url = call_log.recording_url
        elif call_log and call_log.id:
            recording_url = f"/api/v1/recordings/{call_log.id}/audio"

        call_info = None
        if call_log:
            call_info = {
                "id": call_log.id,
                "call_sid": call_log.call_sid,
                "call_type": call_log.call_type.value if hasattr(call_log.call_type, "value") else str(call_log.call_type or "INBOUND"),
                "status": call_log.status or "completed",
                "duration": call_log.duration or "00:00",
                "started_at": call_log.created_at.isoformat() if call_log.created_at else None,
                "intent": call_log.intent or "Inquiry",
                "receptionist_state": call_log.receptionist_state or "Completed",
                "recording_url": recording_url,
                "recording_status": call_log.recording_status or "NOT_RECORDED",
                "transcript": transcript,
            }

        appointment_info = None
        if appointment:
            appointment_info = {
                "id": appointment.id,
                "reference_id": appointment.customer_id or f"APPT-{appointment.id:04d}",
                "customer": appointment.customer,
                "service": appointment.service or appointment.department,
                "department": appointment.department,
                "staff": appointment.staff,
                "date": appointment.appointment_date,
                "time": appointment.appointment_time,
                "duration": appointment.duration or "30 Minutes",
                "status": appointment.status.value if hasattr(appointment.status, "value") else str(appointment.status or "Pending"),
                "notes": appointment.notes,
                "calendar_event_id": getattr(appointment, "calendar_event_id", None) or getattr(lead, "calendar_event_id", None),
                "created_at": appointment.created_at.isoformat() if appointment.created_at else None,
            }

        return {
            "id": lead.id,
            "lead_number": lead.lead_number or f"LEAD-{lead.id:04d}",
            "customer": {
                "name": display_name,
                "raw_name": lead.name,
                "phone": lead.phone,
                "email": display_email,
                "raw_email": lead.email,
                "lead_source": lead.lead_source or "Inbound Voice Call",
            },
            "call": call_info,
            "product_service": {
                "display": product_display,
                "discussed": lead.service_discussed,
                "booked": lead.service_booked,
                "interest_notes": lead.interest_notes or "Customer engaged via AI Voice Call",
                "notes": lead.notes or "",
            },
            "appointment": appointment_info,
            "calendar_event_id": getattr(lead, "calendar_event_id", None),
            "lead_status": lead.lead_status or "New",
            "appointment_status": lead.appointment_status or ("Booked" if appointment else "Not Booked"),
            "follow_up": {
                "required": bool(lead.follow_up_required),
                "date": lead.follow_up_date.isoformat() if lead.follow_up_date else None,
            },
            "timeline": timeline,
            "created_at": lead.created_at.isoformat() if lead.created_at else None,
            "updated_at": lead.updated_at.isoformat() if lead.updated_at else None,
        }

    def update_lead(self, lead_id: int, organization_id: int, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update lead attributes and maintain audit activity log."""
        lead = self.db.query(Lead).filter(
            Lead.id == lead_id,
            Lead.organization_id == organization_id
        ).first()

        if not lead:
            return None

        status_changed = False
        old_status = lead.lead_status

        if "lead_status" in payload and payload["lead_status"]:
            new_status = payload["lead_status"].strip()
            if new_status != lead.lead_status:
                lead.lead_status = new_status
                status_changed = True

        if "appointment_status" in payload and payload["appointment_status"]:
            lead.appointment_status = payload["appointment_status"].strip()

        if "notes" in payload:
            lead.notes = payload["notes"]

        if "follow_up_required" in payload:
            lead.follow_up_required = bool(payload["follow_up_required"])

        if "follow_up_date" in payload:
            if payload["follow_up_date"]:
                try:
                    lead.follow_up_date = datetime.fromisoformat(payload["follow_up_date"].replace("Z", "+00:00"))
                except Exception:
                    pass
            else:
                lead.follow_up_date = None

        if "product_service" in payload:
            lead.product_service = payload["product_service"]

        lead.updated_at = datetime.now(timezone.utc)
        self.db.commit()

        # Audit timeline event for status change
        if status_changed:
            self.add_activity(
                lead_id=lead.id,
                organization_id=organization_id,
                event_type="status_changed",
                title=f"Lead Status Changed to {lead.lead_status}",
                description=f"Status transitioned from {old_status} to {lead.lead_status}",
                metadata={"old_status": old_status, "new_status": lead.lead_status}
            )

        if "notes" in payload and payload["notes"]:
            self.add_activity(
                lead_id=lead.id,
                organization_id=organization_id,
                event_type="note_added",
                title="Lead Notes Updated",
                description=payload["notes"],
                metadata={}
            )

        return self.get_lead_details(lead.id, organization_id)

    def add_activity(
        self,
        lead_id: int,
        organization_id: int,
        event_type: str,
        title: str,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LeadActivity:
        """Add a persisted activity entry to the lead timeline."""
        act = LeadActivity(
            lead_id=lead_id,
            organization_id=organization_id,
            event_type=event_type,
            title=title,
            description=description,
            metadata_json=json.dumps(metadata or {}),
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(act)
        self.db.commit()
        return act

    def upsert_lead_from_session(
        self,
        session: Any,
        call_log_id: Optional[int] = None,
        lead_status: Optional[str] = None,
        appointment_status: Optional[str] = None,
        follow_up_required: Optional[bool] = None,
        notes: Optional[str] = None
    ) -> Optional[Lead]:
        """
        Dynamically captures or updates a lead directly from active conversation session state.
        Ensures customer contact details are saved immediately as turns occur, without waiting
        for appointment finalization or call teardown.
        Guarantees strict multi-tenant isolation, robust deduplication, and non-blocking safety.
        """
        try:
            org_id = getattr(session, "organization_id", 1) or 1
            raw_phone = clean_phone(getattr(session, "contact_number", None) or getattr(session, "caller_phone", None))
            cust_name = getattr(session, "patient_name", None) or getattr(session, "caller_name", None)
            if cust_name and is_generic_name(cust_name):
                cust_name = None
            if cust_name:
                cust_name = cust_name.strip()

            cust_email = getattr(session, "email", None)
            if cust_email:
                cust_email = cust_email.strip()
                if "@" not in cust_email or "." not in cust_email:
                    cust_email = None

            # Need at least phone, email, or genuine name to track a lead
            if not raw_phone and not cust_email and not cust_name:
                return None
            if raw_phone and raw_phone.lower() in ("unknown", "anonymous", "private", "sip caller"):
                raw_phone = ""
            if not raw_phone and not cust_email:
                return None

            d10 = raw_phone[-10:] if len(raw_phone) >= 10 else raw_phone

            # Deduplication: 1. By Phone in this Organization
            lead = None
            if d10:
                lead = (
                    self.db.query(Lead)
                    .filter(
                        Lead.organization_id == org_id,
                        Lead.phone.like(f"%{d10}")
                    )
                    .first()
                )

            # Deduplication: 2. Fallback by Email in this Organization
            if not lead and cust_email:
                lead = (
                    self.db.query(Lead)
                    .filter(
                        Lead.organization_id == org_id,
                        func.lower(Lead.email) == cust_email.lower()
                    )
                    .first()
                )

            now = datetime.now(timezone.utc)
            target_entity = (
                getattr(session, "doctor", None)
                or getattr(session, "service", None)
                or getattr(session, "department", None)
                or (getattr(session, "booking_data", {}).get("service") if hasattr(session, "booking_data") else None)
            )

            is_booked = bool(
                getattr(session, "appointment_id", None)
                or (getattr(session, "state", None) in ("BOOKED", "COMPLETED"))
            )

            # Determine lead status
            if lead_status:
                computed_lead_status = lead_status
            elif is_booked:
                computed_lead_status = "Converted"
            elif lead and lead.lead_status and lead.lead_status != "New":
                computed_lead_status = lead.lead_status
            else:
                computed_lead_status = "New"

            # Determine appointment status
            if appointment_status:
                computed_appt_status = appointment_status
            elif is_booked:
                computed_appt_status = "Booked"
            elif (getattr(session, "doctor", None) or getattr(session, "date", None) or getattr(session, "time", None)):
                computed_appt_status = "Pending"
            elif lead and lead.appointment_status:
                computed_appt_status = lead.appointment_status
            else:
                computed_appt_status = "Not Booked"

            # Determine follow-up required
            if follow_up_required is not None:
                computed_follow_up = follow_up_required
            elif is_booked:
                computed_follow_up = False
            elif computed_appt_status in ("Pending", "Not Booked"):
                computed_follow_up = True
            elif lead:
                computed_follow_up = bool(lead.follow_up_required)
            else:
                computed_follow_up = False

            call_dir = getattr(session, "call_type", "Inbound") or "Inbound"
            if hasattr(call_dir, "value"):
                call_dir = call_dir.value
            call_dir_str = "Outbound" if "out" in str(call_dir).lower() else "Inbound"

            # Type guards for optional relational and metadata fields
            call_id_val = call_log_id if isinstance(call_log_id, int) else (getattr(session, "db_call_id", None) if isinstance(getattr(session, "db_call_id", None), int) else None)
            appt_id_val = getattr(session, "appointment_id", None) if isinstance(getattr(session, "appointment_id", None), int) else None
            cal_ev_raw = getattr(session, "calendar_event_id", None)
            cal_ev_val = str(cal_ev_raw) if (isinstance(cal_ev_raw, str) and cal_ev_raw.strip()) else None

            is_new = False
            if not lead:
                is_new = True
                lead = Lead(
                    organization_id=org_id,
                    phone=raw_phone or (d10 or "Not provided"),
                    name=cust_name,
                    email=cust_email,
                    call_type=call_dir_str,
                    call_time=now,
                    call_log_id=call_id_val,
                    appointment_id=appt_id_val,
                    calendar_event_id=cal_ev_val,
                    lead_status=computed_lead_status,
                    appointment_status=computed_appt_status,
                    product_service=target_entity or "AI Receptionist Booking",
                    service_discussed=target_entity,
                    service_booked=target_entity if is_booked else None,
                    interest_notes=notes or f"Customer engaged via AI Voice Call ({target_entity or 'Consultation'})",
                    follow_up_required=computed_follow_up,
                    lead_source=f"AI Receptionist / {call_dir_str} Call",
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(lead)
                self.db.flush()
                lead.lead_number = f"LEAD-{lead.id:04d}"
            else:
                # Update existing lead with non-empty genuine values
                patient_name_val = getattr(session, "patient_name", None)
                if patient_name_val and not is_generic_name(patient_name_val):
                    lead.name = patient_name_val.strip()
                elif cust_name and (not lead.name or is_generic_name(lead.name)):
                    lead.name = cust_name
                if cust_email and not lead.email:
                    lead.email = cust_email
                if raw_phone and (not lead.phone or lead.phone in ("Unknown", "Not provided")):
                    lead.phone = raw_phone
                if call_id_val and not lead.call_log_id:
                    lead.call_log_id = call_id_val
                if appt_id_val:
                    lead.appointment_id = appt_id_val
                if cal_ev_val:
                    lead.calendar_event_id = cal_ev_val
                if target_entity:
                    lead.service_discussed = target_entity
                    if not lead.product_service:
                        lead.product_service = target_entity
                    if is_booked:
                        lead.service_booked = target_entity
                if notes:
                    lead.notes = notes
                lead.lead_status = computed_lead_status
                lead.appointment_status = computed_appt_status
                lead.follow_up_required = computed_follow_up
                lead.updated_at = now

            self.db.commit()

            if is_new:
                self.add_activity(
                    lead_id=lead.id,
                    organization_id=org_id,
                    event_type="lead_created",
                    title="Lead Captured via AI Receptionist",
                    description=f"Lead established from conversation ({raw_phone or cust_email})",
                    metadata={
                        "phone": raw_phone,
                        "name": cust_name,
                        "email": cust_email,
                        "source": lead.lead_source,
                        "call_sid": getattr(session, "call_sid", None),
                    }
                )
            return lead

        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[LeadService] upsert_lead_from_session error: {exc}")
            return None

    def capture_or_update_from_call(
        self,
        call_log_id: int,
        session_booking_data: Optional[Dict[str, Any]] = None
    ) -> Optional[Lead]:
        """
        Transactional, safe capture from call lifecycle without disrupting voice flows.
        Avoids creating duplicate leads for the same customer phone in the same organization.
        """
        try:
            call = self.db.query(CallLog).filter(CallLog.id == call_log_id).first()
            if not call or not call.caller_phone:
                return None

            org_id = call.organization_id or 1
            raw_phone = clean_phone(call.caller_phone)
            if not raw_phone or raw_phone.lower() in ("unknown", "anonymous", "private"):
                return None

            d10 = raw_phone[-10:] if len(raw_phone) >= 10 else raw_phone

            # Deduplication: find existing lead by phone and organization
            lead = (
                self.db.query(Lead)
                .filter(
                    Lead.organization_id == org_id,
                    Lead.phone.like(f"%{d10}")
                )
                .first()
            )

            is_new = False
            now = datetime.now(timezone.utc)
            call_dt = call.created_at or now

            # Determine genuine customer name if available
            verified_name = None
            if call.caller_name and not is_generic_name(call.caller_name):
                verified_name = call.caller_name.strip()
            if session_booking_data and session_booking_data.get("caller_name"):
                sb_name = session_booking_data["caller_name"].strip()
                if not is_generic_name(sb_name):
                    verified_name = sb_name

            # Determine genuine email if available
            verified_email = None
            if session_booking_data and session_booking_data.get("email"):
                sb_email = session_booking_data["email"].strip()
                if "@" in sb_email and "." in sb_email:
                    verified_email = sb_email

            # Product / service discussed
            service_discussed = None
            if session_booking_data and (session_booking_data.get("service") or session_booking_data.get("product")):
                service_discussed = session_booking_data.get("service") or session_booking_data.get("product")
            elif call.intent and call.intent not in ("Unknown", "Appointment Booking"):
                service_discussed = call.intent

            call_direction = "Outbound" if (call.call_type == CallType.OUTBOUND or "Outbound" in str(call.caller_name)) else "Inbound"

            if not lead:
                is_new = True
                lead = Lead(
                    organization_id=org_id,
                    phone=raw_phone,
                    name=verified_name,
                    email=verified_email,
                    call_type=call_direction,
                    call_time=call_dt,
                    call_log_id=call.id,
                    lead_status="Contacted" if call.duration and call.duration != "00:00" else "New",
                    appointment_status="Not Booked",
                    product_service=service_discussed or (call.intent if call.intent != "Unknown" else "General Inquiry"),
                    service_discussed=service_discussed,
                    lead_source=f"{call_direction} Voice Call",
                    created_at=call_dt,
                    updated_at=now,
                )
                self.db.add(lead)
                self.db.flush()
                lead.lead_number = f"LEAD-{lead.id:04d}"
            else:
                # Update existing lead with verified new details
                lead.call_log_id = call.id
                lead.call_time = call_dt
                lead.call_type = call_direction
                if verified_name and (not lead.name or is_generic_name(lead.name)):
                    lead.name = verified_name
                if verified_email and not lead.email:
                    lead.email = verified_email
                if service_discussed and not lead.service_discussed:
                    lead.service_discussed = service_discussed
                    if not lead.product_service:
                        lead.product_service = service_discussed
                if lead.lead_status == "New" and call.duration and call.duration != "00:00":
                    lead.lead_status = "Contacted"
                lead.updated_at = now

            self.db.commit()
            call.lead_id = lead.id
            self.db.commit()

            # Record Timeline Activities
            if is_new:
                self.add_activity(
                    lead_id=lead.id,
                    organization_id=org_id,
                    event_type="lead_created",
                    title="Lead Created",
                    description=f"Lead recorded via {call_direction} voice call from {raw_phone}",
                    metadata={"source": lead.lead_source, "phone": raw_phone}
                )

            event_title = f"{call_direction} Call Received" if call_direction == "Inbound" else f"{call_direction} Call Placed"
            outcome_desc = f"Call duration: {call.duration or '00:00'}. Outcome: {call.status or 'completed'}."
            if call.intent:
                outcome_desc += f" Intent: {call.intent}."

            self.add_activity(
                lead_id=lead.id,
                organization_id=org_id,
                event_type="call_completed",
                title=event_title,
                description=outcome_desc,
                metadata={
                    "call_id": call.id,
                    "call_sid": call.call_sid,
                    "duration": call.duration,
                    "status": call.status,
                    "call_type": call_direction
                }
            )

            if verified_name or verified_email:
                collected_items = []
                if verified_name: collected_items.append(f"Name: {verified_name}")
                if verified_email: collected_items.append(f"Email: {verified_email}")
                self.add_activity(
                    lead_id=lead.id,
                    organization_id=org_id,
                    event_type="customer_details_collected",
                    title="Customer Details Verified",
                    description=", ".join(collected_items),
                    metadata={"name": verified_name, "email": verified_email}
                )

            if service_discussed:
                self.add_activity(
                    lead_id=lead.id,
                    organization_id=org_id,
                    event_type="product_discussed",
                    title=f"Discussed Product/Service: {service_discussed}",
                    description=f"Caller expressed interest in {service_discussed} (Discussed — Not booked)",
                    metadata={"service": service_discussed}
                )

            return lead

        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[LeadService] Safe capture error (call {call_log_id}): {exc}")
            return None

    def link_appointment(self, appointment: Appointment) -> Optional[Lead]:
        """Link confirmed appointment to customer's lead and update status to Converted."""
        try:
            if not appointment or not appointment.phone:
                return None

            org_id = appointment.organization_id or 1
            raw_phone = clean_phone(appointment.phone)
            d10 = raw_phone[-10:] if len(raw_phone) >= 10 else raw_phone

            lead = (
                self.db.query(Lead)
                .filter(
                    Lead.organization_id == org_id,
                    Lead.phone.like(f"%{d10}")
                )
                .first()
            )

            now = datetime.now(timezone.utc)
            service_name = appointment.service or appointment.department or "Appointment Booking"

            cal_ev_id = getattr(appointment, "calendar_event_id", None)

            if not lead:
                lead = Lead(
                    organization_id=org_id,
                    phone=raw_phone,
                    name=appointment.customer if not is_generic_name(appointment.customer) else None,
                    email=appointment.email if (appointment.email and "@" in appointment.email) else None,
                    call_type="Inbound",
                    call_time=appointment.created_at or now,
                    appointment_id=appointment.id,
                    calendar_event_id=cal_ev_id,
                    appointment_status="Booked",
                    lead_status="Converted",
                    product_service=service_name,
                    service_booked=service_name,
                    follow_up_required=False,
                    lead_source="AI Receptionist / Inbound Call",
                    created_at=appointment.created_at or now,
                    updated_at=now,
                )
                self.db.add(lead)
                self.db.flush()
                lead.lead_number = f"LEAD-{lead.id:04d}"
            else:
                lead.appointment_id = appointment.id
                lead.appointment_status = "Booked"
                lead.lead_status = "Converted"
                lead.follow_up_required = False
                if cal_ev_id:
                    lead.calendar_event_id = cal_ev_id
                lead.service_booked = service_name
                lead.product_service = service_name
                if appointment.customer and not is_generic_name(appointment.customer) and (not lead.name or is_generic_name(lead.name)):
                    lead.name = appointment.customer
                if appointment.email and "@" in appointment.email and not lead.email:
                    lead.email = appointment.email
                lead.updated_at = now

            self.db.commit()

            ref_id = appointment.customer_id or f"APPT-{appointment.id:04d}"
            self.add_activity(
                lead_id=lead.id,
                organization_id=org_id,
                event_type="appointment_booked",
                title=f"Appointment Booked: {service_name}",
                description=f"Confirmed for {appointment.appointment_date} at {appointment.appointment_time} with {appointment.staff}. Reference ID: {ref_id}",
                metadata={
                    "appointment_id": appointment.id,
                    "reference_id": ref_id,
                    "date": appointment.appointment_date,
                    "time": appointment.appointment_time,
                    "staff": appointment.staff,
                    "department": appointment.department,
                    "service": service_name,
                    "calendar_event_id": cal_ev_id,
                }
            )

            return lead
        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[LeadService] Link appointment error: {exc}")
            return None

    def backfill_historical_data(self) -> Dict[str, int]:
        """
        One-time backfill of existing CallLog and Appointment records into Leads.
        Guarantees actual DB historical data appears on the Leads dashboard immediately.
        """
        created = 0
        updated = 0
        try:
            # 1. Backfill all existing appointments first
            appointments = self.db.query(Appointment).order_by(Appointment.created_at.asc()).all()
            for appt in appointments:
                if not appt.phone:
                    continue
                d10 = appt.phone[-10:] if len(appt.phone) >= 10 else appt.phone
                org_id = appt.organization_id or 1
                lead = self.db.query(Lead).filter(
                    Lead.organization_id == org_id,
                    Lead.phone.like(f"%{d10}")
                ).first()

                service_name = appt.service or appt.department or "Appointment Booking"
                cust_name = appt.customer if not is_generic_name(appt.customer) else None
                cust_email = appt.email if (appt.email and "@" in appt.email) else None

                if not lead:
                    lead = Lead(
                        organization_id=org_id,
                        phone=appt.phone,
                        name=cust_name,
                        email=cust_email,
                        call_type="Inbound",
                        call_time=appt.created_at,
                        appointment_id=appt.id,
                        appointment_status="Booked",
                        lead_status="Converted",
                        product_service=service_name,
                        service_booked=service_name,
                        lead_source="Inbound Booking",
                        created_at=appt.created_at,
                        updated_at=appt.created_at,
                    )
                    self.db.add(lead)
                    self.db.flush()
                    lead.lead_number = f"LEAD-{lead.id:04d}"
                    created += 1

                    self.add_activity(
                        lead_id=lead.id,
                        organization_id=org_id,
                        event_type="lead_created",
                        title="Lead Created",
                        description=f"Lead established from customer interaction",
                        metadata={"phone": appt.phone}
                    )
                    ref_id = appt.customer_id or f"APPT-{appt.id:04d}"
                    self.add_activity(
                        lead_id=lead.id,
                        organization_id=org_id,
                        event_type="appointment_booked",
                        title=f"Appointment Booked: {service_name}",
                        description=f"Confirmed for {appt.appointment_date} at {appt.appointment_time} with {appt.staff}. Ref: {ref_id}",
                        metadata={"appointment_id": appt.id, "ref_id": ref_id}
                    )
                else:
                    if not lead.appointment_id:
                        lead.appointment_id = appt.id
                        lead.appointment_status = "Booked"
                        lead.lead_status = "Converted"
                        lead.service_booked = service_name
                        if not lead.product_service:
                            lead.product_service = service_name
                        if cust_name and (not lead.name or is_generic_name(lead.name)):
                            lead.name = cust_name
                        if cust_email and not lead.email:
                            lead.email = cust_email
                        updated += 1
            self.db.commit()

            # 2. Backfill from call logs
            calls = self.db.query(CallLog).order_by(CallLog.created_at.asc()).all()
            for c in calls:
                if not c.caller_phone or c.caller_phone.lower() in ("unknown", "anonymous", "private"):
                    continue
                d10 = c.caller_phone[-10:] if len(c.caller_phone) >= 10 else c.caller_phone
                org_id = c.organization_id or 1

                lead = self.db.query(Lead).filter(
                    Lead.organization_id == org_id,
                    Lead.phone.like(f"%{d10}")
                ).first()

                call_direction = "Outbound" if (c.call_type == CallType.OUTBOUND or "Outbound" in str(c.caller_name)) else "Inbound"
                c_name = c.caller_name if not is_generic_name(c.caller_name) else None
                service_disc = c.intent if c.intent and c.intent != "Unknown" else "General Inquiry"

                if not lead:
                    lead = Lead(
                        organization_id=org_id,
                        phone=c.caller_phone,
                        name=c_name,
                        email=None,
                        call_type=call_direction,
                        call_time=c.created_at,
                        call_log_id=c.id,
                        lead_status="Contacted" if c.duration and c.duration != "00:00" else "New",
                        appointment_status="Not Booked",
                        product_service=service_disc,
                        service_discussed=service_disc,
                        lead_source=f"{call_direction} Voice Call",
                        created_at=c.created_at,
                        updated_at=c.created_at,
                    )
                    self.db.add(lead)
                    self.db.flush()
                    lead.lead_number = f"LEAD-{lead.id:04d}"
                    c.lead_id = lead.id
                    created += 1

                    self.add_activity(
                        lead_id=lead.id,
                        organization_id=org_id,
                        event_type="lead_created",
                        title="Lead Created",
                        description=f"Lead recorded via {call_direction} call",
                        metadata={"phone": c.caller_phone}
                    )
                    self.add_activity(
                        lead_id=lead.id,
                        organization_id=org_id,
                        event_type="call_completed",
                        title=f"{call_direction} Call Completed",
                        description=f"Duration: {c.duration or '00:00'}. Status: {c.status or 'completed'}.",
                        metadata={"call_id": c.id, "call_sid": c.call_sid}
                    )
                else:
                    if not lead.call_log_id:
                        lead.call_log_id = c.id
                        if c_name and (not lead.name or is_generic_name(lead.name)):
                            lead.name = c_name
                        c.lead_id = lead.id
                        updated += 1
            self.db.commit()

        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[LeadService] Backfill error: {exc}")

        return {"created": created, "updated": updated}

    def _format_lead_summary(self, lead: Lead) -> Dict[str, Any]:
        """Format lead for summary table display."""
        display_name = lead.name if (lead.name and not is_generic_name(lead.name)) else "Not provided"
        display_email = lead.email if (lead.email and "@" in lead.email) else "Not provided"

        is_booked = (lead.appointment_status in ("Booked", "Confirmed", "Completed") or lead.appointment_id is not None)
        if is_booked:
            product_display = lead.service_booked or lead.product_service or "Booked"
        else:
            disc = lead.service_discussed or lead.product_service
            if disc and "discussed" not in disc.lower():
                product_display = f"{disc} (Discussed — Not booked)"
            else:
                product_display = disc or "Discussed — Not booked"

        # Linked appointment date/time
        appointment_date_time = "Not booked"
        if lead.appointment_id:
            appt = self.db.query(Appointment).filter(Appointment.id == lead.appointment_id).first()
            if appt:
                appointment_date_time = f"{appt.appointment_date} at {appt.appointment_time}"

        return {
            "id": lead.id,
            "lead_number": lead.lead_number or f"LEAD-{lead.id:04d}",
            "customer": display_name,
            "phone": lead.phone,
            "email": display_email,
            "call_type": lead.call_type or "Inbound",
            "call_time": lead.call_time.isoformat() if lead.call_time else None,
            "product_service": product_display,
            "appointment_date_time": appointment_date_time,
            "lead_status": lead.lead_status or "New",
            "appointment_status": lead.appointment_status or "Not Booked",
            "follow_up_required": bool(lead.follow_up_required),
            "follow_up_date": lead.follow_up_date.isoformat() if lead.follow_up_date else None,
            "call_log_id": lead.call_log_id,
            "appointment_id": lead.appointment_id,
            "calendar_event_id": getattr(lead, "calendar_event_id", None),
            "created_at": lead.created_at.isoformat() if lead.created_at else None,
        }

    @staticmethod
    def safe_capture_from_call_sync(call_log_id: int):
        """Safe non-blocking wrapper to trigger lead capture from outside."""
        from src.config.database import SessionLocal
        db = SessionLocal()
        try:
            svc = LeadService(db)
            svc.capture_or_update_from_call(call_log_id)
        except Exception as e:
            logger.warning(f"[LeadService] safe_capture_from_call_sync warning: {e}")
        finally:
            db.close()
