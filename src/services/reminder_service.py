"""
Reminder database + scheduling service.

The module provides:
- Persistent Reminder SQLAlchemy model
- 24-hour and 1-hour reminder creation
- Appointment-booking hook
- Background worker (no extra scheduler dependency required)
- WhatsApp + Email delivery
- Retry handling
- sent / failed state tracking
- Error reason persistence

Import this module during backend startup so the model is registered and
the worker starts.
"""

import logging
import os
import re
import threading
import time as time_module
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import Boolean, Column, DateTime, Enum as SAEnum, Integer, String, Text
from sqlalchemy.orm import Session

from src.config.database import Base, SessionLocal

logger = logging.getLogger(__name__)


class ReminderChannel(str, Enum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class ReminderStatus(str, Enum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, nullable=False, index=True)
    channel = Column(SAEnum(ReminderChannel), nullable=False)
    reminder_type = Column(String(20), nullable=False)  # 24h / 1h
    scheduled_at = Column(DateTime(timezone=True), nullable=False, index=True)

    status = Column(
        SAEnum(ReminderStatus),
        nullable=False,
        default=ReminderStatus.SCHEDULED,
        index=True,
    )

    recipient = Column(String(255), nullable=True)
    template_name = Column(String(150), nullable=True)

    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)

    sent_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    error_reason = Column(Text, nullable=True)

    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ReminderService:
    REMINDER_OFFSETS = {
        "24h": timedelta(hours=24),
        "1h": timedelta(hours=1),
    }

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def appointment_datetime(appointment) -> datetime:
        """
        Convert appointment display-string fields to a timezone-aware datetime.
        Handles non-standard abbreviations like 'Sept' by normalising before parse.
        Falls back to dateutil.parser for maximum flexibility.
        """
        date_str = str(appointment.appointment_date or "").strip()
        time_str = str(appointment.appointment_time or "").strip()
        value = f"{date_str} {time_str}"

        # Normalise common non-standard month abbreviations
        normalized = re.sub(r'\bSept\b', 'Sep', value, flags=re.IGNORECASE)
        normalized = re.sub(r'\bJune\b', 'Jun', normalized, flags=re.IGNORECASE)
        normalized = re.sub(r'\bJuly\b', 'Jul', normalized, flags=re.IGNORECASE)

        formats = (
            "%d %b %Y %I:%M %p",
            "%d %B %Y %I:%M %p",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %I:%M %p",
            "%d/%m/%Y %I:%M %p",
            "%d/%m/%Y %H:%M",
        )
        for fmt in formats:
            try:
                return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        # Final fallback: dateutil (handles almost any human-readable date string)
        try:
            from dateutil import parser as du_parser
            return du_parser.parse(normalized, dayfirst=True).replace(tzinfo=timezone.utc)
        except Exception:
            pass

        raise ValueError(f"Cannot parse appointment date/time: '{value}'. "
                         f"Supported formats: '24 Sep 2026 10:00 AM', '2026-09-24 10:00', etc.")

    def create_for_appointment(self, appointment) -> list[Reminder]:
        """
        Create both 24-hour and 1-hour reminders when an appointment is booked.
        Duplicate reminders are prevented.
        """
        appointment_at = self.appointment_datetime(appointment)
        created = []

        channels = []
        if getattr(appointment, "phone", None):
            channels.append(
                (
                    ReminderChannel.WHATSAPP,
                    appointment.phone,
                    os.getenv("WHATSAPP_REMINDER_TEMPLATE", "appointment_reminder"),
                )
            )
        if getattr(appointment, "email", None):
            channels.append(
                (
                    ReminderChannel.EMAIL,
                    appointment.email,
                    os.getenv("EMAIL_REMINDER_TEMPLATE", "appointment_reminder"),
                )
            )

        for reminder_type, offset in self.REMINDER_OFFSETS.items():
            scheduled_at = appointment_at - offset

            for channel, recipient, template in channels:
                exists = (
                    self.db.query(Reminder)
                    .filter(
                        Reminder.appointment_id == appointment.id,
                        Reminder.channel == channel,
                        Reminder.reminder_type == reminder_type,
                    )
                    .first()
                )
                if exists:
                    continue

                reminder = Reminder(
                    appointment_id=appointment.id,
                    channel=channel,
                    reminder_type=reminder_type,
                    scheduled_at=scheduled_at,
                    status=ReminderStatus.SCHEDULED,
                    recipient=recipient,
                    template_name=template,
                )
                self.db.add(reminder)
                created.append(reminder)

        self.db.commit()
        for reminder in created:
            self.db.refresh(reminder)
        return created

    def mark_failed(self, reminder: Reminder, error: Exception | str):
        reminder.status = ReminderStatus.FAILED
        reminder.failed_at = datetime.now(timezone.utc)
        reminder.error_reason = str(error)
        reminder.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    def mark_sent(self, reminder: Reminder):
        reminder.status = ReminderStatus.SENT
        reminder.sent_at = datetime.now(timezone.utc)
        reminder.failed_at = None
        reminder.error_reason = None
        reminder.updated_at = datetime.now(timezone.utc)
        self.db.commit()

    def retry(self, reminder_id: int) -> Reminder:
        reminder = self.db.query(Reminder).filter(Reminder.id == reminder_id).first()
        if not reminder:
            raise ValueError("Reminder not found")

        reminder.status = ReminderStatus.SCHEDULED
        reminder.enabled = True
        reminder.failed_at = None
        reminder.error_reason = None
        reminder.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(reminder)
        return reminder

    def deliver(self, reminder: Reminder) -> bool:
        """
        Deliver one reminder through the already-created WhatsApp/Email routes.
        Direct service calls are used so the worker does not make HTTP calls to
        its own application.
        """
        from src.models.appointment import Appointment

        appointment = (
            self.db.query(Appointment)
            .filter(Appointment.id == reminder.appointment_id)
            .first()
        )
        if not appointment:
            raise ValueError("Appointment no longer exists")

        if reminder.channel == ReminderChannel.WHATSAPP:
            if not appointment.phone:
                raise ValueError("Appointment has no phone number")

            try:
                from src.routes.whatsapp_routes import _send, _appointment_params

                label_text = (
                    "1 hour before your appointment"
                    if reminder.reminder_type == "1h"
                    else "1 day before your appointment"
                )
                # Build template params: customer, department, staff, date, time, + label
                params = _appointment_params(appointment) + [label_text]
                result = _send(
                    self.db,
                    {
                        "messaging_product": "whatsapp",
                        "to": appointment.phone,
                        "type": "template",
                        "template": {
                            "name": reminder.template_name or "appointment_reminder",
                            "language": {"code": os.getenv("WHATSAPP_LANGUAGE", "en_US")},
                            "components": [
                                {
                                    "type": "body",
                                    "parameters": [
                                        {"type": "text", "text": str(value)}
                                        for value in params
                                    ],
                                }
                            ],
                        },
                    },
                )
                if not result:
                    raise ValueError("WhatsApp provider returned an empty response")
                logger.info(
                    f"[REMINDER] WhatsApp sent to {appointment.phone} — {reminder.reminder_type} reminder"
                )
                return True
            except Exception as e:
                logger.warning(f"WhatsApp reminder delivery skipped/failed: {e}")
                raise ValueError(str(e))


        if reminder.channel == ReminderChannel.EMAIL:
            if not appointment.email:
                raise ValueError("Appointment has no email address")

            try:
                from src.routes.email_routes import _send, _details

                label = "1 Hour Before" if reminder.reminder_type == "1h" else "1 Day Before"
                subject = (
                    f"⏰ Appointment Reminder ({label}) — "
                    f"{appointment.appointment_date}, {appointment.appointment_time}"
                )
                body = (
                    f"Dear {appointment.customer},\n\n"
                    f"This is a friendly reminder that your appointment is "
                    f"{'coming up in 1 hour' if reminder.reminder_type == '1h' else 'tomorrow'}.\n\n"
                    f"{_details(appointment)}\n\n"
                    f"Please be on time. If you need to reschedule, contact us as soon as possible.\n\n"
                    f"Thank you,\nAI Receptionist"
                )

                # Rich HTML version
                html_body = f"""
<html><body style="font-family:sans-serif;color:#333">
  <h2 style="color:#4f46e5">⏰ Appointment Reminder</h2>
  <p>Dear <strong>{appointment.customer}</strong>,</p>
  <p>Your appointment is <strong>"""
                html_body += (
                    "coming up in 1 hour!" if reminder.reminder_type == "1h" else "scheduled for tomorrow!"
                )
                html_body += f"""
  </strong></p>
  <table style="border-collapse:collapse;width:100%;max-width:480px">
    <tr><td style="padding:6px 12px;background:#f1f5f9"><b>Date</b></td><td style="padding:6px 12px">{appointment.appointment_date}</td></tr>
    <tr><td style="padding:6px 12px"><b>Time</b></td><td style="padding:6px 12px">{appointment.appointment_time}</td></tr>
    <tr><td style="padding:6px 12px;background:#f1f5f9"><b>Department</b></td><td style="padding:6px 12px">{appointment.department}</td></tr>
    <tr><td style="padding:6px 12px"><b>Staff</b></td><td style="padding:6px 12px">{appointment.staff}</td></tr>
    <tr><td style="padding:6px 12px;background:#f1f5f9"><b>Duration</b></td><td style="padding:6px 12px">{appointment.duration}</td></tr>
  </table>
  <p style="margin-top:16px">If you need to reschedule, please contact us as soon as possible.</p>
  <p>Thank you,<br><strong>AI Receptionist</strong></p>
</body></html>"""

                result = _send(
                    self.db,
                    appointment.email,
                    subject,
                    body,
                    html_body=html_body,
                )
                if not result.get("sent"):
                    raise ValueError(result.get("error", "Email provider rejected the message"))
                logger.info(f"[REMINDER] Email sent to {appointment.email} — {reminder.reminder_type} reminder")
                return True
            except Exception as e:
                logger.warning(f"Email reminder delivery skipped/failed: {e}")
                raise ValueError(str(e))

        raise ValueError(f"Unsupported reminder channel: {reminder.channel}")

    def process_due(self, now: datetime | None = None) -> dict:
        """Process due reminders and persist sent/failed status."""
        now = now or datetime.now(timezone.utc)
        sent = 0
        failed = 0

        due = (
            self.db.query(Reminder)
            .filter(
                Reminder.enabled.is_(True),
                Reminder.status.in_(
                    [ReminderStatus.SCHEDULED, ReminderStatus.FAILED]
                ),
                Reminder.scheduled_at <= now,
                Reminder.attempts < Reminder.max_attempts,
            )
            .order_by(Reminder.scheduled_at.asc())
            .limit(100)
            .all()
        )

        if due:
            logger.info(f"[REMINDER WORKER] Processing {len(due)} due reminder(s)...")

        for reminder in due:
            reminder.status = ReminderStatus.PROCESSING
            reminder.attempts += 1
            reminder.updated_at = now
            self.db.commit()

            try:
                self.deliver(reminder)
                self.mark_sent(reminder)
                sent += 1
                # Persist to NotificationLog for UI visibility
                try:
                    from src.models.notification_log import NotificationLog
                    from src.models.appointment import Appointment
                    appt = self.db.query(Appointment).filter(Appointment.id == reminder.appointment_id).first()
                    action_label = f"reminder_{reminder.reminder_type}"
                    nl = NotificationLog(
                        appointment_id=reminder.appointment_id,
                        recipient_type="customer",
                        recipient_name=appt.customer if appt else "Unknown",
                        recipient_contact=reminder.recipient,
                        channel=reminder.channel.value,
                        action=action_label,
                        subject=f"Appointment Reminder ({reminder.reminder_type})",
                        message_body=(
                            f"Reminder sent to {reminder.recipient} via {reminder.channel.value} "
                            f"({reminder.reminder_type} before appointment #{reminder.appointment_id})"
                        ),
                        status="sent",
                    )
                    self.db.add(nl)
                    self.db.commit()
                except Exception as log_err:
                    logger.debug(f"[REMINDER] NotificationLog write note: {log_err}")
            except Exception as exc:
                logger.warning(f"Reminder {reminder.id} delivery failed: {exc}")
                self.mark_failed(reminder, exc)
                failed += 1
                # Log failure to NotificationLog
                try:
                    from src.models.notification_log import NotificationLog
                    nl = NotificationLog(
                        appointment_id=reminder.appointment_id,
                        recipient_type="customer",
                        recipient_contact=reminder.recipient,
                        channel=reminder.channel.value,
                        action=f"reminder_{reminder.reminder_type}",
                        subject=f"Appointment Reminder ({reminder.reminder_type}) — FAILED",
                        message_body=str(exc),
                        status="failed",
                        error_reason=str(exc),
                    )
                    self.db.add(nl)
                    self.db.commit()
                except Exception:
                    pass

        if due:
            logger.info(f"[REMINDER WORKER] Done — sent:{sent}, failed:{failed}")

        return {"processed": len(due), "sent": sent, "failed": failed}



