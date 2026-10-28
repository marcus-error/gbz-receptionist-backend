"""
Reminder management API routes.
Provides endpoints for listing reminder status, manually triggering due reminders,
and retrying failed reminders.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/reminders", tags=["Reminders"])


@router.get("")
def list_reminders(
    appointment_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List reminders with optional filters."""
    from src.services.reminder_service import Reminder, ReminderStatus

    q = db.query(Reminder)

    if appointment_id:
        q = q.filter(Reminder.appointment_id == appointment_id)

    if status and status != "all":
        try:
            q = q.filter(Reminder.status == ReminderStatus(status))
        except ValueError:
            pass

    if channel and channel != "all":
        q = q.filter(Reminder.channel == channel)

    total = q.count()
    reminders = q.order_by(Reminder.scheduled_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "results": [
            {
                "id": r.id,
                "appointment_id": r.appointment_id,
                "channel": r.channel.value,
                "reminder_type": r.reminder_type,
                "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
                "status": r.status.value,
                "recipient": r.recipient,
                "attempts": r.attempts,
                "max_attempts": r.max_attempts,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "failed_at": r.failed_at.isoformat() if r.failed_at else None,
                "error_reason": r.error_reason,
                "enabled": r.enabled,
            }
            for r in reminders
        ],
    }


@router.post("/process-due")
def trigger_process_due(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually trigger the reminder worker to process all due reminders right now."""
    from src.services.reminder_service import ReminderService
    result = ReminderService(db).process_due()
    return {
        "message": f"Processed {result['processed']} reminder(s): {result['sent']} sent, {result['failed']} failed.",
        **result,
    }


@router.post("/{reminder_id}/retry")
def retry_reminder(
    reminder_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reset a failed reminder so it will be retried on the next worker cycle."""
    from src.services.reminder_service import ReminderService
    try:
        reminder = ReminderService(db).retry(reminder_id)
        return {
            "message": f"Reminder {reminder_id} reset to scheduled.",
            "id": reminder.id,
            "status": reminder.status.value,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/appointments/{appointment_id}/create")
def create_reminders_for_appointment(
    appointment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually create 24h + 1h reminders for an existing appointment."""
    from src.models.appointment import Appointment
    from src.services.reminder_service import ReminderService

    appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    try:
        reminders = ReminderService(db).create_for_appointment(appt)
        return {
            "message": f"Created {len(reminders)} reminder(s) for appointment #{appointment_id}.",
            "reminders": [
                {
                    "id": r.id,
                    "channel": r.channel.value,
                    "reminder_type": r.reminder_type,
                    "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
                    "status": r.status.value,
                    "recipient": r.recipient,
                }
                for r in reminders
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
