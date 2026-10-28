"""
Microsoft Outlook / Microsoft 365 Calendar integration through Microsoft Graph OAuth 2.0.

Required environment variables:
    MICROSOFT_CLIENT_ID
    MICROSOFT_CLIENT_SECRET
    MICROSOFT_REDIRECT_URI
    MICROSOFT_TENANT_ID   (default: common)

Required package:
    pip install msal requests
"""

import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.models.calendar_config import CalendarConfig

router = APIRouter(prefix="/microsoft-calendar", tags=["Microsoft Calendar"])

GRAPH = "https://graph.microsoft.com/v1.0"
AUTH = "https://login.microsoftonline.com"
SCOPES = ["openid", "profile", "email", "offline_access", "Calendars.ReadWrite"]


def _env(name, default=None):
    return os.getenv(name, default)


def _oauth():
    client_id = _env("MICROSOFT_CLIENT_ID")
    client_secret = _env("MICROSOFT_CLIENT_SECRET")
    redirect_uri = _env("MICROSOFT_REDIRECT_URI")
    tenant = _env("MICROSOFT_TENANT_ID", "common")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(
            500,
            "Microsoft OAuth is not configured. Set MICROSOFT_CLIENT_ID, "
            "MICROSOFT_CLIENT_SECRET and MICROSOFT_REDIRECT_URI."
        )
    return client_id, client_secret, redirect_uri, tenant


def _config(db):
    config = db.query(CalendarConfig).filter(
        CalendarConfig.provider.like("Microsoft%")
    ).first()
    if not config:
        config = CalendarConfig(provider="Microsoft Calendar")
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


def _token(config, db):
    if not config.access_token:
        if not config.refresh_token:
            raise HTTPException(401, "Microsoft Calendar is not connected")

        client_id, client_secret, redirect_uri, tenant = _oauth()
        token_response = requests.post(
            f"{AUTH}/{tenant}/oauth2/v2.0/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": config.refresh_token,
                "redirect_uri": redirect_uri,
                "scope": " ".join(SCOPES),
            },
            timeout=20,
        )

        if not token_response.ok:
            config.connected = "disconnected"
            db.commit()
            raise HTTPException(
                401, "Microsoft refresh token is invalid or expired"
            )

        token_data = token_response.json()
        config.access_token = token_data["access_token"]
        config.refresh_token = token_data.get(
            "refresh_token", config.refresh_token
        )
        config.connected = "connected"
        db.commit()

    return config.access_token


def _graph(method, path, config, db, **kwargs):
    token = _token(config, db)
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Content-Type", "application/json")

    response = requests.request(
        method, GRAPH + path, headers=headers, timeout=30, **kwargs
    )

    # One automatic refresh retry.
    if response.status_code == 401 and config.refresh_token:
        config.access_token = None
        db.commit()
        token = _token(config, db)
        headers["Authorization"] = f"Bearer {token}"
        response = requests.request(
            method, GRAPH + path, headers=headers, timeout=30, **kwargs
        )

    if not response.ok:
        raise HTTPException(response.status_code, response.text)

    return response.json() if response.content else {}


def _normalize(event):
    return {
        "id": event.get("id"),
        "subject": event.get("subject", ""),
        "summary": event.get("subject", ""),
        "body": event.get("body", {}).get("content", ""),
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
        "time_zone": event.get("start", {}).get("timeZone"),
        "status": event.get("isCancelled") and "cancelled" or "confirmed",
        "web_link": event.get("webLink"),
        "calendar_id": event.get("calendarId"),
        "updated": event.get("lastModifiedDateTime"),
    }


class EventPayload(BaseModel):
    subject: str
    start: datetime
    end: datetime
    body: str = ""
    calendar_id: str = "default"


class EventUpdatePayload(BaseModel):
    subject: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    body: Optional[str] = None
    calendar_id: str = "default"


@router.get("/oauth/authorize")
def authorize():
    client_id, _, redirect_uri, tenant = _oauth()

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "prompt": "select_account",
    }

    return {
        "authorization_url":
            f"{AUTH}/{tenant}/oauth2/v2.0/authorize?" + urlencode(params)
    }


@router.get("/oauth/callback")
def callback(code: str = Query(...), db: Session = Depends(get_db)):
    client_id, client_secret, redirect_uri, tenant = _oauth()

    response = requests.post(
        f"{AUTH}/{tenant}/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
        },
        timeout=30,
    )

    if not response.ok:
        raise HTTPException(400, f"Microsoft OAuth failed: {response.text}")

    token = response.json()
    config = _config(db)
    config.provider = "Microsoft Calendar"
    config.access_token = token.get("access_token")
    config.refresh_token = token.get("refresh_token")
    config.connected = "connected"

    # Get the actual Microsoft account identity.
    me = _graph("GET", "/me", config, db)
    config.email = me.get("mail") or me.get("userPrincipalName")

    db.commit()

    return {
        "connected": True,
        "provider": "Microsoft Calendar",
        "email": config.email,
        "has_access_token": bool(config.access_token),
        "has_refresh_token": bool(config.refresh_token),
    }


