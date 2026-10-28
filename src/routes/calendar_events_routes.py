from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middleware.auth_middleware import get_current_user
from src.models.user import User
from src.services.calendar_service import CalendarService

router = APIRouter(prefix="/calendar/events", tags=["Calendar Events"])


class CalendarEventCreate(BaseModel):
    summary: str
    start: datetime
    end: datetime
    description: str = ""
    calendar_id: str = "primary"


class CalendarEventUpdate(BaseModel):
    summary: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    description: Optional[str] = None
    calendar_id: str = "primary"


@router.get("")
def get_calendar_events(
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch real events from the connected provider for React."""
    return CalendarService(db, user_id=current_user.id).list_events(start, end, calendar_id)


@router.get("/conflicts")
def check_conflicts(
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
    exclude_event_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return {
        "conflict": CalendarService(db, user_id=current_user.id).has_conflict(
            start, end, calendar_id, exclude_event_id
        )
    }


@router.post("")
def create_calendar_event(
    payload: CalendarEventCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a provider event after a confirmed booking."""
    return CalendarService(db, user_id=current_user.id).create_event(
        summary=payload.summary,
        start=payload.start,
        end=payload.end,
        description=payload.description,
        calendar_id=payload.calendar_id,
    )


@router.put("/{event_id}")
def update_calendar_event(
    event_id: str,
    payload: CalendarEventUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update provider event when an appointment is rescheduled."""
    return CalendarService(db, user_id=current_user.id).update_event(
        event_id,
        summary=payload.summary,
        start=payload.start,
        end=payload.end,
        description=payload.description,
        calendar_id=payload.calendar_id,
    )


@router.delete("/{event_id}")
def cancel_calendar_event(
    event_id: str,
    calendar_id: str = "primary",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete/cancel the provider event when an appointment is cancelled."""
    return CalendarService(db, user_id=current_user.id).delete_event(event_id, calendar_id)

