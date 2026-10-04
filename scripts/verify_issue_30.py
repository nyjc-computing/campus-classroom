"""Verification for issue #30: with_classroom_session() -> campus.auth token
broker (Phase 1 switch, tracker campus#733). Re-seamed in issue #34: PR #33
moved the release onto the client library (`auth.broker.token("google",
"classroom", ...)` inside `campus.with_user_session()`), so the harness now
fakes that narrower seam — an in-memory Campus stub whose with_user_session()
mirrors the library contract (flask-session-seeded logins, expired-credential
refresh, yielded client carrying auth.broker) — and keeps the loopback stub
server only for the Classroom API side. No network, no real campus/Google
account needed:

1.  Happy path: the user's campus bearer + min_scopes=MVP reach the broker,
    the released token calls courses.list(), and NOTHING is persisted to the
    Flask session (no refresh token ever arrives).
2.  Campus-token refresh: an expired stored campus credential is refreshed
    the way the library's with_user_session() does (auth.token refresh grant
    + credentials write-back) and the REFRESHED bearer is the one the broker
    sees.
3.  Proactive re-release: a token released with a near-expiry expires_in is
    re-fetched from the broker before the next Classroom call.
4.  Reactive: a 401 from Classroom triggers exactly one broker re-release
    and retry.
5.  _broker_error four-way translation: 404 -> NotConnectedError (403 JSON
    pointing at the profile integrations page; legacy session-stored
    credentials ignored), 403 + details.missing_scopes ->
    MissingClassroomScopesError (exact list + reconnect_url), 401 ->
    CampusSessionExpiredError (401 JSON on API paths; /login redirect on
    browser paths), anything else (400 AUTH_INVALID_SCOPE, 500) ->
    BrokerConfigError (502 JSON, loud ERROR log, never a swallowed failure).
6.  No campus login session (logins.from_session raises) ->
    CampusSessionExpiredError.
7.  OAuthFlowError for unusable releases (malformed response, 200 without
    an access_token) and an unreachable broker — clean page/JSON errors,
    never a 500.
8.  Send scopes (MVP + coursework.students): the broker is asked ONLY for
    the askable MVP subset; the beyond-cap scope fails the local gate with
    MissingClassroomScopesError naming it.
9.  required_scopes=() skips the gate and asks the broker for nothing.
10. /classroom page: Connected state (email + scopes + courses) from the
    broker, Not-connected state with the profile-page CTA, and clean error
    states when the broker is unreachable or rejects the release (no 500,
    no redirect loop).

Usage: .venv/Scripts/python.exe scripts/verify_issue_30.py
"""

import json
import logging
import os
import threading
from contextlib import contextmanager
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

# One pooled, retrying session for every HTTP call the Classroom side of
# the seam makes: fresh loopback connections intermittently abort on this
# dev box (WinError 10053), and keep-alive both dodges that and is far
# faster. The stub handler below runs HTTP/1.1 to match.
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
# Stub Google Classroom API server (the seam's only remaining network hop)
# ---------------------------------------------------------------------------
class StubClassroom(BaseHTTPRequestHandler):

    """Classroom REST courses.list with scriptable 401s (reactive retry)."""
    protocol_version = "HTTP/1.1"

    stale_tokens: set = set()  # released access tokens that get a 401

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path != "/v1/courses":
            self._send({"error": {"code": 404, "message": "Not Found"}}, 404)
            return
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token in type(self).stale_tokens:
            self._send({"error": {"code": 401, "message": "Invalid Credentials"}}, 401)
        else:
            self._send(COURSES_PAYLOAD)


server = ThreadingHTTPServer(("127.0.0.1", 0), StubClassroom)
STUB = f"http://127.0.0.1:{server.server_address[1]}"
threading.Thread(target=server.serve_forever, daemon=True).start()

# Point the bridge's Classroom API side at the stub (the campus side is the
# in-memory Campus stub below — no server, no broker URL to retarget).
cauth.CLASSROOM_API_BASE = STUB


