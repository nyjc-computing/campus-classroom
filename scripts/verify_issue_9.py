"""Verification for issue #9: Campus OAuth incremental Classroom scopes +
Google↔Campus identity mapping — LEGACY in-app flow checks.

NOTE (issue #30, 2026-10-03): `with_classroom_session()` now releases the
user's Classroom token from campus.auth's token broker, and the connect UX
is the campus-profile integrations page. The in-app Google OAuth flow below
(/classroom/authorize|callback + Flask-session token storage) is legacy,
kept working through the transition and removed once the broker path is
proven (issue #30 "Retire" lane). This script therefore now verifies ONLY
the legacy flow:

1. /classroom/authorize redirects to Google with the MVP scope set,
   access_type=offline, include_granted_scopes=true, prompt=consent,
   login_hint = Campus email, hd = workspace domain,
   redirect_uri = {PUBLIC_URL}/classroom/callback.
2. Happy-path callback exchanges the code, enforces the email match, and
   stores the credential in the Flask session (legacy storage contract —
   no longer read by with_classroom_session()).
3. State (CSRF) mismatch is refused loudly (400), nothing stored.
4. Google email ≠ Campus email is refused loudly (403), nothing stored.
5. User-cancelled consent (error=access_denied) lands on a clean flash.
6. The `?scopes=` narrowing parameter requests only the missing subset,
   still with include_granted_scopes=true.
7. GOOGLE_CLIENT_ID/SECRET unset produces clean config errors, not 500s.
8. POST /classroom/disconnect clears the stored credential.

The swapped seam (broker release, error mapping, scope gates, token
refresh, /classroom page states) is verified by scripts/verify_issue_30.py.

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

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
import flask  # noqa: E402
import requests  # noqa: E402
import requests.adapters  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402

# One pooled, retrying session for every HTTP call the legacy flow makes:
# fresh loopback connections intermittently abort on this dev box (WinError
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
OTHER_EMAIL = "someone@gmail.com"

SCOPE_BASE = "https://www.googleapis.com/auth/"
ALL_MVP = list(cauth.CLASSROOM_SCOPES_MVP)
IDENTITY = list(cauth.GOOGLE_IDENTITY_SCOPES)
SUBSET_SCOPES = [s for s in ALL_MVP if "rosters" not in s and
                 "student-submissions.students" not in s]
MISSING = cauth.missing_scopes(ALL_MVP, SUBSET_SCOPES)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Stub Google OAuth server (legacy flow only; no Classroom API needed here)
# ---------------------------------------------------------------------------
class StubGoogle(BaseHTTPRequestHandler):

    """Minimal Google endpoints with scriptable token/email behaviour."""
    protocol_version = "HTTP/1.1"

    codes: dict = {}          # code -> {"scopes": [...], "email": str}
    token_emails: dict = {}   # access_token -> email (userinfo lookup)

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
        return self.headers.get("Authorization", "").removeprefix("Bearer ").strip()

    def do_POST(self):
        if urlsplit(self.path).path != "/token":
            self._send({"error": "not_found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        if form.get("grant_type", [""])[0] == "authorization_code":
            code = form.get("code", [""])[0]
            spec = self.codes.get(code)
            if not spec:
                self._send({"error": "invalid_grant"}, 400)
                return
            access = f"access-for-{code}"
            self.token_emails[access] = spec["email"]
            self._send({
                "access_token": access,
                "refresh_token": f"refresh-for-{code}",
                "expires_in": 3600,
                "scope": " ".join(spec["scopes"]),
                "token_type": "Bearer",
            })
        else:
            self._send({"error": "unsupported_grant_type"}, 400)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/userinfo":
            email = self.token_emails.get(self._bearer())
            self._send({
                "sub": "google-sub-123",
                "email": email or CAMPUS_EMAIL,
                "email_verified": True,
            })
        else:
            self._send({"error": "not_found"}, 404)


server = ThreadingHTTPServer(("127.0.0.1", 0), StubGoogle)
STUB_PORT = server.server_address[1]
STUB = f"http://127.0.0.1:{STUB_PORT}"
threading.Thread(target=server.serve_forever, daemon=True).start()

# Point the legacy flow at the stub (module-level constants, monkeypatched).
cauth.GOOGLE_AUTH_URL = f"{STUB}/auth"
cauth.GOOGLE_TOKEN_URL = f"{STUB}/token"
cauth.GOOGLE_USERINFO_URL = f"{STUB}/userinfo"


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


def seed_user(client):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": CAMPUS_EMAIL, "email": CAMPUS_EMAIL}


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
# 2. Happy path: callback -> stored credential (legacy storage contract)
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
# 6. Incremental re-consent: ?scopes= requests only the missing subset
# ---------------------------------------------------------------------------
seed_user(client)
loc = authorize_location(client, f"?{urlencode({'scopes': ' '.join(MISSING)})}")
inc_params = auth_params(loc)
check("incremental authorize requests only the missing scopes",
      set(inc_params.get("scope", "").split()) == set(IDENTITY) | set(MISSING),
      inc_params.get("scope", ""))
check("incremental authorize still merges granted scopes",
      inc_params.get("include_granted_scopes") == "true")

# ---------------------------------------------------------------------------
# 7. Unconfigured Google credentials -> clean errors, not 500s
# ---------------------------------------------------------------------------
saved = (os.environ.pop("GOOGLE_CLIENT_ID", None),
         os.environ.pop("GOOGLE_CLIENT_SECRET", None))
try:
    resp = client.get("/classroom/authorize")
    check("authorize without GOOGLE_* config redirects with a flash",
          resp.status_code == 302
          and resp.headers["Location"].endswith("/classroom/"))
finally:
    if saved[0]:
        os.environ["GOOGLE_CLIENT_ID"] = saved[0]
    if saved[1]:
        os.environ["GOOGLE_CLIENT_SECRET"] = saved[1]

# ---------------------------------------------------------------------------
# 8. Disconnect
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
