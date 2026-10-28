import html
import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.email_config import EmailConfig
from src.models.appointment import Appointment, AppointmentStatus

router = APIRouter(prefix="/email", tags=["Email"])


class EmailConfigSchema(BaseModel):
    provider: Optional[str] = "Resend"
    api_key: Optional[str] = None
    from_email: Optional[str] = "onboarding@resend.dev"
    smtp_host: Optional[str] = "smtp.gmail.com"
    smtp_port: Optional[str] = "587"
    email_address: Optional[str] = None
    password: Optional[str] = None
    sender_name: Optional[str] = "AI Receptionist"
    encryption: Optional[str] = "TLS"
    template: Optional[str] = None
    staff_template: Optional[str] = None
    is_active: Optional[bool] = False
    connection_status: Optional[str] = "NOT_CONFIGURED"
    missing_fields: Optional[list] = []


class EmailMessageSchema(BaseModel):
    to: str
    subject: str
    body: str
    html_body: Optional[str] = None


class AppointmentEmailSchema(BaseModel):
    appointment_id: int
    subject: Optional[str] = None


DEFAULT_CUSTOMER_TEMPLATE = (
    "Hello {{customer_name}},\n\n"
    "Your {{booking_purpose}} has been confirmed with {{organization_name}}.\n\n"
    "Booking Details:\n"
    "  Reference ID: #{{booking_ref}}\n"
    "  Purpose: {{booking_purpose}}\n"
    "  Staff / Specialist: {{staff_name}}\n"
    "  Service / Product: {{service_product}}\n"
    "  Date: {{date}}\n"
    "  Time: {{time}}\n\n"
    "Location & Contact:\n"
    "  Organization: {{organization_name}}\n"
    "  Contact: {{organization_contact}}\n\n"
    "Thank you for choosing {{organization_name}}!\n"
    "If you need to reschedule or cancel, please contact us."
)

DEFAULT_STAFF_TEMPLATE = (
    "Hello {{staff_name}},\n\n"
    "A new {{booking_purpose}} has been booked and assigned to you at {{organization_name}}.\n\n"
    "Booking & Customer Details:\n"
    "  Reference ID: #{{booking_ref}}\n"
    "  Customer Name: {{customer_name}}\n"
    "  Customer Phone: {{customer_phone}}\n"
    "  Customer Email: {{customer_email}}\n"
    "  Booking Purpose: {{booking_purpose}}\n"
    "  Service / Product: {{service_product}}\n"
    "  Date: {{date}}\n"
    "  Time: {{time}}\n\n"
    "Please review your schedule and prepare accordingly."
)


def _config(db, organization_id: int = 1):
    config = db.query(EmailConfig).filter(EmailConfig.organization_id == organization_id).first()
    if not config:
        config = EmailConfig(
            organization_id=organization_id,
            provider="Resend",
            api_key=None,
            from_email="onboarding@resend.dev",
            smtp_host="smtp.gmail.com",
            smtp_port="587",
            email_address=None,
            password=None,
            sender_name="AI Receptionist",
            template=DEFAULT_CUSTOMER_TEMPLATE,
            staff_template=DEFAULT_STAFF_TEMPLATE,
        )
        db.add(config)
        db.commit()
        db.refresh(config)
    else:
        # Ensure default templates exist if null
        updated = False
        if not config.template:
            config.template = DEFAULT_CUSTOMER_TEMPLATE
            updated = True
        if not getattr(config, "staff_template", None):
            config.staff_template = DEFAULT_STAFF_TEMPLATE
            updated = True
        if updated:
            db.commit()
            db.refresh(config)
    return config



import socket

def _smtp(config):
    if not config.email_address or not config.password:
        raise HTTPException(400, "Email address and password are required for SMTP")

    try:
        port = int(config.smtp_port or 587)
    except ValueError:
        raise HTTPException(400, "Invalid SMTP port")

    # Lower connection timeout to 8 seconds to prevent frontend 30-second timeout
    timeout_sec = 8
    try:
        if (config.encryption or "").upper() == "SSL" or port == 465:
            server = smtplib.SMTP_SSL(
                config.smtp_host, port, context=ssl.create_default_context(), timeout=timeout_sec
            )
        else:
            server = smtplib.SMTP(config.smtp_host, port, timeout=timeout_sec)
            if (config.encryption or "TLS").upper() in ("TLS", "STARTTLS"):
                server.starttls(context=ssl.create_default_context())

        server.login(config.email_address, config.password)
        return server
    except smtplib.SMTPAuthenticationError as exc:
        err_msg = exc.smtp_error.decode("utf-8", errors="ignore") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        raise HTTPException(400, f"SMTP Authentication Failed: {err_msg}. If using Gmail, you MUST use a 16-character App Password (not your main Google account password).")
    except (socket.timeout, TimeoutError):
        raise HTTPException(504, f"SMTP Connection Timed Out connecting to {config.smtp_host}:{port}. Outbound SMTP ports 587/465 are likely blocked by your cloud host/firewall. Use Resend or SendGrid API (HTTPS Port 443).")
    except ConnectionRefusedError:
        raise HTTPException(502, f"SMTP Connection Refused by {config.smtp_host}:{port}. Verify SMTP host and port.")
    except Exception as exc:
        raise HTTPException(500, f"SMTP Error: {str(exc)}")


