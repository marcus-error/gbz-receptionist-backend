import logging
import threading
from typing import Optional, Dict, Any, List
from sqlalchemy import func
from sqlalchemy.orm import Session
from src.models.appointment import Appointment
from src.config.database import SessionLocal

logger = logging.getLogger(__name__)


def _get_org_name(db: Session, appointment: Appointment) -> str:
    """Fetch the organization name for the appointment."""
    try:
        from src.models.organization import Organization
        org_id = getattr(appointment, "organization_id", None)
        if org_id:
            org = db.query(Organization).filter(Organization.id == org_id).first()
            if org:
                return org.name
    except Exception:
        pass
    return "Our Organization"


def log_notification(
    db: Session,
    appointment_id: Optional[int],
    recipient_type: str,
    recipient_name: str,
    recipient_contact: str,
    channel: str,
    action: str,
    subject: Optional[str],
    message_body: str,
    status: str = "sent",
    error_reason: Optional[str] = None,
    org_id: Optional[int] = 1,
):
    """Persists notification log to database for UI auditing."""
    try:
        from src.models.notification_log import NotificationLog
        log_entry = NotificationLog(
            organization_id=org_id,
            appointment_id=appointment_id,
            recipient_type=recipient_type,
            recipient_name=recipient_name,
            recipient_contact=recipient_contact,
            channel=channel,
            action=action,
            subject=subject,
            message_body=message_body,
            status=status,
            error_reason=error_reason,
        )
        db.add(log_entry)
        db.commit()
    except Exception as exc:
        logger.warning(f"Failed to persist notification log: {exc}")


def _get_org_info(db: Session, appointment: Appointment) -> Dict[str, str]:
    """Fetch the organization name and contact details for the appointment."""
    info = {"name": "Our Organization", "contact": ""}
    try:
        from src.models.organization import Organization
        org_id = getattr(appointment, "organization_id", None)
        if org_id:
            org = db.query(Organization).filter(Organization.id == org_id).first()
            if org:
                info["name"] = org.name
                info["contact"] = getattr(org, "phone", None) or getattr(org, "email", None) or ""
    except Exception:
        pass
    return info


def _determine_booking_purpose(appointment: Appointment) -> str:
    """
    Dynamically determines the booking purpose from appointment data:
    - Product demo booking (e.g. 'Product Demo', 'Software Demo', 'Demo of AI Platform')
    - Service booking (e.g. 'Consultation', 'Oil Change', 'General Service')
    - Healthcare appointment (e.g. 'Cardiology Appointment', 'Dental Checkup')
    - General booking
    Never assumes every booking is medical.
    """
    service = (appointment.service or "").strip()
    department = (appointment.department or "").strip()
    notes = (appointment.notes or "").strip()

    combined = f"{service} {department} {notes}".lower()
    if "demo" in combined:
        if service and "demo" in service.lower():
            return f"Product Demo ({service})"
        elif service:
            return f"Product Demo: {service}"
        return "Product Demo"
    if service:
        if any(w in service.lower() for w in ["consultation", "session", "service", "booking", "checkup", "exam", "meeting"]):
            return service
        return f"{service} Service"
    if department:
        if any(w in department.lower() for w in ["demo", "service", "appointment", "consultation", "meeting"]):
            return department
        return f"{department} Appointment"
    return "Booking"


def _render_template(template_str: str, variables: Dict[str, Any]) -> str:
    """
    Renders email template by replacing {{variable_name}} with values.
    Removes unpopulated optional placeholders cleanly.
    """
    import re
    if not template_str:
        return ""
    rendered = template_str
    for key, val in variables.items():
        val_str = str(val) if val is not None else ""
        rendered = rendered.replace(f"{{{{{key}}}}}", val_str)
    # Clean up any leftover unknown/empty {{...}} tags
    rendered = re.sub(r'\{\{[a-zA-Z0-9_]+\}\}', '', rendered)
    return rendered.strip()


