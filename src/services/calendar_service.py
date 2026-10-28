import os
import logging
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any
from dotenv import load_dotenv
load_dotenv()

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.models.calendar_config import CalendarConfig
from src.config.env import settings

logger = logging.getLogger(__name__)

_CALENDAR_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}


class CalendarService:
    """
    Google Calendar integration with token auto-refresh, real-time availability sync,
    and event CRUD operations.
    """

    def __init__(self, db: Session, user_id: Optional[int] = None):
        self.db = db
        self.user_id = user_id

    def get_connected_config(self) -> Optional[CalendarConfig]:
        """Return the active connected calendar config for user or organization."""
        if self.user_id:
            config = self.db.query(CalendarConfig).filter(
                CalendarConfig.user_id == self.user_id,
                CalendarConfig.connected == "connected"
            ).first()
            if config:
                return config

        # Fallback to any connected Google Calendar in DB
        return self.db.query(CalendarConfig).filter(
            CalendarConfig.connected == "connected"
        ).first()

    def _config(self) -> CalendarConfig:
        config = self.get_connected_config()
        if not config:
            raise HTTPException(400, "Google Calendar is not connected")
        return config

    def _mark_disconnected(self, config: CalendarConfig, reason: str):
        """Marks calendar configuration as disconnected in database so future calls skip it."""
        try:
            config.connected = "disconnected"
            self.db.add(config)
            self.db.commit()
            logger.warning(
                f"[CALENDAR_AUTH_ERROR] Google Calendar disconnected for user {config.user_id}: {reason}"
            )
        except Exception as ex:
            logger.error(f"[CALENDAR_AUTH_ERROR] Failed to mark calendar disconnected: {ex}")

    def _google_service(self, config: CalendarConfig):
        """Constructs an authorized Google Calendar API client with auto-refresh."""
        if not config.access_token and not config.refresh_token:
            raise HTTPException(400, "Google Calendar credentials missing")

        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        client_id = os.getenv("GOOGLE_CLIENT_ID") or getattr(settings, "GOOGLE_CLIENT_ID", None)
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or getattr(settings, "GOOGLE_CLIENT_SECRET", None)
        scopes = (os.getenv("GOOGLE_SCOPES") or getattr(settings, "GOOGLE_SCOPES", None) or "https://www.googleapis.com/auth/calendar").split()

        credentials = Credentials(
            token=config.access_token,
            refresh_token=config.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=scopes,
        )

        # Automatically refresh expired credentials
        if (not credentials.valid or credentials.expired) and credentials.refresh_token:
            try:
                from google.auth.transport.requests import Request
                credentials.refresh(Request())
                config.access_token = credentials.token
                if credentials.refresh_token:
                    config.refresh_token = credentials.refresh_token
                config.connected = "connected"
                self.db.add(config)
                self.db.commit()
                self.db.refresh(config)
                logger.info(f"Google Calendar credentials successfully refreshed for user {config.user_id}")
            except Exception as e:
                err_str = str(e)
                self._mark_disconnected(config, err_str)
                raise HTTPException(401, f"Google Calendar token expired: {e}")

        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def list_events(self, start: datetime, end: datetime, calendar_id: str = "primary") -> List[Dict[str, Any]]:
        """List events from connected Google Calendar between start and end times with 30s TTL cache."""
        config = self.get_connected_config()
        if not config:
            return []

        time_min = start.astimezone(timezone.utc).isoformat() if start.tzinfo else (start.replace(tzinfo=timezone.utc)).isoformat()
        time_max = end.astimezone(timezone.utc).isoformat() if end.tzinfo else (end.replace(tzinfo=timezone.utc)).isoformat()
        cache_key = f"{config.id}:{calendar_id}:{time_min}:{time_max}"

        import time as _time
        now_mono = _time.monotonic()
        if cache_key in _CALENDAR_CACHE:
            ts, cached_events = _CALENDAR_CACHE[cache_key]
            if now_mono - ts < 30.0:
                return cached_events

        try:
            service = self._google_service(config)

            result = service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            events = []
            for event in result.get("items", []):
                if event.get("status") != "cancelled":
                    events.append(self._normalize_event(event))

            _CALENDAR_CACHE[cache_key] = (now_mono, events)
            return events
        except HTTPException:
            return []
        except Exception as e:
            err_str = str(e)
            if "invalid_grant" in err_str or "unauthorized_client" in err_str or "RefreshError" in err_str or "401" in err_str:
                self._mark_disconnected(config, err_str)
            else:
                logger.error(f"Error fetching Google Calendar events: {e}")
            return []

    def get_busy_intervals(self, start: datetime, end: datetime, calendar_id: str = "primary") -> List[Tuple[datetime, datetime]]:
        """Returns busy time intervals [start, end] for slot availability calculation."""
        events = self.list_events(start, end, calendar_id)
        intervals = []
        for ev in events:
            try:
                s_str = ev.get("start")
                e_str = ev.get("end")
                if not s_str or not e_str:
                    continue
                # Parse ISO strings
                s_dt = datetime.fromisoformat(s_str.replace("Z", "+00:00")).replace(tzinfo=None)
                e_dt = datetime.fromisoformat(e_str.replace("Z", "+00:00")).replace(tzinfo=None)
                intervals.append((s_dt, e_dt))
            except Exception as e:
                logger.warning(f"Could not parse Google event interval: {e}")
        return intervals

    def has_conflict(self, start: datetime, end: datetime, calendar_id: str = "primary",
                     exclude_event_id: Optional[str] = None) -> bool:
        """Checks whether the requested [start, end] period conflicts with existing Google events."""
        events = self.list_events(start, end, calendar_id)
        s_naive = start.replace(tzinfo=None) if start.tzinfo else start
        e_naive = end.replace(tzinfo=None) if end.tzinfo else end

        for ev in events:
            if ev.get("id") == exclude_event_id:
                continue
            try:
                ev_s = datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).replace(tzinfo=None)
                ev_e = datetime.fromisoformat(ev["end"].replace("Z", "+00:00")).replace(tzinfo=None)
                # Overlap test: start < ev_e and end > ev_s
                if s_naive < ev_e and e_naive > ev_s:
                    return True
            except Exception:
                continue
        return False

    def create_event(self, *, summary: str, start: datetime, end: datetime,
                     description: str = "", calendar_id: str = "primary") -> Optional[Dict[str, Any]]:
        """Creates an appointment event in Google Calendar."""
        config = self.get_connected_config()
        if not config:
            return None
        try:
            service = self._google_service(config)

            if self.has_conflict(start, end, calendar_id):
                logger.warning(f"Google Calendar conflict detected for {summary} at {start} - {end}")
                raise HTTPException(409, "Google Calendar conflict: requested time is already booked")

            tz_name = getattr(config, "timezone", None) or os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
            start_iso = start.isoformat() if start.tzinfo else start.strftime("%Y-%m-%dT%H:%M:%S")
            end_iso = end.isoformat() if end.tzinfo else end.strftime("%Y-%m-%dT%H:%M:%S")

            body = {
                "summary": summary,
                "description": description,
                "start": {"dateTime": start_iso, "timeZone": tz_name},
                "end": {"dateTime": end_iso, "timeZone": tz_name},
            }
            event = service.events().insert(
                calendarId=calendar_id, body=body
            ).execute()
            _CALENDAR_CACHE.clear()
            logger.info(f"Google Calendar event created: ID={event.get('id')}, Summary={summary}")
            return self._normalize_event(event)
        except HTTPException as he:
            if he.status_code == 409:
                raise
            logger.warning(f"[CALENDAR_SYNC_SKIPPED] Google Calendar event creation skipped: {he.detail}")
            return None
        except Exception as e:
            err_str = str(e)
            if "invalid_grant" in err_str or "unauthorized_client" in err_str or "RefreshError" in err_str or "401" in err_str:
                self._mark_disconnected(config, err_str)
            else:
                logger.error(f"Failed to create Google Calendar event: {e}")
            return None

    def update_event(self, event_id: str, *, summary: Optional[str] = None,
                     start: Optional[datetime] = None, end: Optional[datetime] = None,
                     description: Optional[str] = None,
                     calendar_id: str = "primary") -> Optional[Dict[str, Any]]:
        """Updates an existing Google Calendar event."""
        try:
            config = self._config()
            service = self._google_service(config)

            existing = service.events().get(
                calendarId=calendar_id, eventId=event_id
            ).execute()

            if start and end and self.has_conflict(
                start, end, calendar_id, exclude_event_id=event_id
            ):
                raise HTTPException(409, "Google Calendar conflict: new time is already booked")

            tz_name = getattr(config, "timezone", None) or os.getenv("DEFAULT_TIMEZONE", "Asia/Kolkata")
            if summary is not None:
                existing["summary"] = summary
            if description is not None:
                existing["description"] = description
            if start is not None:
                start_iso = start.isoformat() if start.tzinfo else start.strftime("%Y-%m-%dT%H:%M:%S")
                existing["start"] = {"dateTime": start_iso, "timeZone": tz_name}
            if end is not None:
                end_iso = end.isoformat() if end.tzinfo else end.strftime("%Y-%m-%dT%H:%M:%S")
                existing["end"] = {"dateTime": end_iso, "timeZone": tz_name}

            updated = service.events().update(
                calendarId=calendar_id, eventId=event_id, body=existing
            ).execute()
            return self._normalize_event(updated)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to update Google Calendar event {event_id}: {e}")
            return None

    def delete_event(self, event_id: str, calendar_id: str = "primary") -> Dict[str, Any]:
        """Deletes/cancels an event in Google Calendar."""
        try:
            config = self._config()
            service = self._google_service(config)
            service.events().delete(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            logger.info(f"Deleted Google Calendar event: {event_id}")
            return {"event_id": event_id, "deleted": True}
        except Exception as e:
            logger.warning(f"Could not delete Google Calendar event {event_id}: {e}")
            return {"event_id": event_id, "deleted": False, "error": str(e)}

    @staticmethod
    def _normalize_event(event: dict) -> dict:
        start = event.get("start", {})
        end = event.get("end", {})
        return {
            "id": event.get("id"),
            "summary": event.get("summary", ""),
            "description": event.get("description", ""),
            "status": event.get("status"),
            "html_link": event.get("htmlLink"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "attendees": [
                {"email": a.get("email"), "response": a.get("responseStatus")}
                for a in event.get("attendees", [])
            ],
            "updated": event.get("updated"),
        }