def _send(db, to, subject, body, html_body=None, organization_id: int = 1):
    config = _config(db, organization_id=organization_id)
    provider = (config.provider or "Resend").strip()
    api_key = config.api_key or os.getenv("RESEND_API_KEY") or os.getenv("SENDGRID_API_KEY")

    # 1. RESEND API (HTTPS Port 443 — Standard, Never blocked on Railway)
    if provider.lower() in ("resend", "api") or (api_key and str(api_key).startswith("re_")):
        resend_key = api_key or os.getenv("RESEND_API_KEY")
        if not resend_key:
            return {"sent": False, "status": "failed", "to": to, "subject": subject, "error": "Resend API key is not configured"}

        sender_name = config.sender_name or "AI Receptionist"
        from_addr = config.from_email or config.email_address or os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")
        from_header = f"{sender_name} <{from_addr}>" if sender_name and "<" not in from_addr else from_addr

        payload = {
            "from": from_header,
            "to": [to],
            "subject": subject,
            "text": body,
        }
        if html_body:
            payload["html"] = html_body

        try:
            res = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )
            data = res.json() if res.content else {}
            if res.ok:
                return {"sent": True, "status": "sent", "provider": "Resend", "to": to, "subject": subject, "id": data.get("id")}
            return {"sent": False, "status": "failed", "provider": "Resend", "to": to, "subject": subject, "error": data}
        except Exception as exc:
            return {"sent": False, "status": "failed", "provider": "Resend", "to": to, "subject": subject, "error": str(exc)}

    # 2. SENDGRID API (HTTPS Port 443 — Standard, Never blocked on Railway)
    if provider.lower() in ("sendgrid",) or (api_key and str(api_key).startswith("SG.")):
        sendgrid_key = api_key or os.getenv("SENDGRID_API_KEY")
        if not sendgrid_key:
            return {"sent": False, "status": "failed", "to": to, "subject": subject, "error": "SendGrid API key is not configured"}

        sender_name = config.sender_name or "AI Receptionist"
        from_addr = config.from_email or config.email_address or os.getenv("SENDGRID_FROM_EMAIL", "receptionist@yourdomain.com")

        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": from_addr, "name": sender_name},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        if html_body:
            payload["content"].append({"type": "text/html", "value": html_body})

        try:
            res = requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {sendgrid_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )
            if res.status_code in (200, 201, 202):
                msg_id = res.headers.get("X-Message-Id") or res.headers.get("x-message-id")
                return {"sent": True, "status": "sent", "provider": "SendGrid", "to": to, "subject": subject, "id": msg_id}
            data = res.json() if res.content else {}
            return {"sent": False, "status": "failed", "provider": "SendGrid", "to": to, "subject": subject, "error": data}
        except Exception as exc:
            return {"sent": False, "status": "failed", "provider": "SendGrid", "to": to, "subject": subject, "error": str(exc)}

    # 3. SMTP FALLBACK (Classic SMTP)
    message = EmailMessage()
    from_header = f"{config.sender_name} <{config.email_address}>" if config.sender_name else config.email_address
    message["From"] = from_header
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    try:
        server = _smtp(config)
        try:
            server.send_message(message)
        finally:
            server.quit()
        msg_id = message.get("Message-ID") or f"<smtp-{datetime.now(timezone.utc).timestamp()}@callzenza>"
        return {"sent": True, "status": "sent", "provider": "SMTP", "to": to, "subject": subject, "id": str(msg_id)}
    except Exception as exc:
        return {"sent": False, "status": "failed", "provider": "SMTP", "to": to, "subject": subject, "error": str(exc)}


