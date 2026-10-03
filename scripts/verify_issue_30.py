"""Verification for issue #30: with_classroom_session() -> campus.auth token
broker (Phase 1 switch, tracker campus#733).

Runs the swapped seam against a local stub of the campus.auth broker and the
Classroom API, with a fake campus SDK auth object (no network, no real
campus/Google account needed):

1.  Happy path: the user's campus bearer + min_scopes=MVP reach the broker,
    the released token calls courses.list(), and NOTHING is persisted to the
    Flask session (no refresh token ever arrives).
2.  Campus-token refresh: an expired campus credential is refreshed via the
    SDK surface (auth.token + credentials update) and the REFRESHED bearer is
    the one the broker sees.
3.  Proactive re-release: a token released with a near-expiry expires_in is
    re-fetched from the broker before the next Classroom call.
4.  Reactive: a 401 from Classroom triggers exactly one broker re-release
    and retry.
5.  Broker 404 -> NotConnectedError; API path 403 JSON whose message points
    at the profile integrations page. Legacy session-stored credentials are
    ignored (the seam no longer reads the Flask session).
6.  Broker 403 + missing_scopes -> MissingClassroomScopesError with the
    exact list, 403 JSON with the profile-page reconnect_url.
7.  Broker 401 -> CampusSessionExpiredError: 401 JSON on API paths, redirect
    to /login on browser paths.
8.  Broker 400 AUTH_INVALID_SCOPE -> BrokerConfigError (502 JSON, loud ERROR
    log), never a 500 or a swallowed failure.
9.  Send scopes (MVP + coursework.students): the broker is asked ONLY for
    the askable MVP subset; the beyond-cap scope fails the local gate with
    MissingClassroomScopesError naming it.
10. required_scopes=() skips the gate and sends no min_scopes.
11. _campus_bearer(): fresh token used as-is; expired -> refresh dance; no
    login session -> CampusSessionExpiredError.
12. /classroom page: Connected state (email + scopes + courses) from the
    broker, Not-connected state with the profile-page CTA, and a clean
    error state when the broker is unreachable (no 500, no redirect loop).

Usage: .venv/Scripts/python.exe scripts/verify_issue_30.py
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-30-secret"
os.environ["CAMPUS_PROFILE_URL"] = "https://profile.example"
os.environ["CLIENT_ID"] = "verify-issue-30-campus-client"
os.environ["CLIENT_SECRET"] = "verify-issue-30-campus-secret"

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
import flask  # noqa: E402
import requests  # noqa: E402
import requests.adapters  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402

# One pooled, retrying session for every HTTP call the seam makes: fresh
# loopback connections intermittently abort on this dev box (WinError
# 10053), and keep-alive both dodges that and is far faster. The stub
# handler below runs HTTP/1.1 to match.
_shared_http = requests.Session()
_shared_http.mount("http://", requests.adapters.HTTPAdapter(
    max_retries=Retry(total=3, backoff_factor=0.05), pool_maxsize=20))
_shared_http.mount("https://", requests.adapters.HTTPAdapter(
    max_retries=Retry(total=3, backoff_factor=0.05), pool_maxsize=20))


class _PooledRequests:
    """Drop-in for the requests-module surface classroom_auth uses."""

    def get(self, *args, **kwargs):
        return _shared_http.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        return _shared_http.post(*args, **kwargs)

    def request(self, *args, **kwargs):
        return _shared_http.request(*args, **kwargs)

    def __getattr__(self, name):
        # module attributes the seam reads (e.g. RequestException)
        return getattr(requests, name)


cauth.requests = _PooledRequests()

CAMPUS_EMAIL = "teacher@nyjc.edu.sg"

SCOPE_BASE = "https://www.googleapis.com/auth/"
ALL_MVP = list(cauth.CLASSROOM_SCOPES_MVP)
SEND_SCOPES = list(cauth.CLASSROOM_SCOPES_MVP) + list(cauth.CLASSROOM_SCOPES_SEND)
MVP_SCOPE_STRING = " ".join(ALL_MVP)

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
# Stub campus.auth broker + Classroom API server
# ---------------------------------------------------------------------------
class StubCampus(BaseHTTPRequestHandler):

    """campus.auth broker + Classroom API with scriptable behaviour."""
    protocol_version = "HTTP/1.1"

    # mode: ok | not_found | missing_scopes | unauthorized |
    #       invalid_scope | server_error
    mode = "ok"
    expires_in = 3600
    counter = 0               # access tokens handed out: gat-1, gat-2, ...
    bearers: list = []        # Authorization bearers seen at the broker
    bodies: list = []         # JSON bodies seen at the broker
    stale_tokens: set = set()  # Classroom answers 401 for these
    broker_calls = 0

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def reset(cls, mode="ok", **kwargs):
        cls.mode = mode
        cls.expires_in = 3600
        cls.counter = 0
        cls.bearers = []
        cls.bodies = []
        cls.stale_tokens = set()
        cls.broker_calls = 0
        for key, value in kwargs.items():
            setattr(cls, key, value)

    def do_POST(self):
        if urlsplit(self.path).path != cauth.BROKER_PATH:
            self._send({"error": {"code": "NOT_FOUND", "message": "no route"}}, 404)
            return
        type(self).broker_calls += 1
        type(self).bearers.append(
            self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        type(self).bodies.append(json.loads(raw) if raw else {})
        mode = type(self).mode
        if mode == "ok":
            type(self).counter += 1
            access = f"gat-{type(self).counter}"
            self._send({
                "provider": "google.classroom",
                "user_id": CAMPUS_EMAIL,
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": type(self).expires_in,
                "scope": MVP_SCOPE_STRING,
            })
        elif mode == "not_found":
            self._send({
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"No google.classroom credential for user "
                               f"{CAMPUS_EMAIL}; complete the Classroom connect "
                               "flow first (via the app's integrations page)",
                    "details": {},
                    "request_id": None,
                }
            }, 404)
        elif mode == "missing_scopes":
            self._send({
                "error": {
                    "code": "FORBIDDEN",
                    "message": "The user's google.classroom grant does not "
                               "cover the requested scopes",
                    "details": {
                        "missing_scopes": [f"{SCOPE_BASE}classroom.rosters.readonly"],
                        "provider": "google.classroom",
                    },
                    "request_id": None,
                }
            }, 403)
        elif mode == "unauthorized":
            self._send({
                "error": {"code": "UNAUTHORIZED", "message": "token invalid",
                          "details": {}, "request_id": None}
            }, 401)
        elif mode == "invalid_scope":
            self._send({
                "error": {
                    "code": "AUTH_INVALID_SCOPE",
                    "message": "Requested scopes exceed the google.classroom "
                               "integration's configured scope cap",
                    "details": {
                        "provider": "google.classroom",
                        "disallowed_scopes": [
                            f"{SCOPE_BASE}classroom.coursework.students"],
                    },
                    "request_id": None,
                }
            }, 400)
        else:
            self._send({
                "error": {"code": "INTERNAL_ERROR", "message": "boom",
                          "details": {}, "request_id": None}
            }, 500)

    def do_GET(self):
        if urlsplit(self.path).path != "/v1/courses":
            self._send({"error": "not_found"}, 404)
            return
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token in type(self).stale_tokens:
            self._send({"error": {"code": 401, "message": "Invalid Credentials"}}, 401)
        elif type(self).mode == "ok":
            self._send(COURSES_PAYLOAD)
        else:
            self._send({"error": {"code": 403, "message": "Permission denied"}}, 403)


server = ThreadingHTTPServer(("127.0.0.1", 0), StubCampus)
STUB = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()

# Point the bridge at the stub (Classroom API base; the broker URL comes
# from the fake campus client's auth.client.base_url below).
cauth.CLASSROOM_API_BASE = STUB


# ---------------------------------------------------------------------------
# Fake campus SDK auth object (what _campus_bearer drives)
# ---------------------------------------------------------------------------
class _CredUser:
    def __init__(self, log: list):
        self._log = log

    def update(self, token=None):
        self._log.append(token)


class _CredProvider:
    def __init__(self, log: list):
        self._log = log

    def __getitem__(self, user_id):
        return _CredUser(self._log)


class _CredRoot:
    def __init__(self):
        self._log: list = []

    def __getitem__(self, provider):
        return _CredProvider(self._log)


class FakeCampusAuth:
    """Scriptable stand-in for campus_python AuthRoot (token custodian)."""

    def __init__(self):
        self.client = SimpleNamespace(base_url=STUB)
        self.has_login = True
        self.campus_token = SimpleNamespace(
            id="cat-1",
            access_token="cat-1",
            refresh_token="crt-1",
            is_expired=lambda: False,
        )
        self.token_calls: list = []
        self.cred_updates: list = []
        self.logins = self
        self.credentials = _CredRoot()
        self.credentials._log = self.cred_updates

    def get_token(self):
        if not self.has_login:
            raise campus_errors.AuthenticationError(
                error_description="No login session found. User must log in first."
            )
        return self.campus_token

    def from_session(self):
        return SimpleNamespace(user_id=CAMPUS_EMAIL)

    def token(self, grant_type, *, refresh_token=None):
        self.token_calls.append((grant_type, refresh_token))
        n = len(self.token_calls)
        self.campus_token = SimpleNamespace(
            id=f"cat-{n + 1}",
            access_token=f"cat-{n + 1}",
            refresh_token=f"crt-{n + 1}",
            is_expired=lambda: False,
        )
        return self.campus_token


class FakeCampus:
    def __init__(self):
        self.auth = FakeCampusAuth()


# App with a faked Campus login (push_context reads verify_user from session)
def _fake_push_context(self):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


campus_auth_v1.AuthRoot.push_context = _fake_push_context

app = create_app()
fake_campus = FakeCampus()
app.campus = fake_campus  # the seam reads flask.current_app.campus

log_records: list = []


class _Capture(logging.Handler):
    def emit(self, record):
        log_records.append(record)


app.logger.addHandler(_Capture())


@app.get("/api/_probe_classroom")
def _probe_classroom(**_):
    with cauth.with_classroom_session() as classroom:
        return {"data": classroom.courses_list()}


@app.get("/api/_probe_send")
def _probe_send(**_):
    with cauth.with_classroom_session(required_scopes=SEND_SCOPES) as classroom:
        return {"data": classroom.courses_list()}


@app.get("/api/_probe_nogate")
def _probe_nogate(**_):
    with cauth.with_classroom_session(required_scopes=()) as classroom:
        return {"data": classroom.courses_list()}


def seed_user(client):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": CAMPUS_EMAIL, "email": CAMPUS_EMAIL}


def reset_fake(token: str = "cat-1", *, expired: bool = False):
    """Fresh campus-credential state on the fake SDK auth object."""
    fake_campus.auth.campus_token = SimpleNamespace(
        id=token,
        access_token=token,
        refresh_token=f"crt-{token.split('-')[-1]}",
        is_expired=lambda: expired,
    )
    fake_campus.auth.token_calls.clear()
    fake_campus.auth.cred_updates.clear()
    fake_campus.auth.has_login = True


client = app.test_client()
seed_user(client)

# ---------------------------------------------------------------------------
# 1. Happy path: bearer + min_scopes reach the broker; nothing persisted
# ---------------------------------------------------------------------------
StubCampus.reset("ok")
resp = client.get("/api/_probe_classroom")
check("happy path returns courses via the broker-released token",
      resp.status_code == 200 and resp.get_json()["data"][0]["name"] == "CS1101s",
      resp.get_data(as_text=True)[:200])
check("broker saw exactly one call", StubCampus.broker_calls == 1)
check("broker saw the user's campus bearer",
      StubCampus.bearers == ["cat-1"], str(StubCampus.bearers))
check("broker was asked for exactly the MVP min_scopes",
      StubCampus.bodies and StubCampus.bodies[0].get("min_scopes") == ALL_MVP,
      str(StubCampus.bodies))
with client.session_transaction() as sess:
    check("nothing persisted to the Flask session",
          "classroom_credentials" not in sess, str(list(sess.keys())))

# ---------------------------------------------------------------------------
# 2. Expired campus credential -> refreshed bearer at the broker
# ---------------------------------------------------------------------------
StubCampus.reset("ok")
reset_fake(expired=True)
resp = client.get("/api/_probe_classroom")
check("refreshed campus token is what the broker sees",
      resp.status_code == 200 and StubCampus.bearers == ["cat-2"],
      str(StubCampus.bearers))
check("refresh used the SDK token endpoint with the stored refresh token",
      fake_campus.auth.token_calls == [("refresh_token", "crt-1")],
      str(fake_campus.auth.token_calls))
check("refreshed credential written back via credentials.update",
      len(fake_campus.auth.cred_updates) == 1
      and fake_campus.auth.cred_updates[0].access_token == "cat-2")

# ---------------------------------------------------------------------------
# 3. Near-expiry release -> proactive broker re-release before Classroom
# ---------------------------------------------------------------------------
StubCampus.reset("ok", expires_in=10)  # within the 60s skew: stale at once
reset_fake()
resp = client.get("/api/_probe_classroom")
check("near-expiry token re-released from the broker in-request",
      resp.status_code == 200 and StubCampus.broker_calls == 2,
      f"calls={StubCampus.broker_calls}")

# ---------------------------------------------------------------------------
# 4. Classroom 401 -> one broker re-release and retry
# ---------------------------------------------------------------------------
StubCampus.reset("ok")
StubCampus.stale_tokens = {"gat-1"}
reset_fake()
resp = client.get("/api/_probe_classroom")
check("Classroom 401 triggers one re-release and retry",
      resp.status_code == 200 and StubCampus.broker_calls == 2,
      f"calls={StubCampus.broker_calls}")

# ---------------------------------------------------------------------------
# 5. Broker 404 -> NotConnectedError (+ legacy session creds ignored)
# ---------------------------------------------------------------------------
StubCampus.reset("not_found")
reset_fake()
with client.session_transaction() as sess:
    sess["classroom_credentials"] = {
        "access_token": "legacy-session-token",
        "refresh_token": "legacy-refresh",
        "expires_at": 9999999999,
        "scopes": ALL_MVP,
        "email": CAMPUS_EMAIL,
    }
resp = client.get("/api/_probe_classroom")
body = resp.get_json()
check("broker 404 -> 403 JSON classroom_not_connected",
      resp.status_code == 403 and body["error"]["code"] == "classroom_not_connected",
      resp.get_data(as_text=True)[:200])
check("not-connected message points at the profile integrations page",
      "https://profile.example/profile/integrations" in body["error"]["message"],
      body["error"]["message"])
check("legacy session-stored credential is ignored",
      StubCampus.broker_calls == 1 and StubCampus.bearers == ["cat-1"])
with client.session_transaction() as sess:
    del sess["classroom_credentials"]

# ---------------------------------------------------------------------------
# 6. Broker 403 + missing_scopes -> MissingClassroomScopesError
# ---------------------------------------------------------------------------
StubCampus.reset("missing_scopes")
reset_fake()
resp = client.get("/api/_probe_classroom")
body = resp.get_json()
check("broker 403 missing_scopes -> 403 JSON classroom_missing_scopes",
      resp.status_code == 403 and body["error"]["code"] == "classroom_missing_scopes",
      resp.get_data(as_text=True)[:200])
check("missing scopes carried exactly",
      body["error"]["missing_scopes"] == [f"{SCOPE_BASE}classroom.rosters.readonly"],
      str(body["error"].get("missing_scopes")))
check("JSON error carries the profile-page reconnect_url",
      body["error"].get("reconnect_url")
      == "https://profile.example/profile/integrations",
      str(body["error"].get("reconnect_url")))

# ---------------------------------------------------------------------------
# 7. Broker 401 -> CampusSessionExpiredError (JSON 401 / browser re-login)
# ---------------------------------------------------------------------------
StubCampus.reset("unauthorized")
reset_fake()
resp = client.get("/api/_probe_classroom")
check("broker 401 -> 401 JSON campus_session_expired",
      resp.status_code == 401
      and resp.get_json()["error"]["code"] == "campus_session_expired",
      resp.get_data(as_text=True)[:200])
resp = client.get("/classroom/")
check("broker 401 on browser path redirects to campus re-login",
      resp.status_code == 302 and "/login" in resp.headers["Location"]
      and "next=" in resp.headers["Location"],
      resp.headers.get("Location", ""))

# ---------------------------------------------------------------------------
# 8. Broker 400 AUTH_INVALID_SCOPE -> BrokerConfigError, loud, not 500
# ---------------------------------------------------------------------------
StubCampus.reset("invalid_scope")
reset_fake()
log_records.clear()
resp = client.get("/api/_probe_classroom")
body = resp.get_json()
check("broker 400 -> 502 JSON classroom_broker_config",
      resp.status_code == 502 and body["error"]["code"] == "classroom_broker_config",
      f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")
check("rejection logged loudly at ERROR",
      any(r.levelno == logging.ERROR and "broker" in r.getMessage()
          for r in log_records), str([r.getMessage() for r in log_records]))
resp = client.get("/classroom/", follow_redirects=True)
check("broker 400 on browser path renders a flash, not a 500 or loop",
      resp.status_code == 200
      and "deployment configuration problem".encode() in resp.data)

# ---------------------------------------------------------------------------
# 9. Send scopes: broker asked for the askable subset; cap-scope gated locally
# ---------------------------------------------------------------------------
StubCampus.reset("ok")
reset_fake()
resp = client.get("/api/_probe_send")
body = resp.get_json()
check("send probe: broker asked ONLY for the MVP subset (conservative ask)",
      StubCampus.bodies and StubCampus.bodies[0].get("min_scopes") == ALL_MVP,
      str(StubCampus.bodies))
check("send probe: unasked feature scope fails the local gate",
      resp.status_code == 403
      and body["error"]["code"] == "classroom_missing_scopes"
      and body["error"]["missing_scopes"] == list(cauth.CLASSROOM_SCOPES_SEND),
      resp.get_data(as_text=True)[:300])

# ---------------------------------------------------------------------------
# 10. required_scopes=() -> no gate, no min_scopes
# ---------------------------------------------------------------------------
StubCampus.reset("ok")
reset_fake()
resp = client.get("/api/_probe_nogate")
check("required_scopes=() skips the gate and the min_scopes ask",
      resp.status_code == 200 and StubCampus.bodies[0] == {},
      f"{resp.status_code} {StubCampus.bodies}")

# ---------------------------------------------------------------------------
# 11. _campus_bearer() directly
# ---------------------------------------------------------------------------
with app.test_request_context("/"):
    flask.session["verify_user"] = {"id": CAMPUS_EMAIL, "email": CAMPUS_EMAIL}
    reset_fake("cat-x")
    check("fresh campus token used as-is",
          cauth._campus_bearer() == "cat-x"
          and not fake_campus.auth.token_calls)
    fake_campus.auth.campus_token.is_expired = lambda: True
    check("expired campus token refreshed before use",
          cauth._campus_bearer() == "cat-2"
          and fake_campus.auth.token_calls == [("refresh_token", "crt-x")],
          f"token={cauth._campus_bearer()} calls={fake_campus.auth.token_calls}")
    fake_campus.auth.has_login = False
    try:
        cauth._campus_bearer()
        check("no campus login session -> CampusSessionExpiredError", False)
    except cauth.CampusSessionExpiredError:
        check("no campus login session -> CampusSessionExpiredError", True)

# ---------------------------------------------------------------------------
# 12. /classroom page states
# ---------------------------------------------------------------------------
StubCampus.reset("ok")
reset_fake()
resp = client.get("/classroom/")
html = resp.get_data(as_text=True)
check("page renders Connected from the broker",
      resp.status_code == 200 and "Connected" in html and CAMPUS_EMAIL in html)
check("page lists granted scopes and live courses",
      all(s in html for s in ("classroom.rosters.readonly", "CS1101s")))

StubCampus.reset("not_found")
resp = client.get("/classroom/")
html = resp.get_data(as_text=True)
check("page renders Not-connected with the profile CTA (no 500)",
      resp.status_code == 200 and "Not connected" in html
      and "https://profile.example/profile/integrations" in html)

# Broker unreachable -> clean error state, no 500, no redirect loop
saved_url = cauth._broker_url
cauth._broker_url = lambda: "http://127.0.0.1:9/unreachable"
try:
    resp = client.get("/classroom/")
    html = resp.get_data(as_text=True)
    check("unreachable broker renders the page with an error alert",
          resp.status_code == 200
          and "Could not check your Classroom connection" in html)
finally:
    cauth._broker_url = saved_url

server.shutdown()
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
