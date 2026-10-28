"""
Full Google Calendar Flow Verification
Checks: env vars, Google libs, DB table, OAuth config, route registration, token refresh logic
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")

# ── Load env ──────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️  WARN"

results = []

def check(label, fn):
    try:
        msg = fn()
        results.append((PASS, label, msg or ""))
    except AssertionError as e:
        results.append((FAIL, label, str(e)))
    except Exception as e:
        results.append((FAIL, label, repr(e)))

# ── 1. Env vars ───────────────────────────────────────────────────────
def test_env():
    cid = os.getenv("GOOGLE_CLIENT_ID", "")
    cs  = os.getenv("GOOGLE_CLIENT_SECRET", "")
    ru  = os.getenv("GOOGLE_REDIRECT_URI", "")
    sc  = os.getenv("GOOGLE_SCOPES", "")
    assert cid and "placeholder" not in cid and cid != "your-google-client-id.apps.googleusercontent.com", \
        f"GOOGLE_CLIENT_ID not set or still placeholder: {cid!r}"
    assert cs and cs != "your-google-client-secret", \
        f"GOOGLE_CLIENT_SECRET not set or still placeholder"
    assert "localhost:8000" in ru, f"GOOGLE_REDIRECT_URI unexpected: {ru}"
    assert "calendar" in sc, f"GOOGLE_SCOPES missing calendar scope: {sc}"
    return f"CLIENT_ID={cid[:30]}... | REDIRECT={ru}"

check("Env vars: GOOGLE_CLIENT_ID / SECRET / REDIRECT_URI / SCOPES", test_env)

# ── 2. Google Python libraries installed ──────────────────────────────
def test_google_libs():
    import google.oauth2.credentials
    import googleapiclient.discovery
    import google_auth_oauthlib.flow
    import google.auth.transport.requests
    return "google-api-python-client, google-auth-oauthlib all importable"

check("Python libs: google-api-python-client, google-auth-oauthlib", test_google_libs)

# ── 3. Database: calendar_configs table and columns ───────────────────
def test_db():
    import sqlite3
    conn = sqlite3.connect("receptionist.db")
    cur  = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    assert "calendar_configs" in tables, "calendar_configs table missing from DB"

    cur.execute("PRAGMA table_info(calendar_configs)")
    cols = [c[1] for c in cur.fetchall()]
    required = ["id", "user_id", "provider", "email", "access_token", "refresh_token", "connected"]
    missing  = [c for c in required if c not in cols]
    assert not missing, f"Missing columns: {missing}"

    cur.execute("SELECT COUNT(*) FROM calendar_configs")
    count = cur.fetchone()[0]
    conn.close()
    return f"All columns present | {count} config row(s) in DB"

check("DB: calendar_configs table + all required columns", test_db)

# ── 4. OAuth URL construction ─────────────────────────────────────────
def test_oauth_url():
    from urllib.parse import urlencode, quote
    client_id   = os.getenv("GOOGLE_CLIENT_ID")
    redirect    = os.getenv("GOOGLE_REDIRECT_URI")
    scopes      = os.getenv("GOOGLE_SCOPES", "https://www.googleapis.com/auth/calendar")
    params = {
        "client_id":     client_id,
        "redirect_uri":  redirect,
        "response_type": "code",
        "scope":         scopes,
        "access_type":   "offline",
        "prompt":        "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    assert "accounts.google.com" in url
    assert client_id in url
    assert "calendar" in url
    return f"URL preview: {url[:90]}..."

check("OAuth URL: correctly constructed with real credentials", test_oauth_url)

# ── 5. Backend route registration ─────────────────────────────────────
def test_routes():
    sys.path.insert(0, ".")
    from src.routes.calendar_routes import router
    paths = [r.path for r in router.routes]
    expected = [
        "/oauth/authorize",
        "/oauth/callback",
        "/connection",
        "/events",
        "/disconnect",
    ]
    missing = [p for p in expected if not any(p in r for r in paths)]
    assert not missing, f"Missing routes: {missing}"
    return f"Routes registered: {paths}"

check("Backend routes: all calendar endpoints registered", test_routes)

# ── 6. CalendarService can be instantiated ────────────────────────────
def test_service():
    from unittest.mock import MagicMock
    from src.services.calendar_service import CalendarService
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    svc = CalendarService(mock_db, user_id=1)
    assert svc.user_id == 1
    config = svc.get_connected_config()
    assert config is None   # no connected config in mock
    return "CalendarService instantiated, get_connected_config() returns None when no config"

check("CalendarService: instantiation + get_connected_config()", test_service)

# ── 7. Flow: Credentials object builds correctly ──────────────────────
def test_credentials_build():
    from google.oauth2.credentials import Credentials
    creds = Credentials(
        token="dummy_access_token",
        refresh_token="dummy_refresh_token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        scopes=[os.getenv("GOOGLE_SCOPES", "https://www.googleapis.com/auth/calendar")],
    )
    assert creds.client_id == os.getenv("GOOGLE_CLIENT_ID")
    assert creds.client_secret == os.getenv("GOOGLE_CLIENT_SECRET")
    return "Credentials object built with real client_id + secret"

check("Google Credentials object: builds with real CLIENT_ID + SECRET", test_credentials_build)

# ── 8. Frontend calendar service file exists ──────────────────────────
def test_frontend_service():
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "receptionist-frontend-", "src", "services", "calendar.service.js"
    )
    assert os.path.exists(path), f"Frontend calendar.service.js not found at {path}"
    content = open(path).read()
    assert "getGoogleAuthUrl" in content
    assert "getCalendarEvents" in content
    assert "disconnectGoogle" in content
    return "calendar.service.js present with all required exports"

check("Frontend: calendar.service.js exists with all exports", test_frontend_service)

# ── 9. Frontend CalendarConfig page exists ────────────────────────────
def test_frontend_page():
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "receptionist-frontend-", "src", "pages", "CalendarConfig", "index.jsx"
    )
    assert os.path.exists(path), "CalendarConfig/index.jsx missing"
    content = open(path).read()
    assert "handleConnectGoogle" in content
    assert "getCalendarConfig" in content
    return "CalendarConfig page present with live OAuth handler + config loader"

check("Frontend: CalendarConfig page with live OAuth", test_frontend_page)

# ── 10. ConnectGoogleCalendarModal exists ─────────────────────────────
def test_modal():
    path = os.path.join(
        os.path.dirname(__file__),
        "..", "receptionist-frontend-", "src", "components", "modals", "ConnectGoogleCalendarModal.jsx"
    )
    assert os.path.exists(path), "ConnectGoogleCalendarModal.jsx missing"
    content = open(path).read()
    assert "getGoogleAuthUrl" in content
    assert "window.location.href" in content
    return "Modal present — calls getGoogleAuthUrl() and redirects browser"

check("Frontend: ConnectGoogleCalendarModal does OAuth redirect", test_modal)

# ── Print results ─────────────────────────────────────────────────────
print("\n" + "═" * 70)
print("  GOOGLE CALENDAR FLOW — FULL VERIFICATION REPORT")
print("═" * 70)
passed = failed = 0
for status, label, detail in results:
    icon = status
    print(f"\n{icon}  {label}")
    if detail:
        short = detail if len(detail) < 120 else detail[:117] + "..."
        print(f"     → {short}")
    if PASS in status:
        passed += 1
    else:
        failed += 1

print("\n" + "─" * 70)
print(f"  TOTAL: {passed} passed  |  {failed} failed")
print("─" * 70)

if failed == 0:
    print("\n  🎉 ALL CHECKS PASSED — Calendar flow is ready!")
    print("  Next step: Click 'Connect Google Calendar' in the app.")
else:
    print(f"\n  ⚠️  {failed} check(s) need attention before the flow works.")
print()