# ---------------------------------------------------------------------------
# Campus stub: with_user_session() + auth.broker.token() (issue #34 re-seam)
# ---------------------------------------------------------------------------
class FakeBroker:
    """Scriptable stand-in for the library's auth.broker resource.

    token() mirrors the real contract: the release dict on success; on
    failure an APIError subclass shaped the way the client's
    raise_for_status raises them — 404 NotFoundError, 401
    AuthenticationError, 403 AccessDeniedError carrying
    details.missing_scopes, 400 BadRequestError, 500 ServerError.
    Records the campus bearer each release rode in on and the min_scopes
    ask (None when the caller took none).
    """

    # ok | not_found | missing_scopes | unauthorized | invalid_scope |
    # server_error | empty_release | malformed | unreachable
    mode = "ok"
    expires_in = 3600
    calls = 0                # token() invocations
    tokens_issued = 0        # successful releases: gat-1, gat-2, ...
    bearers: list = []       # campus bearer per release
    asks: list = []          # min_scopes per release (list, or None)

    def __init__(self, auth: "FakeCampusAuth"):
        self._auth = auth

    @classmethod
    def reset(cls, mode="ok", **kwargs):
        cls.mode = mode
        cls.expires_in = 3600
        cls.calls = 0
        cls.tokens_issued = 0
        cls.bearers = []
        cls.asks = []
        for key, value in kwargs.items():
            setattr(cls, key, value)

    def token(self, provider, integration, *, min_scopes=None):
        assert (provider, integration) == ("google", "classroom"), (
            provider, integration)
        cls = type(self)
        cls.calls += 1
        cls.bearers.append(self._auth.session_bearer)
        cls.asks.append(list(min_scopes) if min_scopes is not None else None)
        mode = cls.mode
        if mode == "unreachable":
            raise requests.ConnectionError("connection refused")
        if mode == "malformed":
            raise campus_errors.MalformedResponseError(
                error_description="Response is not valid JSON")
        if mode == "not_found":
            raise campus_errors.NotFoundError(
                404,
                f"No google.classroom credential for user {CAMPUS_EMAIL}; "
                "complete the Classroom connect flow first (via the "
                "campus-profile integrations page)",
                details={})
        if mode == "unauthorized":
            raise campus_errors.AuthenticationError(401, "token invalid")
        if mode == "missing_scopes":
            raise campus_errors.AccessDeniedError(
                403,
                "The user's google.classroom grant does not cover the "
                "requested scopes",
                details={
                    "missing_scopes": [f"{SCOPE_BASE}classroom.rosters.readonly"],
                    "provider": "google.classroom",
                })
        if mode == "invalid_scope":
            raise campus_errors.BadRequestError(
                400,
                "Requested scopes exceed the google.classroom integration's "
                "configured scope cap",
                details={
                    "provider": "google.classroom",
                    "disallowed_scopes": [
                        f"{SCOPE_BASE}classroom.coursework.students"],
                })
        if mode == "server_error":
            raise campus_errors.ServerError(500, "boom")
        if mode == "empty_release":
            return {"provider": "google.classroom", "user_id": CAMPUS_EMAIL}
        cls.tokens_issued += 1
        return {
            "provider": "google.classroom",
            "user_id": CAMPUS_EMAIL,
            "access_token": f"gat-{cls.tokens_issued}",
            "token_type": "Bearer",
            "expires_in": cls.expires_in,
            "scope": MVP_SCOPE_STRING,
        }


class FakeCampusAuth:
    """campus.auth stand-in: flask-session-seeded logins, a stored campus
    credential with the SDK's refresh dance, and the broker resource."""

    def __init__(self):
        self.broker = FakeBroker(self)
        self.session_bearer: "str | None" = None
        self.stored = self._mint("cat-1", expired=False)
        self.token_calls: list = []   # (grant_type, refresh_token) endpoint hits
        self.cred_updates: list = []  # tokens written back to the credential
        self.has_login = True

    @staticmethod
    def _mint(name: str, *, expired: bool):
        return SimpleNamespace(
            id=name,
            access_token=name,
            refresh_token=f"crt-{name.split('-', 1)[-1]}",
            is_expired=lambda: expired,
        )

    # auth.logins.from_session(): the user's campus login, seeded in the
    # flask session (the harness stores verify_user). No session -> 401,
    # which _broker_release translates to CampusSessionExpiredError.
    def from_session(self):
        user = flask.session.get("verify_user")
        if not user or not self.has_login:
            raise campus_errors.AuthenticationError(
                401, "No login session found. User must log in first.")
        return SimpleNamespace(user_id=user["id"])

    # auth.token(): the OAuth token endpoint (refresh grant).
    def token(self, grant_type, *, refresh_token=None):
        self.token_calls.append((grant_type, refresh_token))
        return self._mint(f"cat-{len(self.token_calls) + 1}", expired=False)