def _get_staff_info(db: Session, appointment: Appointment) -> Dict[str, Optional[str]]:
    """
    Find the assigned staff member for the appointment strictly within the active organization.
    Returns a dict with 'name', 'email', 'phone'.
    Falls back to any registered staff member or admin in the active organization so staff notifications are never lost.
    """
    staff_name = (appointment.staff or "").strip()
    org_id = getattr(appointment, "organization_id", 1) or 1
    result = {
        "name": staff_name or "Assigned Staff",
        "email": None,
        "phone": None
    }

    try:
        from src.models.staff import Staff
        staff = None
        if staff_name and staff_name.lower() not in ("representative", "unassigned", "general", "none"):
            # Match exact or case-insensitive staff name strictly within active organization
            staff = db.query(Staff).filter(
                Staff.organization_id == org_id,
                func.lower(Staff.name) == func.lower(staff_name)
            ).first()

            if not staff:
                # Fallback to partial match within active organization
                staff = db.query(Staff).filter(
                    Staff.organization_id == org_id,
                    Staff.name.ilike(f"%{staff_name}%")
                ).first()

        if staff and (staff.email or "").strip():
            result["name"] = staff.name
            result["email"] = staff.email.strip()
            result["phone"] = (staff.phone or "").strip() or None
            return result

        # Fallback 1: Any registered staff member in this organization with an email
        any_staff = db.query(Staff).filter(
            Staff.organization_id == org_id,
            Staff.email.isnot(None),
            Staff.email != ""
        ).order_by(Staff.id.asc()).first()
        if any_staff and (any_staff.email or "").strip():
            result["name"] = staff_name if staff_name and staff_name.lower() not in ("representative", "unassigned", "general", "none") else any_staff.name
            result["email"] = any_staff.email.strip()
            result["phone"] = (any_staff.phone or "").strip() or None
            return result

        # Fallback 2: Active Organization Admin user
        from src.models.user import User
        admin_user = db.query(User).filter(
            User.organization_id == org_id,
            User.is_active == 1,
            User.email.isnot(None),
            User.email != ""
        ).order_by(User.id.asc()).first()
        if admin_user and (admin_user.email or "").strip():
            result["name"] = staff_name or admin_user.name or "Admin"
            result["email"] = admin_user.email.strip()
            return result

    except Exception as exc:
        logger.warning(f"Error querying staff for appointment #{appointment.id}: {exc}")

    return result