def schedule_reminders_for_appointment(appointment, db: Session | None = None):
    """Convenience function to call immediately after appointment creation."""
    owns_session = db is None
    db = db or SessionLocal()
    try:
        return ReminderService(db).create_for_appointment(appointment)
    finally:
        if owns_session:
            db.close()


def _appointment_insert_hook(mapper, connection, target):
    """
    SQLAlchemy after_insert hook.

    It creates reminders automatically for newly inserted appointments. A
    separate DB session is intentionally used because the mapper connection is
    not safe for arbitrary ORM writes during the flush.
    """
    try:
        if not target.id:
            return
        db = SessionLocal()
        try:
            ReminderService(db).create_for_appointment(target)
        finally:
            db.close()
    except Exception:
        # Booking itself must not fail because reminder scheduling failed.
        logger.exception(
            "Could not create reminders for appointment %s", getattr(target, "id", None)
        )


def register_appointment_hook():
    from sqlalchemy import event
    from src.models.appointment import Appointment

    if not event.contains(Appointment, "after_insert", _appointment_insert_hook):
        event.listen(Appointment, "after_insert", _appointment_insert_hook)


def ensure_reminder_table():
    """Create the reminder table if the application's startup does not run migrations."""
    Base.metadata.create_all(bind=__import__("src.config.database", fromlist=["engine"]).engine)


_worker_started = False
_worker_lock = threading.Lock()


def start_reminder_worker(interval_seconds: int = 60):
    """Start the background reminder worker once per application process."""
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True

    def worker():
        while True:
            db = None
            try:
                db = SessionLocal()
                ReminderService(db).process_due()
            except Exception:
                logger.exception("Reminder worker cycle failed")
            finally:
                if db:
                    db.close()
            time_module.sleep(max(30, interval_seconds))

    thread = threading.Thread(
        target=worker,
        name="appointment-reminder-worker",
        daemon=True,
    )
    thread.start()


def initialize_reminder_system():
    """
    Call once from backend startup.
    Registers automatic booking -> reminder creation and starts the worker.
    """
    ensure_reminder_table()
    register_appointment_hook()
    start_reminder_worker(
        int(os.getenv("REMINDER_WORKER_INTERVAL_SECONDS", "60"))
    )