class FakeCampus:
    """flask.current_app.campus: with_user_session() per the library
    contract — resolve the campus bearer from the flask login session,
    refresh an expired stored credential via auth.token plus a credentials
    write-back, then yield a client whose auth.broker.token() releases the
    google.classroom token under that bearer."""

    def __init__(self):
        self.auth = FakeCampusAuth()

    @contextmanager
    def with_user_session(self):
        auth = self.auth
        auth.from_session()  # logins.from_session()
        if auth.stored.is_expired():
            token = auth.token(
                "refresh_token", refresh_token=auth.stored.refresh_token)
            auth.stored = token
            auth.cred_updates.append(token)  # credentials[...].update(token=)
        else:
            token = auth.stored
        auth.session_bearer = token.access_token
        try:
            yield self
        finally:
            auth.session_bearer = None


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
    """Fresh stored campus credential on the fake campus.auth object."""
    fake_campus.auth.stored = FakeCampusAuth._mint(token, expired=expired)
    fake_campus.auth.token_calls.clear()
    fake_campus.auth.cred_updates.clear()
    fake_campus.auth.has_login = True


client = app.test_client()
seed_user(client)

# ---------------------------------------------------------------------------
# 1. Happy path: bearer + min_scopes reach the broker; nothing persisted
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
resp = client.get("/api/_probe_classroom")
check("happy path returns courses via the broker-released token",
      resp.status_code == 200 and resp.get_json()["data"][0]["name"] == "CS1101s",
      resp.get_data(as_text=True)[:200])
check("broker saw exactly one release", FakeBroker.calls == 1)
check("release rode on the user's campus bearer",
      FakeBroker.bearers == ["cat-1"], str(FakeBroker.bearers))
check("broker was asked for exactly the MVP min_scopes",
      FakeBroker.asks == [ALL_MVP], str(FakeBroker.asks))
with client.session_transaction() as sess:
    check("nothing persisted to the Flask session",
          "classroom_credentials" not in sess, str(list(sess.keys())))

# ---------------------------------------------------------------------------
# 2. Expired stored credential -> refreshed bearer at the broker
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
reset_fake(expired=True)
resp = client.get("/api/_probe_classroom")
check("refreshed campus token is what the broker sees",
      resp.status_code == 200 and FakeBroker.bearers == ["cat-2"],
      str(FakeBroker.bearers))
check("refresh used the token endpoint with the stored refresh token",
      fake_campus.auth.token_calls == [("refresh_token", "crt-1")],
      str(fake_campus.auth.token_calls))
check("refreshed credential written back",
      len(fake_campus.auth.cred_updates) == 1
      and fake_campus.auth.cred_updates[0].access_token == "cat-2")

# ---------------------------------------------------------------------------
# 3. Near-expiry release -> proactive broker re-release before Classroom
# ---------------------------------------------------------------------------
FakeBroker.reset("ok", expires_in=10)  # within the 60s skew: stale at once
reset_fake()
resp = client.get("/api/_probe_classroom")
check("near-expiry token re-released from the broker in-request",
      resp.status_code == 200 and FakeBroker.calls == 2,
      f"calls={FakeBroker.calls}")

# ---------------------------------------------------------------------------
# 4. Classroom 401 -> one broker re-release and retry
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
StubClassroom.stale_tokens = {"gat-1"}
reset_fake()
resp = client.get("/api/_probe_classroom")
check("Classroom 401 triggers one re-release and retry",
      resp.status_code == 200 and FakeBroker.calls == 2
      and FakeBroker.tokens_issued == 2,
      f"calls={FakeBroker.calls}")
StubClassroom.stale_tokens = set()

# ---------------------------------------------------------------------------
# 5a. Broker 404 -> NotConnectedError (+ legacy session creds ignored)
# ---------------------------------------------------------------------------
FakeBroker.reset("not_found")
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
      FakeBroker.calls == 1 and FakeBroker.bearers == ["cat-1"])
with client.session_transaction() as sess:
    del sess["classroom_credentials"]