def _send_batch(db, messages_list: list, organization_id: int = 1) -> list:
    """
    Send multiple emails in a single persistent SMTP connection or rapid API calls.
    Avoids reconnecting/reauthenticating per recipient, speeding up dual notifications 3x-5x.
    """
    if not messages_list:
        return []

    config = _config(db, organization_id=organization_id)
    provider = (config.provider or "Resend").strip()
    api_key = config.api_key or os.getenv("RESEND_API_KEY") or os.getenv("SENDGRID_API_KEY")

    if not provider.lower().startswith("smtp") and (api_key or provider.lower() in ("resend", "sendgrid")):
        results = []
        for m in messages_list:
            results.append(_send(db, m["to"], m["subject"], m["body"], m.get("html_body"), organization_id=organization_id))
        return results

    results = []
    server = None
    try:
        server = _smtp(config)
        for m in messages_list:
            msg = EmailMessage()
            from_header = f"{config.sender_name} <{config.email_address}>" if config.sender_name else config.email_address
            msg["From"] = from_header
            msg["To"] = m["to"]
            msg["Subject"] = m["subject"]
            msg.set_content(m["body"])
            if m.get("html_body"):
                msg.add_alternative(m["html_body"], subtype="html")
            try:
                server.send_message(msg)
                msg_id = msg.get("Message-ID") or f"<smtp-{datetime.now(timezone.utc).timestamp()}@callzenza>"
                results.append({"sent": True, "status": "sent", "provider": "SMTP", "to": m["to"], "subject": m["subject"], "id": str(msg_id)})
            except Exception as item_err:
                results.append({"sent": False, "status": "failed", "provider": "SMTP", "to": m["to"], "subject": m["subject"], "error": str(item_err)})
    except Exception as exc:
        for m in messages_list[len(results):]:
            results.append({"sent": False, "status": "failed", "provider": "SMTP", "to": m["to"], "subject": m["subject"], "error": str(exc)})
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
    return results


def _appointment(db, appointment_id):
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(404, "Appointment not found")
    if not appt.email:
        raise HTTPException(400, "Appointment has no customer email")
    return appt


def _details(appt):
    return (
        f"Appointment #{appt.id}\n"
        f"Customer: {appt.customer}\n"
        f"Department: {appt.department}\n"
        f"Staff: {appt.staff}\n"
        f"Date: {appt.appointment_date}\n"
        f"Time: {appt.appointment_time}\n"
        f"Duration: {appt.duration}\n"
        f"Status: {appt.status.value if hasattr(appt.status, 'value') else appt.status}"
    )


