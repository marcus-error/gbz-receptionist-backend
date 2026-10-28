import os
import sys
sys.path.insert(0, os.path.abspath("."))

import pytest
from fastapi.testclient import TestClient
from src.app import fastapi_app
from src.services.ai_service import ai_service
from src.middleware.auth_middleware import get_current_user
from src.models.user import User

client = TestClient(fastapi_app)

def mock_get_current_user():
    user = User()
    user.id = 1
    user.email = "test@example.com"
    user.role = "admin"
    return user

fastapi_app.dependency_overrides[get_current_user] = mock_get_current_user


def test_smart_qa_fallback_sentence_selection():
    """Verify that smart QA sentence matching returns the relevant answer instead of a random first sentence."""
    context = (
        "ABC General Hospital is located in Chennai Main Road.\n"
        "Visiting hours for ICU patients are strictly between 4 PM and 6 PM.\n"
        "Outpatient OPD registration is available from 8 AM to 1 PM daily.\n"
        "Emergency services operate 24 hours a day."
    )
    
    # Query 1: Visiting hours
    ans_visiting = ai_service.generate_response(
        text="What are the visiting hours for ICU?",
        context=context,
        session_history=[],
        intent="general_question",
        organization_id=1
    )
    assert "visiting" in ans_visiting.lower() or "4 pm" in ans_visiting.lower() or "6 pm" in ans_visiting.lower() or "icu" in ans_visiting.lower()

    # Query 2: OPD registration
    ans_opd = ai_service.generate_response(
        text="When is OPD registration open?",
        context=context,
        session_history=[],
        intent="general_question",
        organization_id=1
    )
    assert "opd" in ans_opd.lower() or "8 am" in ans_opd.lower() or "registration" in ans_opd.lower()


def test_calendar_oauth_authorize_unconfigured_returns_400(monkeypatch):
    """Verify that requesting Google OAuth authorize when unconfigured returns HTTP 400 instead of HTTP 500."""
    monkeypatch.setattr("src.config.env.settings.GOOGLE_CLIENT_ID", "")
    monkeypatch.setattr("src.config.env.settings.GOOGLE_CLIENT_SECRET", "")
    response = client.get("/api/v1/calendar/oauth/authorize")
    assert response.status_code == 400
    msg = str(response.json())
    assert "Google OAuth is not configured" in msg


def test_calendar_connection_returns_clean_status():
    """Verify that connection endpoint returns HTTP 200 without HTTP 500."""
    response = client.get("/api/v1/calendar/connection")
    assert response.status_code == 200


def test_calendar_list_unconfigured_returns_400():
    """Verify that fetching calendars when unconfigured returns 400 instead of 500."""
    response = client.get("/api/v1/calendar/calendars")
    assert response.status_code in (400, 401)
    msg = str(response.json())
    assert "Google Calendar is not connected" in msg or "configured" in msg or "Could not retrieve" in msg

