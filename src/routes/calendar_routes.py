"""
Google Calendar OAuth + calendar/event integration.

Environment variables:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REDIRECT_URI (default: http://localhost:8000/api/v1/calendar/oauth/callback)
    GOOGLE_SCOPES
    FRONTEND_URL (default: http://localhost:5173)
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, List
from urllib.parse import urlencode
# Allow oauthlib to accept HTTP in reverse-proxy environments and relax scope checks
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from urllib.parse import quote_plus

from src.config.database import get_db
from src.config.env import settings
from src.middleware.auth_middleware import get_current_user
from src.models.calendar_config import CalendarConfig
from src.models.user import User
from src.services.calendar_service import CalendarService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar", tags=["Google Calendar"])


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    if hasattr(settings, name):
        val = getattr(settings, name)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
        elif val == "":
            return default
    val = os.getenv(name)
    return str(val).strip() if (val is not None and str(val).strip() != "") else default


def _get_frontend_url(request: Optional[Request] = None) -> str:
    explicit = _env("FRONTEND_URL")
    if explicit:
        return explicit.rstrip("/")
    if request:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if "vrapi.aminoid.in" in host:
            return "https://vr.aminoid.in"
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        if host and "localhost" not in host and "127.0.0.1" not in host:
            return f"{proto}://{host}"
    return "http://localhost:5173"


def _oauth_config(request: Optional[Request] = None):
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    
    # Check if a custom redirect URI is provided in env
    redirect_uri = _env("GOOGLE_REDIRECT_URI")
    if not redirect_uri:
        if request:
            host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8000"
            proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
            redirect_uri = f"{proto}://{host}/api/v1/calendar/oauth/callback"
        else:
            redirect_uri = "http://localhost:8000/api/v1/calendar/oauth/callback"

    if not client_id or not client_secret:
        raise HTTPException(
            400,
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in your backend .env."
        )
    return client_id, client_secret, redirect_uri


def _scopes():
    scopes_str = _env("GOOGLE_SCOPES")
    if scopes_str:
        return scopes_str.split()
    return [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/userinfo.email"
    ]


class EventPayload(BaseModel):
    summary: str
    start: datetime
    end: datetime
    description: str = ""
    calendar_id: str = "primary"


class EventUpdatePayload(BaseModel):
    summary: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    description: Optional[str] = None
    calendar_id: str = "primary"


class CalendarConfigSchema(BaseModel):
    provider: Optional[str] = None
    email: Optional[str] = None
    connected: Optional[str] = None
    working_days: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    break_start: Optional[str] = None
    break_end: Optional[str] = None
    default_appointment_duration: Optional[int] = None
    staff_appointment_duration: Optional[int] = None
    services_handled_by_staff: Optional[bool] = None
    staff_availability: Optional[bool] = None
    leave_management: Optional[bool] = None
    holiday_blocking: Optional[bool] = None
    online_booking: Optional[bool] = None


@router.get("/oauth/authorize")
def google_authorize(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Return the Google OAuth authorization URL for the current user."""
    client_id, _, redirect_uri = _oauth_config(request)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(_scopes()),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": str(current_user.id),
    }
    return {
        "authorization_url":
            "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    }


@router.get("/oauth/callback")
def google_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Exchange OAuth authorization code, persist tokens, and redirect user back to app."""
    frontend_url = _get_frontend_url(request)

    if error:
        logger.warning(f"Google OAuth returned error: {error}")
        return RedirectResponse(
            url=f"{frontend_url}/admin/calendar-integration?status=error&error={quote_plus(error)}"
        )

    if not code:
        logger.warning("Google OAuth callback called without code")
        return RedirectResponse(
            url=f"{frontend_url}/admin/calendar-integration?status=error&error={quote_plus('No authorization code received')}"
        )

    try:
        from google_auth_oauthlib.flow import Flow

        client_id, client_secret, redirect_uri = _oauth_config(request)
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=_scopes(),
        )
        flow.redirect_uri = redirect_uri

        # Perform token exchange
        flow.fetch_token(code=code)
        credentials = flow.credentials

        # Safely resolve target user & organization to prevent FK constraint failures
        target_user = None
        if state and state.isdigit():
            target_user = db.query(User).filter(User.id == int(state)).first()

        if not target_user:
            target_user = db.query(User).first()

        resolved_user_id = target_user.id if target_user else 1
        resolved_org_id = target_user.organization_id if (target_user and target_user.organization_id) else 1

        config = db.query(CalendarConfig).filter(CalendarConfig.user_id == resolved_user_id).first()
        if not config:
            config = CalendarConfig(user_id=resolved_user_id, organization_id=resolved_org_id)
            db.add(config)

        config.organization_id = resolved_org_id
        config.user_id = resolved_user_id
        config.provider = "Google Calendar"
        config.access_token = credentials.token
        if credentials.refresh_token:
            config.refresh_token = credentials.refresh_token
        config.connected = "connected"

        # Resolve the authenticated Google account email
        account_email = None
        try:
            from googleapiclient.discovery import build
            service = build("oauth2", "v2", credentials=credentials, cache_discovery=False)
            info = service.userinfo().get().execute()
            account_email = info.get("email")
        except Exception as e:
            logger.warning(f"Could not retrieve Google userinfo: {e}")

        if not account_email:
            try:
                from googleapiclient.discovery import build
                cal_service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
                primary_cal = cal_service.calendars().get(calendarId="primary").execute()
                account_email = primary_cal.get("id") or primary_cal.get("summary")
            except Exception as e:
                logger.warning(f"Could not retrieve primary calendar email: {e}")

        if account_email:
            config.email = account_email

        db.commit()
        db.refresh(config)

        redirect_target = f"{frontend_url}/admin/calendar-integration?status=connected&email={quote_plus(config.email or '')}"
        logger.info(f"Google Calendar successfully connected for user {resolved_user_id} ({config.email}). Redirecting to {redirect_target}")
        return RedirectResponse(url=redirect_target)

    except Exception as exc:
        logger.exception(f"Unhandled error during Google OAuth callback: {exc}")
        return RedirectResponse(
            url=f"{frontend_url}/admin/calendar-integration?status=error&error={quote_plus(str(exc))}"
        )


@router.get("/connection")
def connection_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Connection status reflects live stored OAuth credentials for the active user."""
    cal_svc = CalendarService(db, user_id=current_user.id)
    config = cal_svc.get_connected_config()

    if not config or config.connected != "connected":
        return {
            "connected": False,
            "provider": "Google Calendar",
            "email": config.email if config else None,
        }

    try:
        # Validate and refresh token if needed
        cal_svc._google_service(config)
        return {
            "connected": True,
            "provider": config.provider,
            "email": config.email,
            "has_access_token": bool(config.access_token),
            "has_refresh_token": bool(config.refresh_token),
        }
    except Exception as e:
        logger.warning(f"Google Calendar connection check failed: {e}")
        return {
            "connected": False,
            "provider": "Google Calendar",
            "email": config.email,
        }


@router.get("/calendars")
def fetch_calendars(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Fetch calendars belonging to the authenticated user's Google account."""
    try:
        cal_svc = CalendarService(db, user_id=current_user.id)
        config = cal_svc._config()
        service = cal_svc._google_service(config)

        result = service.calendarList().list().execute()
        return [
            {
                "id": item.get("id"),
                "summary": item.get("summary"),
                "description": item.get("description"),
                "primary": item.get("primary", False),
                "access_role": item.get("accessRole"),
                "time_zone": item.get("timeZone"),
            }
            for item in result.get("items", [])
        ]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in fetch_calendars: {e}")
        raise HTTPException(400, f"Could not retrieve Google Calendars: {e}")


@router.post("/select")
def select_calendar(
    calendar_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate and persist the selected Google calendar for the user."""
    try:
        cal_svc = CalendarService(db, user_id=current_user.id)
        config = cal_svc._config()
        service = cal_svc._google_service(config)
        calendar = service.calendars().get(calendarId=calendar_id).execute()

        config.provider = f"Google Calendar:{calendar_id}"
        db.commit()
        db.refresh(config)

        return {
            "selected": True,
            "calendar_id": calendar.get("id"),
            "summary": calendar.get("summary"),
            "time_zone": calendar.get("timeZone"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error selecting calendar: {e}")
        raise HTTPException(400, f"Could not select Google Calendar: {e}")


@router.get("/events")
def fetch_events(
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List Google Calendar events for the active user."""
    cal_svc = CalendarService(db, user_id=current_user.id)
    return cal_svc.list_events(start, end, calendar_id)


@router.get("/events/conflicts")
@router.get("/conflicts")
def check_conflicts(
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
    exclude_event_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check for conflicting Google Calendar events."""
    cal_svc = CalendarService(db, user_id=current_user.id)
    events = cal_svc.list_events(start, end, calendar_id)
    conflicts = [
        e for e in events
        if e.get("id") != exclude_event_id and e.get("status") != "cancelled"
    ]
    return {"conflict": bool(conflicts), "events": conflicts}


@router.post("/events")
def create_event(
    payload: EventPayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new Google Calendar event."""
    try:
        cal_svc = CalendarService(db, user_id=current_user.id)
        event = cal_svc.create_event(
            summary=payload.summary,
            start=payload.start,
            end=payload.end,
            description=payload.description,
            calendar_id=payload.calendar_id,
        )
        if not event:
            raise HTTPException(400, "Failed to create Google Calendar event. Check authorization and calendar ID.")
        return event
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating Google event: {e}")
        raise HTTPException(400, f"Failed to create Google Calendar event: {e}")


@router.put("/events/{event_id}")
def update_event(
    event_id: str,
    payload: EventUpdatePayload,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an existing Google Calendar event."""
    try:
        cal_svc = CalendarService(db, user_id=current_user.id)
        event = cal_svc.update_event(
            event_id=event_id,
            summary=payload.summary,
            start=payload.start,
            end=payload.end,
            description=payload.description,
            calendar_id=payload.calendar_id,
        )
        if not event:
            raise HTTPException(400, "Failed to update Google Calendar event")
        return event
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating Google event: {e}")
        raise HTTPException(400, f"Failed to update Google Calendar event: {e}")


@router.delete("/events/{event_id}")
def cancel_event(
    event_id: str,
    calendar_id: str = "primary",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a Google Calendar event."""
    cal_svc = CalendarService(db, user_id=current_user.id)
    return cal_svc.delete_event(event_id=event_id, calendar_id=calendar_id)


@router.post("/disconnect")
def disconnect_google_calendar(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Revoke Google OAuth tokens and disconnect calendar."""
    config = db.query(CalendarConfig).filter(CalendarConfig.user_id == current_user.id).first()
    if not config:
        return {"connected": False, "disconnected": True}

    if config.access_token:
        try:
            import requests
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": config.access_token},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Revoking Google token failed: {e}")

    config.access_token = None
    config.refresh_token = None
    config.connected = "disconnected"
    db.commit()

    return {"connected": False, "disconnected": True}


@router.get("/config", response_model=CalendarConfigSchema)
def get_calendar_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Return stored calendar provider state for active user."""
    config = db.query(CalendarConfig).filter(CalendarConfig.user_id == current_user.id).first()
    if not config:
        return CalendarConfigSchema(
            provider="Google Calendar", 
            connected="disconnected",
            working_days="Monday,Tuesday,Wednesday,Thursday,Friday",
            start_time="09:00",
            end_time="17:00",
            break_start="13:00",
            break_end="14:00",
            default_appointment_duration=30,
            staff_appointment_duration=30,
            services_handled_by_staff=True,
            staff_availability=True,
            leave_management=True,
            holiday_blocking=True,
            online_booking=True
        )
    return CalendarConfigSchema(
        provider=config.provider or "Google Calendar",
        email=config.email,
        connected=config.connected or "disconnected",
        working_days=config.working_days or "Monday,Tuesday,Wednesday,Thursday,Friday",
        start_time=config.start_time or "09:00",
        end_time=config.end_time or "17:00",
        break_start=config.break_start or "13:00",
        break_end=config.break_end or "14:00",
        default_appointment_duration=config.default_appointment_duration or 30,
        staff_appointment_duration=config.staff_appointment_duration or 30,
        services_handled_by_staff=config.services_handled_by_staff if config.services_handled_by_staff is not None else True,
        staff_availability=config.staff_availability if config.staff_availability is not None else True,
        leave_management=config.leave_management if config.leave_management is not None else True,
        holiday_blocking=config.holiday_blocking if config.holiday_blocking is not None else True,
        online_booking=config.online_booking if config.online_booking is not None else True,
    )


@router.post("/config", response_model=CalendarConfigSchema)
def update_calendar_config(
    payload: CalendarConfigSchema,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Persist calendar config state for active user."""
    config = db.query(CalendarConfig).filter(CalendarConfig.user_id == current_user.id).first()
    if not config:
        config = CalendarConfig(user_id=current_user.id)
        db.add(config)

    if payload.provider is not None:
        config.provider = payload.provider
    if payload.email is not None:
        config.email = payload.email
    if payload.connected is not None:
        config.connected = payload.connected
    if payload.working_days is not None:
        config.working_days = payload.working_days
    if payload.start_time is not None:
        config.start_time = payload.start_time
    if payload.end_time is not None:
        config.end_time = payload.end_time
    if payload.break_start is not None:
        config.break_start = payload.break_start
    if payload.break_end is not None:
        config.break_end = payload.break_end
    if payload.default_appointment_duration is not None:
        config.default_appointment_duration = payload.default_appointment_duration
    if payload.staff_appointment_duration is not None:
        config.staff_appointment_duration = payload.staff_appointment_duration
    if payload.services_handled_by_staff is not None:
        config.services_handled_by_staff = payload.services_handled_by_staff
    if payload.staff_availability is not None:
        config.staff_availability = payload.staff_availability
    if payload.leave_management is not None:
        config.leave_management = payload.leave_management
    if payload.holiday_blocking is not None:
        config.holiday_blocking = payload.holiday_blocking
    if payload.online_booking is not None:
        config.online_booking = payload.online_booking

    db.commit()
    db.refresh(config)
    return CalendarConfigSchema(
        provider=config.provider,
        email=config.email,
        connected=config.connected,
        working_days=config.working_days,
        start_time=config.start_time,
        end_time=config.end_time,
        break_start=config.break_start,
        break_end=config.break_end,
        default_appointment_duration=config.default_appointment_duration,
        staff_appointment_duration=config.staff_appointment_duration,
        services_handled_by_staff=config.services_handled_by_staff,
        staff_availability=config.staff_availability,
        leave_management=config.leave_management,
        holiday_blocking=config.holiday_blocking,
        online_booking=config.online_booking,
    )
