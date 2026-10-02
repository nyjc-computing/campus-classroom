"""Verification for issue #9: Campus OAuth incremental Classroom scopes +
Google↔Campus identity mapping.

Runs the full auth bridge against a local stub of Google's OAuth +
Classroom endpoints (no network, no real Google account needed):

1. /classroom/authorize redirects to Google with the MVP scope set,
   access_type=offline, include_granted_scopes=true, prompt=consent,
   login_hint = Campus email, hd = workspace domain,
   redirect_uri = {PUBLIC_URL}/classroom/callback.
2. Happy-path callback exchanges the code, enforces the email match,
   stores the credential, and /classroom then performs an authenticated
   courses.list() through with_classroom_session().
3. State (CSRF) mismatch is refused loudly (400), nothing stored.
4. Google email ≠ Campus email is refused loudly (403), nothing stored.
5. user-cancelled consent (error=access_denied) lands on a clean flash.
6. Missing-scope state: /classroom shows a clean reconnect prompt, the
   with_classroom_session() gate raises with the exact missing list, the
   incremental re-authorize URL requests only the missing scopes, and an
   API-style route gets 403 JSON with an authorize_url — never a 500.
7. Expired access tokens are refreshed against the token endpoint and the
   refreshed credential is persisted back into the session; a 401 from the
   API triggers one refresh-and-retry.
8. GOOGLE_CLIENT_ID/SECRET unset produces clean config errors, not 500s.

Usage: .venv/Scripts/python.exe scripts/verify_issue_9.py
"""

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-9-secret"
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-issue-9-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-issue-9-client-secret")
os.environ["WORKSPACE_DOMAIN"] = "nyjc.edu.sg"

import flask  # noqa: E402

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402

CAMPUS_EMAIL = "teacher@nyjc.edu.sg"
OTHER_EMAIL = "someone@gmail.com"

SCOPE_BASE = "https://www.googleapis.com/auth/"
ALL_MVP = list(cauth.CLASSROOM_SCOPES_MVP)
IDENTITY = list(cauth.GOOGLE_IDENTITY_SCOPES)
SUBSET_SCOPES = [s for s in ALL_MVP if "rosters" not in s and
                 "student-submissions.students" not in s]
MISSING = cauth.missing_scopes(ALL_MVP, SUBSET_SCOPES)