def _ics(appt):
    try:
        import re
        raw = f"{appt.appointment_date} {appt.appointment_time}"
        # Normalise non-standard month abbreviations that strptime doesn't know
        raw = re.sub(r'\bSept\b', 'Sep', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\bJune\b', 'Jun', raw, flags=re.IGNORECASE)
        raw = re.sub(r'\bJuly\b', 'Jul', raw, flags=re.IGNORECASE)
        try:
            start = datetime.strptime(raw, "%d %b %Y %I:%M %p")
        except ValueError:
            # dateutil fallback for any other format
            from dateutil import parser as du_parser
            start = du_parser.parse(raw, dayfirst=True)
    except Exception:
        return None

    import re
    m = re.search(r"\d+", appt.duration or "")
    minutes = int(m.group()) if m else 30
    end = start + timedelta(minutes=minutes)

    def stamp(dt):
        return dt.strftime("%Y%m%dT%H%M%S")

    uid = f"appointment-{appt.id}@ai-receptionist"
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//AI Receptionist//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp(datetime.utcnow())}\r\n"
        f"DTSTART:{stamp(start)}\r\n"
        f"DTEND:{stamp(end)}\r\n"
        f"SUMMARY:{appt.department} - {appt.customer}\r\n"
        f"DESCRIPTION:Staff: {appt.staff}\\nAppointment ID: {appt.id}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


from src.middleware.auth_middleware import get_current_user
from src.models.user import User

@router.get("/config", response_model=EmailConfigSchema)
def get_email_config(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    config = _config(db, organization_id=org_id)
    
    missing_fields = []
    provider = (config.provider or "Resend").strip()
    is_active = False

    if provider.lower() in ("resend", "api"):
        if not config.api_key:
            missing_fields.append("api_key")
        if not config.from_email:
            missing_fields.append("from_email")
        is_active = bool(config.api_key)
    elif provider.lower() == "sendgrid":
        if not config.api_key:
            missing_fields.append("api_key")
        if not config.from_email:
            missing_fields.append("from_email")
        is_active = bool(config.api_key)
    elif provider.lower() == "smtp":
        if not config.email_address:
            missing_fields.append("email_address")
        if not config.password:
            missing_fields.append("password")
        if not config.smtp_host:
            missing_fields.append("smtp_host")
        if not config.smtp_port:
            missing_fields.append("smtp_port")
        is_active = len(missing_fields) == 0

    conn_status = "ACTIVE" if is_active else ("INCOMPLETE" if missing_fields and (config.api_key or config.email_address) else "NOT_CONFIGURED")

    return EmailConfigSchema(
        provider=config.provider or "Resend",
        api_key="••••••••••••••••" if config.api_key else None,
        from_email=config.from_email or "onboarding@resend.dev",
        smtp_host=config.smtp_host,
        smtp_port=config.smtp_port,
        email_address=config.email_address,
        password="••••••••••••••••" if config.password else None,
        sender_name=config.sender_name,
        encryption=config.encryption,
        template=config.template,
        staff_template=config.staff_template,
        is_active=is_active,
        connection_status=conn_status,
        missing_fields=missing_fields,
    )


@router.post("/config", response_model=EmailConfigSchema)
def update_email_config(
    payload: EmailConfigSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    config = _config(db, organization_id=org_id)
    config.provider = payload.provider or "Resend"
    if payload.api_key and payload.api_key != "••••••••••••••••":
        config.api_key = payload.api_key
    config.from_email = payload.from_email
    config.smtp_host = payload.smtp_host
    config.smtp_port = payload.smtp_port
    config.email_address = payload.email_address
    if payload.password and payload.password != "••••••••••••••••":
        config.password = payload.password
    config.sender_name = payload.sender_name
    config.encryption = payload.encryption
    config.template = payload.template
    config.staff_template = payload.staff_template
    db.commit()
    db.refresh(config)
    return get_email_config(db, current_user=current_user)


@router.get("/validate")
def validate_email_connection(
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    config = _config(db, organization_id=org_id)
    provider = (config.provider or "Resend").strip()
    api_key = config.api_key or (os.getenv("RESEND_API_KEY") if provider.lower() in ("resend", "api") else os.getenv("SENDGRID_API_KEY"))

    # 1. RESEND
    if provider.lower() in ("resend", "api"):
        if not api_key:
            return {
                "connected": False,
                "provider": "Resend",
                "missing_fields": ["api_key"],
                "error": "Resend API key is missing. Please configure your API key."
            }
        try:
            res = requests.get(
                "https://api.resend.com/api-keys",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5
            )
            if res.status_code == 200:
                return {
                    "connected": True,
                    "provider": "Resend",
                    "note": "Resend API verified successfully (HTTPS Port 443)."
                }
            elif res.status_code in (401, 403):
                return {
                    "connected": False,
                    "provider": "Resend",
                    "error": "Resend authentication failed: Invalid or expired API Key."
                }
            else:
                return {
                    "connected": True,
                    "provider": "Resend",
                    "note": f"Resend API key configured ({api_key[:6]}...)."
                }
        except Exception as e:
            return {
                "connected": False,
                "provider": "Resend",
                "error": f"Failed to reach Resend API: {e}"
            }

    # 2. SENDGRID
    if provider.lower() == "sendgrid":
        if not api_key:
            return {
                "connected": False,
                "provider": "SendGrid",
                "missing_fields": ["api_key"],
                "error": "SendGrid API key is missing. Please configure your API key."
            }
        try:
            res = requests.get(
                "https://api.sendgrid.com/v3/scopes",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5
            )
            if res.status_code == 200:
                return {
                    "connected": True,
                    "provider": "SendGrid",
                    "note": "SendGrid API verified successfully (HTTPS Port 443)."
                }
            elif res.status_code in (401, 403):
                return {
                    "connected": False,
                    "provider": "SendGrid",
                    "error": "SendGrid authentication failed: Invalid or expired API Key."
                }
            else:
                return {
                    "connected": True,
                    "provider": "SendGrid",
                    "note": f"SendGrid API key configured ({api_key[:6]}...)."
                }
        except Exception as e:
            return {
                "connected": False,
                "provider": "SendGrid",
                "error": f"Failed to reach SendGrid API: {e}"
            }

    # 3. SMTP
    missing_smtp = []
    if not config.email_address:
        missing_smtp.append("email_address")
    if not config.password:
        missing_smtp.append("password")
    if not config.smtp_host:
        missing_smtp.append("smtp_host")
    if not config.smtp_port:
        missing_smtp.append("smtp_port")

    if missing_smtp:
        return {
            "connected": False,
            "provider": "SMTP",
            "missing_fields": missing_smtp,
            "error": f"Missing required SMTP fields: {', '.join(missing_smtp)}"
        }

    try:
        server = _smtp(config)
        server.noop()
        server.quit()
        return {
            "connected": True,
            "provider": "SMTP",
            "smtp_host": config.smtp_host,
            "note": f"SMTP handshake and authentication verified ({config.email_address})."
        }
    except Exception as exc:
        err_msg = str(exc)
        if hasattr(exc, "detail"):
            err_msg = exc.detail
        return {
            "connected": False,
            "provider": "SMTP",
            "error": err_msg
        }


@router.post("/send")
def send_email(
    payload: EmailMessageSchema,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user)
):
    org_id = current_user.organization_id if current_user else 1
    result = _send(
        db, payload.to, payload.subject, payload.body, payload.html_body, organization_id=org_id
    )
    if not result.get("sent"):
        err_info = result.get("error") or result
        err_msg = err_info if isinstance(err_info, str) else str(err_info)
        raise HTTPException(400, err_msg)
    return result


@router.post("/appointments/confirmation")
def confirmation_email(
    payload: AppointmentEmailSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    org_id = getattr(appt, "organization_id", 1) or 1
    subject = payload.subject or f"Appointment Confirmation #{appt.id}"
    body = (
        f"Hello {appt.customer},\n\n"
        "Your appointment is confirmed.\n\n"
        f"{_details(appt)}\n\n"
        "Thank you."
    )
    ics = _ics(appt)
    result = _send(db, appt.email, subject, body, organization_id=org_id)
    return result


@router.post("/appointments/cancellation")
def cancellation_email(
    payload: AppointmentEmailSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    org_id = getattr(appt, "organization_id", 1) or 1
    return _send(
        db,
        appt.email,
        payload.subject or f"Appointment Cancelled #{appt.id}",
        f"Hello {appt.customer},\n\nYour appointment has been cancelled.\n\n{_details(appt)}",
        organization_id=org_id
    )


@router.post("/appointments/reschedule")
def reschedule_email(
    payload: AppointmentEmailSchema, db: Session = Depends(get_db)
):
    appt = _appointment(db, payload.appointment_id)
    return _send(
        db,
        appt.email,
        payload.subject or f"Appointment Rescheduled #{appt.id}",
        f"Hello {appt.customer},\n\nYour appointment has been rescheduled.\n\n{_details(appt)}",
        f"<p>Hello {html.escape(appt.customer)},</p><p>Your appointment has been rescheduled.</p>"
        f"<pre>{html.escape(_details(appt))}</pre>",
    )


@router.post("/reminders/{hours_before}")
def send_reminders(
    hours_before: int,
    db: Session = Depends(get_db),
):
    if hours_before not in (24, 1):
        raise HTTPException(400, "Only 24-hour and 1-hour reminders are supported")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    target = now + timedelta(hours=hours_before)
    window_end = target + timedelta(minutes=5)
    sent, failed = [], []

    for appt in db.query(Appointment).filter(
        Appointment.status == AppointmentStatus.CONFIRMED
    ).all():
        if not appt.email:
            continue
        try:
            start = datetime.strptime(
                f"{appt.appointment_date} {appt.appointment_time}",
                "%d %b %Y %I:%M %p",
            )
        except ValueError:
            continue

        if not (target <= start <= window_end):
            continue

        result = _send(
            db,
            appt.email,
            f"Appointment Reminder - {hours_before} hour(s)",
            f"Hello {appt.customer},\n\nThis is your {hours_before}-hour appointment reminder.\n\n{_details(appt)}",
        )
        (sent if result["sent"] else failed).append(
            {"appointment_id": appt.id, "result": result}
        )

    return {
        "hours_before": hours_before,
        "delivery_status": {"sent": len(sent), "failed": len(failed)},
        "sent": sent,
        "failed": failed,
    }


@router.get("/delivery-status")
def email_delivery_status():
    return {
        "status": "available",
        "statuses": ["sent", "failed"],
        "note": "Resend and SendGrid API deliveries are reported synchronously over HTTPS.",
    }


@router.get("/failed")
def failed_email_handling():
    return {
        "status": "available",
        "retry_supported": True,
        "note": "Failed sends return an explicit error and can be retried through the same endpoint.",
    }