@router.get("/connection")
def connection_status(db: Session = Depends(get_db)):
    config = _config(db)

    if not config.refresh_token and not config.access_token:
        return {
            "connected": False,
            "provider": "Microsoft Calendar",
            "email": config.email,
        }

    try:
        me = _graph("GET", "/me", config, db)
        config.email = me.get("mail") or me.get("userPrincipalName")
        config.connected = "connected"
        db.commit()
        return {
            "connected": True,
            "provider": "Microsoft Calendar",
            "email": config.email,
            "has_access_token": bool(config.access_token),
            "has_refresh_token": bool(config.refresh_token),
        }
    except Exception:
        config.connected = "disconnected"
        db.commit()
        return {"connected": False, "provider": "Microsoft Calendar"}


@router.get("/calendars")
def calendars(db: Session = Depends(get_db)):
    config = _config(db)
    data = _graph("GET", "/me/calendars", config, db)
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "color": c.get("color"),
            "can_edit": c.get("canEdit"),
            "is_default": c.get("isDefaultCalendar", False),
        }
        for c in data.get("value", [])
    ]


@router.post("/select")
def select_calendar(calendar_id: str, db: Session = Depends(get_db)):
    config = _config(db)
    calendar = _graph(
        "GET", f"/me/calendars/{calendar_id}", config, db
    )
    config.provider = f"Microsoft Calendar:{calendar_id}"
    db.commit()

    return {
        "selected": True,
        "calendar_id": calendar.get("id"),
        "name": calendar.get("name"),
    }


@router.get("/events")
def events(
    start: datetime,
    end: datetime,
    calendar_id: str = "default",
    db: Session = Depends(get_db),
):
    config = _config(db)
    data = _graph(
        "GET",
        f"/me/calendars/{calendar_id}/calendarView",
        config,
        db,
        params={
            "startDateTime": start.astimezone(timezone.utc).isoformat(),
            "endDateTime": end.astimezone(timezone.utc).isoformat(),
            "$orderby": "start/dateTime",
            "$top": "500",
        },
    )
    return [_normalize(e) for e in data.get("value", [])]


@router.get("/conflicts")
def conflicts(
    start: datetime,
    end: datetime,
    calendar_id: str = "default",
    exclude_event_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    found = events(start, end, calendar_id, db)
    found = [
        e for e in found
        if e["id"] != exclude_event_id and e["status"] != "cancelled"
    ]
    return {"conflict": bool(found), "events": found}


@router.post("/events")
def create_event(
    payload: EventPayload,
    db: Session = Depends(get_db),
):
    config = _config(db)

    conflict = conflicts(
        payload.start, payload.end, payload.calendar_id, None, db
    )
    if conflict["conflict"]:
        raise HTTPException(409, "Microsoft Calendar conflict")

    body = {
        "subject": payload.subject,
        "body": {"contentType": "HTML", "content": payload.body},
        "start": {
            "dateTime": payload.start.isoformat(),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": payload.end.isoformat(),
            "timeZone": "UTC",
        },
    }

    event = _graph(
        "POST",
        f"/me/calendars/{payload.calendar_id}/events",
        config, db, json=body
    )
    return _normalize(event)


@router.put("/events/{event_id}")
def update_event(
    event_id: str,
    payload: EventUpdatePayload,
    db: Session = Depends(get_db),
):
    config = _config(db)

    if payload.start and payload.end:
        conflict = conflicts(
            payload.start, payload.end,
            payload.calendar_id, event_id, db
        )
        if conflict["conflict"]:
            raise HTTPException(409, "Microsoft Calendar conflict")

    body = {}
    if payload.subject is not None:
        body["subject"] = payload.subject
    if payload.body is not None:
        body["body"] = {
            "contentType": "HTML",
            "content": payload.body,
        }
    if payload.start:
        body["start"] = {
            "dateTime": payload.start.isoformat(),
            "timeZone": "UTC",
        }
    if payload.end:
        body["end"] = {
            "dateTime": payload.end.isoformat(),
            "timeZone": "UTC",
        }

    event = _graph(
        "PATCH",
        f"/me/calendars/{payload.calendar_id}/events/{event_id}",
        config, db, json=body
    )
    return _normalize(event)


@router.delete("/events/{event_id}")
def cancel_event(
    event_id: str,
    calendar_id: str = "default",
    db: Session = Depends(get_db),
):
    config = _config(db)
    _graph(
        "DELETE",
        f"/me/calendars/{calendar_id}/events/{event_id}",
        config, db
    )
    return {"deleted": True, "event_id": event_id}


@router.post("/disconnect")
def disconnect(db: Session = Depends(get_db)):
    config = _config(db)

    # Microsoft Graph does not expose a general-purpose revoke endpoint
    # equivalent to Google's revoke API. Removing stored tokens disconnects
    # this application's local session; the user can revoke consent from
    # their Microsoft account's app permissions separately.
    config.access_token = None
    config.refresh_token = None
    config.connected = "disconnected"
    db.commit()

    return {"connected": False, "disconnected": True}