def send_realtime_appointment_notification(db: Session, appointment: Appointment, action: str) -> Dict[str, str]:
    """
    Send real-time Email and WhatsApp notifications immediately to BOTH 2 members:
    1. Customer (the person booking the appointment/demo/service)
    2. Assigned Staff Member (resolved from Staff Directory)

    Returns a dict summarizing delivery status for both recipients.
    Best-effort execution: API/SMTP failures are logged and never block appointment persistence.
    """
    results = {
        "customer_email": "skipped",
        "customer_whatsapp": "skipped",
        "staff_email": "skipped",
        "staff_whatsapp": "skipped",
    }
    if not appointment:
        return results

    org_info = _get_org_info(db, appointment)
    org_name = org_info["name"]
    org_contact = org_info["contact"]
    org_id = getattr(appointment, "organization_id", 1) or 1
    booking_purpose = _determine_booking_purpose(appointment)
    staff_info = _get_staff_info(db, appointment)
    status_str = appointment.status.value if hasattr(appointment.status, "value") else str(appointment.status)

    from src.routes.email_routes import _send as send_email, _send_batch as send_email_batch, _config as get_email_config, DEFAULT_CUSTOMER_TEMPLATE, DEFAULT_STAFF_TEMPLATE
    from src.routes.whatsapp_routes import _send as send_whatsapp
    from src.models.notification_log import NotificationLog

    email_cfg = get_email_config(db, organization_id=org_id)

    # -------------------------------------------------------------
    # 1. PREPARE CUSTOMER EMAIL (IF APPLICABLE)
    # -------------------------------------------------------------
    customer_email = (appointment.email or "").strip()
    customer_phone = (appointment.phone or "").strip()
    staff_email = (staff_info.get("email") or "").strip()
    staff_phone = (staff_info.get("phone") or "").strip()
    staff_name = staff_info.get("name") or appointment.staff or "Staff Member"

    cust_mail_obj = None
    if customer_email:
        already_sent_cust = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appointment.id,
            NotificationLog.channel == "email",
            NotificationLog.recipient_type == "customer",
            NotificationLog.action == action,
            NotificationLog.status == "sent"
        ).first()

        if already_sent_cust:
            logger.info(f"Customer email for #{appointment.id} ({action}) already sent. Skipping duplicate.")
            results["customer_email"] = "already_sent"
        else:
            cust_vars = {
                "customer_name": appointment.customer,
                "customer_phone": customer_phone or "",
                "customer_email": customer_email,
                "booking_ref": str(appointment.id),
                "booking_id": str(appointment.id),
                "appointment_id": str(appointment.id),
                "booking_purpose": booking_purpose,
                "purpose": booking_purpose,
                "staff_name": staff_name,
                "doctor_name": staff_name,
                "department": appointment.department or "",
                "service_product": appointment.service or appointment.department or "",
                "service": appointment.service or "",
                "product": appointment.service or "",
                "date": appointment.appointment_date or "",
                "appointment_date": appointment.appointment_date or "",
                "time": appointment.appointment_time or "",
                "appointment_time": appointment.appointment_time or "",
                "duration": appointment.duration or "30 Minutes",
                "organization_name": org_name,
                "organization_contact": org_contact,
                "notes": appointment.notes or "",
            }

            if action == "created":
                cust_subject = f"Booking Confirmation — #{appointment.id} ({booking_purpose}) - {org_name}"
                tmpl = email_cfg.template or DEFAULT_CUSTOMER_TEMPLATE
                cust_body = _render_template(tmpl, cust_vars)
            elif action in ("cancelled", "deleted"):
                cust_subject = f"Booking Cancelled — #{appointment.id} ({booking_purpose}) - {org_name}"
                cust_body = (
                    f"Hello {appointment.customer},\n\n"
                    f"Your {booking_purpose} #{appointment.id} at {org_name} has been cancelled.\n\n"
                    f"  Staff: {staff_name}\n"
                    f"  Date: {appointment.appointment_date}\n"
                    f"  Time: {appointment.appointment_time}\n"
                    f"  Status: {status_str}\n\n"
                    f"If this was a mistake, please contact us to rebook."
                )
            elif action in ("updated", "rescheduled"):
                cust_subject = f"Booking Updated — #{appointment.id} ({booking_purpose}) - {org_name}"
                cust_body = (
                    f"Hello {appointment.customer},\n\n"
                    f"Your {booking_purpose} #{appointment.id} at {org_name} has been updated.\n\n"
                    f"  Service/Product: {appointment.service or appointment.department}\n"
                    f"  Staff: {staff_name}\n"
                    f"  New Date: {appointment.appointment_date}\n"
                    f"  New Time: {appointment.appointment_time}\n"
                    f"  Status: {status_str}\n"
                    f"  Organization: {org_name}"
                )
            else:
                cust_subject = f"Notification — #{appointment.id} ({booking_purpose}) - {org_name}"
                cust_body = f"{booking_purpose} #{appointment.id} at {org_name}: {action}"

            cust_mail_obj = {
                "to": customer_email,
                "subject": cust_subject,
                "body": cust_body,
                "recipient_type": "customer",
                "recipient_name": appointment.customer,
            }

    # -------------------------------------------------------------
    # 2. PREPARE STAFF MEMBER EMAIL (IF APPLICABLE)
    # -------------------------------------------------------------
    staff_mail_obj = None
    if not staff_email:
        results["staff_email"] = "failed"
        log_notification(
            db=db,
            appointment_id=appointment.id,
            recipient_type="staff",
            recipient_name=staff_name,
            recipient_contact="None",
            channel="email",
            action=action,
            subject=f"[Staff Notice] {booking_purpose} #{appointment.id}",
            message_body=f"Staff email notification skipped: Staff member '{staff_name}' has no valid email address in Staff Directory.",
            status="failed",
            error_reason="Assigned staff member has no valid email address in Staff Directory",
            org_id=org_id
        )
    elif customer_email and staff_email.lower() == customer_email.lower():
        results["staff_email"] = "skipped_identical"
    else:
        already_sent_staff = db.query(NotificationLog).filter(
            NotificationLog.appointment_id == appointment.id,
            NotificationLog.channel == "email",
            NotificationLog.recipient_type == "staff",
            NotificationLog.action == action,
            NotificationLog.status == "sent"
        ).first()

        if already_sent_staff:
            logger.info(f"Staff email for #{appointment.id} ({action}) already sent. Skipping duplicate.")
            results["staff_email"] = "already_sent"
        else:
            staff_vars = {
                "staff_name": staff_name,
                "customer_name": appointment.customer,
                "customer_phone": customer_phone or "Not provided",
                "customer_email": customer_email or "Not provided",
                "booking_ref": str(appointment.id),
                "booking_id": str(appointment.id),
                "appointment_id": str(appointment.id),
                "booking_purpose": booking_purpose,
                "purpose": booking_purpose,
                "service_product": appointment.service or appointment.department or "",
                "department": appointment.department or "",
                "date": appointment.appointment_date or "",
                "appointment_date": appointment.appointment_date or "",
                "time": appointment.appointment_time or "",
                "appointment_time": appointment.appointment_time or "",
                "duration": appointment.duration or "30 Minutes",
                "organization_name": org_name,
                "notes": appointment.notes or "None",
            }

            if action == "created":
                staff_subject = f"[New {booking_purpose} Assigned] #{appointment.id} - {appointment.customer} ({org_name})"
                tmpl_staff = getattr(email_cfg, "staff_template", None) or DEFAULT_STAFF_TEMPLATE
                staff_body = _render_template(tmpl_staff, staff_vars)
            elif action in ("cancelled", "deleted"):
                staff_subject = f"[{booking_purpose} Cancelled] #{appointment.id} - {appointment.customer} ({org_name})"
                staff_body = (
                    f"Hello {staff_name},\n\n"
                    f"{booking_purpose} #{appointment.id} for {appointment.customer} assigned to you at {org_name} has been CANCELLED.\n\n"
                    f"  Date: {appointment.appointment_date}\n"
                    f"  Time: {appointment.appointment_time}\n"
                    f"  Status: {status_str}\n"
                )
            elif action in ("updated", "rescheduled"):
                staff_subject = f"[{booking_purpose} Rescheduled] #{appointment.id} - {appointment.customer} ({org_name})"
                staff_body = (
                    f"Hello {staff_name},\n\n"
                    f"{booking_purpose} #{appointment.id} for {appointment.customer} has been updated / rescheduled.\n\n"
                    f"  Customer: {appointment.customer}\n"
                    f"  Phone: {customer_phone or 'N/A'}\n"
                    f"  Service/Product: {appointment.service or appointment.department}\n"
                    f"  New Date: {appointment.appointment_date}\n"
                    f"  New Time: {appointment.appointment_time}\n"
                    f"  Status: {status_str}\n"
                )
            else:
                staff_subject = f"[Staff Notice] #{appointment.id} - {org_name}"
                staff_body = f"{booking_purpose} #{appointment.id} for {appointment.customer} has been {action}."

            staff_mail_obj = {
                "to": staff_email,
                "subject": staff_subject,
                "body": staff_body,
                "recipient_type": "staff",
                "recipient_name": staff_name,
            }

    # -------------------------------------------------------------
    # 3. DISPATCH BOTH EMAILS IMMEDIATELY (RAPID BATCH SMTP)
    # -------------------------------------------------------------
    emails_to_dispatch = []
    if cust_mail_obj:
        emails_to_dispatch.append(cust_mail_obj)
    if staff_mail_obj:
        emails_to_dispatch.append(staff_mail_obj)

    if emails_to_dispatch:
        try:
            batch_results = send_email_batch(db, emails_to_dispatch, organization_id=org_id)
            for item_obj, res in zip(emails_to_dispatch, batch_results):
                is_sent = bool(res and res.get("sent"))
                rtype = item_obj["recipient_type"]
                results[f"{rtype}_email"] = "sent" if is_sent else "failed"
                log_notification(
                    db=db,
                    appointment_id=appointment.id,
                    recipient_type=rtype,
                    recipient_name=item_obj["recipient_name"],
                    recipient_contact=item_obj["to"],
                    channel="email",
                    action=action,
                    subject=item_obj["subject"],
                    message_body=item_obj["body"],
                    status="sent" if is_sent else "failed",
                    error_reason=None if is_sent else str(res.get("error") if res else "Email delivery failed"),
                    org_id=org_id
                )
        except Exception as exc:
            logger.warning(f"Batch email notification failed for appointment #{appointment.id}: {exc}")
            for item_obj in emails_to_dispatch:
                rtype = item_obj["recipient_type"]
                results[f"{rtype}_email"] = "failed"
                log_notification(
                    db=db,
                    appointment_id=appointment.id,
                    recipient_type=rtype,
                    recipient_name=item_obj["recipient_name"],
                    recipient_contact=item_obj["to"],
                    channel="email",
                    action=action,
                    subject=item_obj["subject"],
                    message_body=item_obj["body"],
                    status="failed",
                    error_reason=str(exc),
                    org_id=org_id
                )

    # -------------------------------------------------------------
    # 4. DISPATCH WHATSAPP IN DETACHED NON-BLOCKING BACKGROUND THREAD
    # -------------------------------------------------------------
    def _dispatch_whatsapp_async():
        from src.config.database import SessionLocal
        bg_db = SessionLocal()
        try:
            # Customer WhatsApp
            if customer_phone:
                try:
                    if action == "created":
                        msg = (
                            f"✅ {booking_purpose} Confirmed - {org_name}\n\n"
                            f"Reference ID: #{appointment.id}\n"
                            f"Customer: {appointment.customer}\n"
                            f"Service/Product: {appointment.service or appointment.department}\n"
                            f"Staff: {staff_name}\n"
                            f"Date: {appointment.appointment_date}\n"
                            f"Time: {appointment.appointment_time}\n"
                            f"Status: {status_str}\n"
                            f"Organization: {org_name}\n\n"
                            f"Thank you for choosing {org_name}!"
                        )
                    elif action in ("cancelled", "deleted"):
                        msg = (
                            f"❌ {booking_purpose} Cancelled - {org_name}\n\n"
                            f"Reference ID: #{appointment.id}\n"
                            f"Your booking on {appointment.appointment_date} at {appointment.appointment_time} has been cancelled.\n"
                            f"Status: {status_str}"
                        )
                    elif action in ("updated", "rescheduled"):
                        msg = (
                            f"🔄 {booking_purpose} Updated - {org_name}\n\n"
                            f"Reference ID: #{appointment.id}\n"
                            f"New Date: {appointment.appointment_date}\n"
                            f"New Time: {appointment.appointment_time}\n"
                            f"Staff: {staff_name}\n"
                            f"Status: {status_str}"
                        )
                    else:
                        msg = f"{booking_purpose} #{appointment.id} at {org_name}: {action}"

                    send_whatsapp(bg_db, {
                        "messaging_product": "whatsapp",
                        "to": customer_phone,
                        "type": "text",
                        "text": {"body": msg},
                    })
                    log_notification(
                        db=bg_db,
                        appointment_id=appointment.id,
                        recipient_type="customer",
                        recipient_name=appointment.customer,
                        recipient_contact=customer_phone,
                        channel="whatsapp",
                        action=action,
                        subject=None,
                        message_body=msg,
                        status="sent",
                        org_id=org_id
                    )
                except Exception as w_exc:
                    log_notification(
                        db=bg_db,
                        appointment_id=appointment.id,
                        recipient_type="customer",
                        recipient_name=appointment.customer,
                        recipient_contact=customer_phone,
                        channel="whatsapp",
                        action=action,
                        subject=None,
                        message_body=f"WhatsApp error: {w_exc}",
                        status="failed",
                        error_reason=str(w_exc),
                        org_id=org_id
                    )

            # Staff WhatsApp
            if staff_phone and staff_phone != customer_phone:
                try:
                    if action == "created":
                        staff_msg = (
                            f"👨‍⚕️ New Appointment Assigned - {org_name}\n\n"
                            f"Appointment ID: #{appointment.id}\n"
                            f"Assigned To: {staff_name}\n"
                            f"Patient/Customer: {appointment.customer}\n"
                            f"Patient Phone: {customer_phone or 'N/A'}\n"
                            f"Service/Product: {appointment.service or appointment.department}\n"
                            f"Date: {appointment.appointment_date}\n"
                            f"Time: {appointment.appointment_time}\n"
                            f"Status: {status_str}\n"
                            f"Notes: {appointment.notes or 'None'}"
                        )
                    elif action in ("cancelled", "deleted"):
                        staff_msg = (
                            f"❌ Appointment Cancelled - {org_name}\n\n"
                            f"Appointment ID: #{appointment.id}\n"
                            f"Patient/Customer: {appointment.customer}\n"
                            f"Date: {appointment.appointment_date}\n"
                            f"Time: {appointment.appointment_time}\n"
                            f"Status: {status_str}"
                        )
                    elif action in ("updated", "rescheduled"):
                        staff_msg = (
                            f"🔄 Appointment Rescheduled - {org_name}\n\n"
                            f"Appointment ID: #{appointment.id}\n"
                            f"Patient/Customer: {appointment.customer}\n"
                            f"New Date: {appointment.appointment_date}\n"
                            f"New Time: {appointment.appointment_time}\n"
                            f"Status: {status_str}"
                        )
                    else:
                        staff_msg = f"Appointment #{appointment.id} ({appointment.customer}) updated: {action}"

                    send_whatsapp(bg_db, {
                        "messaging_product": "whatsapp",
                        "to": staff_phone,
                        "type": "text",
                        "text": {"body": staff_msg},
                    })
                    log_notification(
                        db=bg_db,
                        appointment_id=appointment.id,
                        recipient_type="staff",
                        recipient_name=staff_name,
                        recipient_contact=staff_phone,
                        channel="whatsapp",
                        action=action,
                        subject=None,
                        message_body=staff_msg,
                        status="sent",
                        org_id=org_id
                    )
                except Exception as sw_exc:
                    log_notification(
                        db=bg_db,
                        appointment_id=appointment.id,
                        recipient_type="staff",
                        recipient_name=staff_name,
                        recipient_contact=staff_phone,
                        channel="whatsapp",
                        action=action,
                        subject=None,
                        message_body=f"Staff WhatsApp error: {sw_exc}",
                        status="failed",
                        error_reason=str(sw_exc),
                        org_id=org_id
                    )
        finally:
            bg_db.close()

    if customer_phone or staff_phone:
        threading.Thread(target=_dispatch_whatsapp_async, daemon=True).start()

    return results


def send_realtime_appointment_notification_async(appointment_id: int, action: str):
    """
    Asynchronously trigger dual notifications (Customer + Staff/Doctor) in a background thread.
    Never blocks HTTP requests or database operations.
    """
    def _worker():
        db = SessionLocal()
        try:
            appointment = db.query(Appointment).filter(Appointment.id == appointment_id).first()
            if appointment:
                send_realtime_appointment_notification(db, appointment, action)
        except Exception as exc:
            logger.warning(f"Background notification error for appointment #{appointment_id}: {exc}")
        finally:
            db.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