COURSES_PAYLOAD = {
    "courses": [
        {"id": "course_111", "name": "CS1101s", "section": "25S1-01"},
        {"id": "course_222", "name": "Computing Plus", "section": "25S1-02"},
    ]
}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Stub Google OAuth + Classroom server
# ---------------------------------------------------------------------------
class StubGoogle(BaseHTTPRequestHandler):
    """Minimal Google endpoints with scriptable token/email/401 behaviour."""

    codes: dict = {}          # code -> {"scopes": [...], "email": str}
    refresh_scopes: dict = {}  # refresh_token -> [...]
    token_emails: dict = {}    # access_token -> str
    stale_tokens: set = set()  # access tokens that answer 401
    refresh_calls: list = []

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        import json
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self):
        header = self.headers.get("Authorization", "")
        return header.removeprefix("Bearer ").strip()

    def do_POST(self):
        if urlsplit(self.path).path != "/token":
            self._send({"error": "not_found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        grant_type = form.get("grant_type", [""])[0]
        if grant_type == "authorization_code":
            code = form.get("code", [""])[0]
            spec = self.codes.get(code)
            if not spec:
                self._send({"error": "invalid_grant"}, 400)
                return
            access = f"access-for-{code}"
            refresh = f"refresh-for-{code}"
            self.token_emails[access] = spec["email"]
            self.refresh_scopes[refresh] = spec["scopes"]
            self._send({
                "access_token": access,
                "refresh_token": refresh,
                "expires_in": 3600,
                "scope": " ".join(spec["scopes"]),
                "token_type": "Bearer",
            })
        elif grant_type == "refresh_token":
            refresh = form.get("refresh_token", [""])[0]
            scopes = self.refresh_scopes.get(refresh)
            if not scopes:
                self._send({"error": "invalid_grant"}, 400)
                return
            self.refresh_calls.append(refresh)
            access = f"access-refreshed-{len(self.refresh_calls)}-{refresh}"
            self.token_emails[access] = None  # email not re-checked on refresh
            self._send({
                "access_token": access,
                "expires_in": 3600,
                "scope": " ".join(scopes),
                "token_type": "Bearer",
            })
        else:
            self._send({"error": "unsupported_grant_type"}, 400)

    def do_GET(self):
        path = urlsplit(self.path).path
        token = self._bearer()
        if path == "/userinfo":
            email = self.token_emails.get(token)
            if email is None or token in self.stale_tokens:
                self._send({"error": "invalid_token"}, 401)
                return
            self._send({
                "sub": "google-sub-123",
                "email": email,
                "email_verified": True,
            })
        elif path == "/v1/courses":
            if token in self.stale_tokens:
                self._send({"error": {"code": 401, "message": "Invalid Credentials"}}, 401)
                return
            if token not in self.token_emails:
                self._send({"error": {"code": 403, "message": "Permission denied"}}, 403)
                return
            self._send(COURSES_PAYLOAD)
        else:
            self._send({"error": "not_found"}, 404)


server = ThreadingHTTPServer(("127.0.0.1", 0), StubGoogle)
STUB_PORT = server.server_address[1]
STUB = f"http://127.0.0.1:{STUB_PORT}"
threading.Thread(target=server.serve_forever, daemon=True).start()

# Point the auth bridge at the stub (module-level constants, monkeypatched).
cauth.GOOGLE_AUTH_URL = f"{STUB}/auth"
cauth.GOOGLE_TOKEN_URL = f"{STUB}/token"
cauth.GOOGLE_USERINFO_URL = f"{STUB}/userinfo"
cauth.CLASSROOM_API_BASE = STUB


# ---------------------------------------------------------------------------
# App with a faked Campus login (push_context reads verify_user from session)
# ---------------------------------------------------------------------------
def _fake_push_context(self):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


campus_auth_v1.AuthRoot.push_context = _fake_push_context

app = create_app()


@app.get("/api/_probe_classroom")
def _probe_classroom(**_):
    """Stands in for the API/iframe routes future sessions will write."""
    with cauth.with_classroom_session() as classroom:
        return {"data": classroom.courses_list()}


def seed_user(client):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": CAMPUS_EMAIL, "email": CAMPUS_EMAIL}


def seed_credentials(client, scopes, *, expires_at=None, access="seeded-access",
                    refresh="seeded-refresh"):
    with client.session_transaction() as sess:
        sess["classroom_credentials"] = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": expires_at if expires_at is not None else time.time() + 3600,
            "scopes": list(scopes),
            "email": CAMPUS_EMAIL,
        }
        if access:
            StubGoogle.token_emails[access] = CAMPUS_EMAIL
        if refresh:
            StubGoogle.refresh_scopes[refresh] = list(scopes)


def stored_creds(client):
    with client.session_transaction() as sess:
        return sess.get("classroom_credentials")


def authorize_location(client, query=""):
    resp = client.get(f"/classroom/authorize{query}")
    assert resp.status_code == 302, resp.status_code
    return resp.headers["Location"]


def auth_params(location):
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


client = app.test_client()

# ---------------------------------------------------------------------------
# 1. /classroom/authorize redirect composition
# ---------------------------------------------------------------------------
seed_user(client)
loc = authorize_location(client)
check("authorize redirects to Google auth endpoint",
      loc.startswith(f"{STUB}/auth?"), loc)
params = auth_params(loc)
check("authorize requests full MVP scope set",
      set(params.get("scope", "").split()) >= set(ALL_MVP),
      params.get("scope", ""))
check("authorize always includes identity scopes",
      set(IDENTITY) <= set(params.get("scope", "").split()))
check("authorize uses access_type=offline", params.get("access_type") == "offline")
check("authorize uses include_granted_scopes=true (incremental)",
      params.get("include_granted_scopes") == "true")
check("authorize uses prompt=consent (refresh token guaranteed)",
      params.get("prompt") == "consent")
check("authorize sets login_hint to Campus email",
      params.get("login_hint") == CAMPUS_EMAIL)
check("authorize pins workspace domain via hd",
      params.get("hd") == "nyjc.edu.sg")
check("authorize callback is PUBLIC_URL/classroom/callback",
      params.get("redirect_uri") == "http://localhost:5000/classroom/callback",
      params.get("redirect_uri", ""))
check("authorize carries CSRF state", bool(params.get("state")))

with client.session_transaction() as sess:
    check("authorize stores state + next destination in session",
          sess.get("classroom_oauth_state") == params["state"]
          and sess.get("classroom_auth_next", "").endswith("/classroom/"))

# Unknown scopes in ?scopes= fall back to the full set (fresh client: this
# rotates the session's OAuth state, which the happy path below needs intact)
client_unknown = app.test_client()
seed_user(client_unknown)
loc = authorize_location(client_unknown, f"?{urlencode({'scopes': 'not-a-scope'})}")
check("authorize ignores unknown ?scopes= tokens",
      set(ALL_MVP) <= set(auth_params(loc).get("scope", "").split()))

# ---------------------------------------------------------------------------
# 2. Happy path: callback -> stored credential -> authenticated courses.list()
# ---------------------------------------------------------------------------
StubGoogle.codes["code-full"] = {"scopes": IDENTITY + ALL_MVP, "email": CAMPUS_EMAIL}
resp = client.get(
    f"/classroom/callback?code=code-full&state={params['state']}"
)
check("happy callback redirects to the connection page",
      resp.status_code == 302 and resp.headers["Location"].endswith("/classroom/"),
      f"{resp.status_code} {resp.headers.get('Location')}")

creds = stored_creds(client)
check("callback stores credential in session", creds is not None)
if creds:
    check("stored credential email = Campus email", creds["email"] == CAMPUS_EMAIL)
    check("stored credential carries MVP scopes",
          set(ALL_MVP) <= set(creds["scopes"]), str(creds["scopes"]))
    check("stored credential keeps the refresh token", bool(creds["refresh_token"]))
    check("stored credential expiry is in the future",
          creds["expires_at"] > time.time())

resp = client.get("/classroom/")
check("connection page renders (200)", resp.status_code == 200)
html = resp.get_data(as_text=True)
check("connection page shows connected state", "Connected" in html)
check("connection page shows the courses.list() result (acceptance demo)",
      "CS1101s" in html and "Computing Plus" in html)

# Direct courses_list through the context manager, in a request context
with app.test_request_context("/"):
    flask.session["verify_user"] = {"id": CAMPUS_EMAIL, "email": CAMPUS_EMAIL}
    flask.g.user = SimpleNamespace(id=CAMPUS_EMAIL, email=CAMPUS_EMAIL)
    flask.session["classroom_credentials"] = stored_creds(client)
    with cauth.with_classroom_session() as classroom:
        courses = classroom.courses_list(teacher_only=True)
    check("with_classroom_session yields a working client (courses.list)",
          [c["name"] for c in courses] == ["CS1101s", "Computing Plus"])

# ---------------------------------------------------------------------------
# 3. CSRF: state mismatch refused loudly, nothing stored
# ---------------------------------------------------------------------------
client_csrf = app.test_client()
seed_user(client_csrf)
loc = authorize_location(client_csrf)
resp = client_csrf.get("/classroom/callback?code=code-full&state=evil-state")
check("state mismatch returns 400 (not 500)", resp.status_code == 400)
check("state mismatch refusal page is loud", b"could not be verified" in resp.data)
check("state mismatch stores nothing", stored_creds(client_csrf) is None)

# ---------------------------------------------------------------------------
# 4. Identity mapping: Google email must equal Campus email
# ---------------------------------------------------------------------------
client_mismatch = app.test_client()
seed_user(client_mismatch)
StubGoogle.codes["code-mismatch"] = {
    "scopes": IDENTITY + ALL_MVP, "email": OTHER_EMAIL}
loc = authorize_location(client_mismatch)
resp = client_mismatch.get(
    f"/classroom/callback?code=code-mismatch&state={auth_params(loc)['state']}"
)
check("identity mismatch refused with 403", resp.status_code == 403)
check("identity mismatch names both accounts",
      CAMPUS_EMAIL.encode() in resp.data and OTHER_EMAIL.encode() in resp.data)
check("identity mismatch stores no credential", stored_creds(client_mismatch) is None)

# ---------------------------------------------------------------------------
# 5. User cancels consent -> clean flash, no credential
# ---------------------------------------------------------------------------
client_cancel = app.test_client()
resp = client_cancel.get("/classroom/callback?error=access_denied&state=x")
check("cancelled consent redirects cleanly",
      resp.status_code == 302 and resp.headers["Location"].endswith("/classroom/"))
check("cancelled consent stores nothing", stored_creds(client_cancel) is None)

# ---------------------------------------------------------------------------
# 6. Missing-scope state: clean errors + incremental re-consent
# ---------------------------------------------------------------------------
seed_user(client)
seed_credentials(client, SUBSET_SCOPES)
resp = client.get("/classroom/")
check("missing-scope connection page still renders (no 500)", resp.status_code == 200)
html = resp.get_data(as_text=True)
check("missing-scope page lists what to grant",
      all(s in html for s in MISSING))
check("missing-scope page offers incremental reconnect",
      "Grant missing permissions" in html)

with app.test_request_context("/"):
    flask.session["classroom_credentials"] = {
        "access_token": "a", "refresh_token": "r",
        "expires_at": time.time() + 3600, "scopes": SUBSET_SCOPES,
        "email": CAMPUS_EMAIL,
    }
    try:
        with cauth.with_classroom_session():
            pass
        check("scope gate raises on missing scopes", False, "no error raised")
    except cauth.MissingClassroomScopesError as err:
        check("scope gate raises on missing scopes", True)
        check("missing scopes reported exactly",
              set(err.missing) == set(MISSING), str(err.missing))

loc = authorize_location(client, f"?{urlencode({'scopes': ' '.join(MISSING)})}")
inc_params = auth_params(loc)
check("incremental authorize requests only the missing scopes",
      set(inc_params.get("scope", "").split()) == set(IDENTITY) | set(MISSING),
      inc_params.get("scope", ""))
check("incremental authorize still merges granted scopes",
      inc_params.get("include_granted_scopes") == "true")

# API-style route gets structured JSON, never a 500
seed_credentials(client, SUBSET_SCOPES)
resp = client.get("/api/_probe_classroom")
check("API route with missing scopes returns 403 JSON", resp.status_code == 403)
body = resp.get_json()
check("API error carries code + missing scopes + authorize_url",
      body["error"]["code"] == "classroom_missing_scopes"
      and set(body["error"]["missing_scopes"]) == set(MISSING)
      and "/classroom/authorize" in body["error"]["authorize_url"])

resp = client.get("/api/_probe_classroom", headers={"Accept": "application/json"})
check("JSON-negotiated requests get JSON errors too", resp.status_code == 403)

# Not connected at all
client2 = app.test_client()
seed_user(client2)
resp = client2.get("/classroom/")
check("not-connected page renders with connect CTA (no 500)",
      resp.status_code == 200 and b"Connect Google Classroom" in resp.data)
resp = client2.get("/api/_probe_classroom")
check("not-connected API route returns 403 JSON",
      resp.status_code == 403
      and resp.get_json()["error"]["code"] == "classroom_not_connected")

# ---------------------------------------------------------------------------
# 7. Token refresh: proactive (expired) and reactive (401 retry)
# ---------------------------------------------------------------------------
seed_user(client)
seed_credentials(
    client, ALL_MVP,
    expires_at=time.time() - 100,
    access="expired-access", refresh="refresh-valid",
)
resp = client.get("/classroom/")
check("expired token is refreshed and courses.list succeeds",
      resp.status_code == 200 and b"CS1101s" in resp.data)
check("refresh endpoint was hit", "refresh-valid" in StubGoogle.refresh_calls)
refreshed = stored_creds(client)
check("refreshed credential persisted to session",
      refreshed["access_token"] != "expired-access"
      and refreshed["expires_at"] > time.time())

StubGoogle.stale_tokens.add("revoked-server-side")
seed_credentials(
    client, ALL_MVP,
    expires_at=time.time() + 3600,
    access="revoked-server-side", refresh="refresh-401-retry",
)
resp = client.get("/classroom/")
check("401 triggers one refresh-and-retry (courses.list still succeeds)",
      resp.status_code == 200 and b"CS1101s" in resp.data)
check("post-retry credential persisted",
      stored_creds(client)["access_token"] != "revoked-server-side")
StubGoogle.stale_tokens.clear()

# ---------------------------------------------------------------------------
# 8. Unconfigured Google credentials -> clean errors, not 500s
# ---------------------------------------------------------------------------
saved = (os.environ.pop("GOOGLE_CLIENT_ID", None),
         os.environ.pop("GOOGLE_CLIENT_SECRET", None))
try:
    resp = client.get("/classroom/authorize")
    check("authorize without GOOGLE_* config redirects with a flash",
          resp.status_code == 302
          and resp.headers["Location"].endswith("/classroom/"))
    resp = client.get("/classroom/", follow_redirects=True)
    check("connection page shows config guidance (no 500)",
          resp.status_code == 200 and b"GOOGLE_CLIENT_ID" in resp.data)
finally:
    if saved[0]:
        os.environ["GOOGLE_CLIENT_ID"] = saved[0]
    if saved[1]:
        os.environ["GOOGLE_CLIENT_SECRET"] = saved[1]

# ---------------------------------------------------------------------------
# 9. Disconnect
# ---------------------------------------------------------------------------
resp = client.post("/classroom/disconnect")
check("disconnect clears the stored credential",
      resp.status_code == 302 and stored_creds(client) is None)

server.shutdown()
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