# ---------------------------------------------------------------------------
# 5b. Broker 403 + missing_scopes -> MissingClassroomScopesError
# ---------------------------------------------------------------------------
FakeBroker.reset("missing_scopes")
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
# 5c. Broker 401 -> CampusSessionExpiredError (JSON 401 / browser re-login)
# ---------------------------------------------------------------------------
FakeBroker.reset("unauthorized")
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
# 5d. Broker 400 AUTH_INVALID_SCOPE -> BrokerConfigError, loud, not 500
# ---------------------------------------------------------------------------
FakeBroker.reset("invalid_scope")
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
resp = client.get("/classroom/")
check("broker 400 on browser path renders a flash, not a 500 or loop",
      resp.status_code == 200
      and "deployment configuration problem".encode() in resp.data)

# 5e. Anything else (500 ServerError) -> BrokerConfigError too
FakeBroker.reset("server_error")
reset_fake()
log_records.clear()
resp = client.get("/api/_probe_classroom")
body = resp.get_json()
check("broker 500 -> 502 JSON classroom_broker_config (anything-else arm)",
      resp.status_code == 502 and body["error"]["code"] == "classroom_broker_config",
      f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")
check("500 rejection logged loudly at ERROR",
      any(r.levelno == logging.ERROR and "broker" in r.getMessage()
          for r in log_records), str([r.getMessage() for r in log_records]))

# ---------------------------------------------------------------------------
# 6. No campus login session -> CampusSessionExpiredError
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
reset_fake()
anon = app.test_client()  # deliberately no verify_user seed
resp = anon.get("/api/_probe_classroom")
check("no campus login session -> 401 JSON campus_session_expired",
      resp.status_code == 401
      and resp.get_json()["error"]["code"] == "campus_session_expired",
      f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")

# ---------------------------------------------------------------------------
# 7. Unusable releases and an unreachable broker -> OAuthFlowError, no 500
# ---------------------------------------------------------------------------
FakeBroker.reset("empty_release")
reset_fake()
resp = client.get("/api/_probe_classroom")
check("release without an access_token -> 502 JSON classroom_oauth_flow_error",
      resp.status_code == 502
      and resp.get_json()["error"]["code"] == "classroom_oauth_flow_error",
      f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")

FakeBroker.reset("malformed")
reset_fake()
resp = client.get("/api/_probe_classroom")
check("malformed release -> 502 JSON classroom_oauth_flow_error",
      resp.status_code == 502
      and resp.get_json()["error"]["code"] == "classroom_oauth_flow_error",
      f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")

FakeBroker.reset("unreachable")
reset_fake()
resp = client.get("/classroom/")
html = resp.get_data(as_text=True)
check("unreachable broker renders the page with an error alert",
      resp.status_code == 200
      and "Could not check your Classroom connection" in html)

# ---------------------------------------------------------------------------
# 8. Send scopes: broker asked for the askable subset; cap-scope gated locally
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
reset_fake()
resp = client.get("/api/_probe_send")
body = resp.get_json()
check("send probe: broker asked ONLY for the MVP subset (conservative ask)",
      FakeBroker.asks and FakeBroker.asks[0] == ALL_MVP,
      str(FakeBroker.asks))
check("send probe: unasked feature scope fails the local gate",
      resp.status_code == 403
      and body["error"]["code"] == "classroom_missing_scopes"
      and body["error"]["missing_scopes"] == list(cauth.CLASSROOM_SCOPES_SEND),
      resp.get_data(as_text=True)[:300])

# ---------------------------------------------------------------------------
# 9. required_scopes=() -> no gate, no min_scopes ask
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
reset_fake()
resp = client.get("/api/_probe_nogate")
check("required_scopes=() skips the gate and the min_scopes ask",
      resp.status_code == 200 and FakeBroker.asks == [None],
      f"{resp.status_code} {FakeBroker.asks}")

# ---------------------------------------------------------------------------
# 10. /classroom page states
# ---------------------------------------------------------------------------
FakeBroker.reset("ok")
reset_fake()
resp = client.get("/classroom/")
html = resp.get_data(as_text=True)
check("page renders Connected from the broker",
      resp.status_code == 200 and "Connected" in html and CAMPUS_EMAIL in html)
check("page lists granted scopes and live courses",
      all(s in html for s in ("classroom.rosters.readonly", "CS1101s")))

FakeBroker.reset("not_found")
resp = client.get("/classroom/")
html = resp.get_data(as_text=True)
check("page renders Not-connected with the profile CTA (no 500)",
      resp.status_code == 200 and "Not connected" in html
      and "https://profile.example/profile/integrations" in html)

server.shutdown()
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
