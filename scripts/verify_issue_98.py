"""Verification for issue #98: a dead Campus login-session grant
mid-session must degrade to re-authentication, never a 500 or the
/classroom/ connection page.

The #98 state: the Flask app session stays valid but the stored campus
grant behind it is gone/unrefreshable, so every
campus.with_user_session() mint fails with the login-session token
route's OAuth invalid_grant 400 ("No campus grant … re-authentication
is required"). No network and no real Campus are involved — the Campus
client is faked at the session seams:

1. campus_session.is_reauth_required: the invalid_grant discriminator
   (envelope details form and the production-stripped AUTH_* code form
   via the SDK's real response parsing) and its negatives.
2. API routes answer 401 campus_session_expired JSON (not 500) on
   submit / responses-save / for-student resume.
3. The /a/ answer sheet redirects browser paths into
   /login?next=<path+query> — including the ?course= pin — instead of
   flashing onto the Classroom-connection page; the app-scope
   fallback path redirects too; a non-invalid_grant SDK failure still
   surfaces as a 500 (behavior preserved).
4. The auth-bridge seam: a broker release whose user-session mint
   fails invalid_grant raises CampusSessionExpiredError (re-login),
   not BrokerConfigError ("deployment configuration problem").

Usage: .venv/Scripts/python.exe scripts/verify_issue_98.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-98-secret"

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import (
    campus_session,  # noqa: E402
    create_app,  # noqa: E402
    enrollment,  # noqa: E402
)
from apps.classroom import classroom_auth as cauth  # noqa: E402

STUDENT = "uid-user-student"
TEACHER = "uid-user-teacher"

INVALID_GRANT_400 = campus_errors.BadRequestError(
    400,
    "No campus grant for this user and client; re-authentication is required",
    details={"oauth_error": "invalid_grant"},
)


class _Assignments:
    """client.api.assignments stand-in serving one scripted assignment."""

    def __init__(self, assignments):
        self._assignments = assignments

    def __getitem__(self, assignment_id):
        if assignment_id not in self._assignments:
            raise campus_errors.NotFoundError(
                404, "Assignment not found")
        record = self._assignments[assignment_id]
        return SimpleNamespace(get=lambda: record)


class FakeBroker:
    """auth.broker stand-in: hands out one canned release dict."""

    @staticmethod
    def token(provider, integration, *, min_scopes=None):  # noqa: ARG002, ARG004
        assert (provider, integration) == ("google", "classroom")
        return {"access_token": "broker-token-1", "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/classroom.courses.readonly"}


class FakeCampus:
    """campus_python.Campus stand-in for the #98 state.

    The app-scoped session works (the /a/ primary read succeeds). The
    user-scoped session fails at the login-session token mint with the
    OAuth invalid_grant 400 the dead campus grant produces; fail_mints
    bounds how many consecutive mints fail (default: every one — the
    dead-grant state; a small N simulates a transient store failure
    that the #98 retry must paper over).
    """

    def __init__(self, assignments, fail_mints=10**9):
        self._assignments = assignments
        self.fail_mints = fail_mints
        self.mint_calls = 0
        self.auth = SimpleNamespace(broker=FakeBroker())

    @contextmanager
    def with_app_session(self):
        yield SimpleNamespace(
            api=SimpleNamespace(assignments=_Assignments(self._assignments)))

    @contextmanager
    def with_user_session(self):
        self.mint_calls += 1
        if self.mint_calls <= self.fail_mints:
            raise INVALID_GRANT_400
        yield SimpleNamespace(auth=SimpleNamespace(broker=FakeBroker()))


ASSIGNMENTS = {
    "a1": SimpleNamespace(
        id="a1", created_by=TEACHER, title="Algebra Worksheet",
        description="", questions=[], classroom_links=[
            SimpleNamespace(course_id="course-1", coursework_id="cw-1"),
        ],
        created_at="2026-11-09T00:00:00Z",
    ),
}

FAKE_CAMPUS = FakeCampus(ASSIGNMENTS)


def _fake_push_context(*_args):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context

app = create_app()
app.campus = FAKE_CAMPUS
# _broker_release builds its own throwaway Campus per release (#92) —
# the constructor is the release-side seam; hand it FAKE_CAMPUS too.
campus_python.Campus = lambda timeout=None: FAKE_CAMPUS  # noqa: ARG005

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond:
        failures.append(name)


def seed_user(client, user_id):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": user_id, "email": f"{user_id}@nyjc.edu.sg"}


def _ajaxify(client):
    """Mutations carry the X-Requested-With header (#12 CSRF guard)."""
    for method in ("post", "patch", "put", "delete"):
        original = getattr(client, method)

        def wrapper(path, *a, _orig=original, **kw):
            headers = kw.pop("headers", None) or {}
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            kw["headers"] = headers
            return _orig(path, *a, **kw)

        setattr(client, method, wrapper)


# --- 1. the invalid_grant discriminator --------------------------------------
print("== campus_session.is_reauth_required ==")

check("invalid_grant in envelope details -> re-auth",
      campus_session.is_reauth_required(INVALID_GRANT_400))

# Production strips the envelope details; the SDK recovers the OAuth
# error from the AUTH_* code (#87) — test through the real parser.
stripped = campus_errors.APIError.with_status_code(
    400,
    {"error": {
        "code": "AUTH_INVALID_GRANT",
        "message": "No campus grant for this user and client; "
                   "re-authentication is required",
        "details": None,
    }},
)
check("production-stripped AUTH_INVALID_GRANT envelope -> re-auth",
      isinstance(stripped, campus_errors.BadRequestError)
      and campus_session.is_reauth_required(stripped),
      f"oauth_error={stripped.oauth_error!r}")

other_grant = campus_errors.BadRequestError(
    400, "Invalid client", details={"oauth_error": "invalid_client"})
check("invalid_client -> NOT re-auth",
      not campus_session.is_reauth_required(other_grant))

not_found = campus_errors.NotFoundError(404, "Assignment not found")
check("404 NotFoundError -> NOT re-auth",
      not campus_session.is_reauth_required(not_found))

plain_400 = campus_errors.BadRequestError(400, "Empty request body")
check("plain 400 with no oauth_error -> NOT re-auth",
      not campus_session.is_reauth_required(plain_400))


# --- 2. API routes: 401 campus_session_expired, never a raw 500 --------------
print("== API routes answer 401 campus_session_expired ==")

client = app.test_client()
_ajaxify(client)
seed_user(client, STUDENT)

r = client.post("/api/v1/submissions/uid-submission-1/submit", json={})
check("POST .../submit -> 401 campus_session_expired",
      r.status_code == 401
      and r.get_json(silent=True)
      and r.get_json().get("error", {}).get("code") == "campus_session_expired",
      f"status={r.status_code} body={r.get_data(as_text=True)[:120]!r}")

r = client.patch(
    "/api/v1/submissions/uid-submission-1",
    json={"responses": [{"question_id": "q1", "response_text": "hi"}]})
check("PATCH .../responses -> 401 campus_session_expired",
      r.status_code == 401
      and r.get_json(silent=True)
      and r.get_json().get("error", {}).get("code") == "campus_session_expired",
      f"status={r.status_code} body={r.get_data(as_text=True)[:120]!r}")

r = client.get(
    "/api/v1/submissions/for-student?assignment_id=a1")
check("GET .../for-student -> 401 campus_session_expired",
      r.status_code == 401
      and r.get_json(silent=True)
      and r.get_json().get("error", {}).get("code") == "campus_session_expired",
      f"status={r.status_code} body={r.get_data(as_text=True)[:120]!r}")

r = client.post("/api/v1/submissions", json={
    "assignment_id": "a1", "course_id": "course-1"})
check("POST .../submissions (create) -> 401 campus_session_expired",
      r.status_code == 401
      and r.get_json(silent=True)
      and r.get_json().get("error", {}).get("code") == "campus_session_expired",
      f"status={r.status_code} body={r.get_data(as_text=True)[:120]!r}")


# --- 3. /a/ browser paths: /login?next=… redirect, not /classroom/ -----------
print("== /a/ redirects to /login with path+query preserved ==")

student_browser = app.test_client()
seed_user(student_browser, STUDENT)

r = student_browser.get("/a/a1?course=course-1")
loc = r.headers.get("Location", "")
next_url = parse_qs(urlsplit(loc).query).get("next", [""])[0]
check("dead grant on /a/ -> 302 into /login (not /classroom/)",
      r.status_code == 302 and urlsplit(loc).path == "/login",
      f"status={r.status_code} location={loc!r}")
check("next= preserves the page path",
      urlsplit(next_url).path == "/a/a1",
      f"next={next_url!r}")
check("next= preserves the ?course= pin",
      parse_qs(urlsplit(next_url).query).get("course") == ["course-1"],
      f"next={next_url!r}")

r = student_browser.get("/a/a1")
loc = r.headers.get("Location", "")
next_url = parse_qs(urlsplit(loc).query).get("next", [""])[0]
check("query-less /a/ redirects without a bare trailing '?'",
      r.status_code == 302 and next_url == "/a/a1",
      f"status={r.status_code} next={next_url!r}")


class AppScopeDeadCampus(FakeCampus):
    """App scope unavailable too (non-apphost deployment): the /a/
    primary read fails generically, so the route falls back to the
    user session — which is dead (#98)."""

    @contextmanager
    def with_app_session(self):
        raise RuntimeError("app scope unavailable on this deployment")
        yield  # pragma: no cover


app.campus = AppScopeDeadCampus(ASSIGNMENTS)
r = student_browser.get("/a/a1")
loc = r.headers.get("Location", "")
check("/a/ with app scope down + dead grant -> still a /login redirect",
      r.status_code == 302 and urlsplit(loc).path == "/login",
      f"status={r.status_code} location={loc!r}")
app.campus = FAKE_CAMPUS

# A non-invalid_grant failure from the mint is still an unexpected
# error: it must keep surfacing as a 500, not masquerade as re-auth.
class OtherErrorCampus(FakeCampus):
    @contextmanager
    def with_user_session(self):
        raise campus_errors.BadRequestError(
            400, "Invalid client", details={"oauth_error": "invalid_client"})
        yield  # pragma: no cover


app.campus = OtherErrorCampus(ASSIGNMENTS)
r = client.post("/api/v1/submissions/uid-submission-1/submit", json={})
check("non-invalid_grant mint failure -> still 500 (behavior preserved)",
      r.status_code == 500,
      f"status={r.status_code}")
app.campus = FAKE_CAMPUS


# --- 4. the auth-bridge seam --------------------------------------------------
print("== broker release maps invalid_grant to CampusSessionExpiredError ==")

assignment = ASSIGNMENTS["a1"]
try:
    enrollment.enrolled_courses(assignment)
    raised = None
except cauth.CampusSessionExpiredError as err:
    raised = err
except cauth.ClassroomAuthError as err:  # noqa: F841 — the old wrong answer
    raised = None
check("enrollment bridge -> CampusSessionExpiredError (not BrokerConfig)",
      isinstance(raised, cauth.CampusSessionExpiredError),
      f"raised={raised!r}")


# --- 5. the #98 retry: one transient mint failure is papered over ------
print("== transient mint failures are retried once ==")

transient = FakeCampus(ASSIGNMENTS, fail_mints=1)
app.campus = transient
opened = False
with app.app_context():
    try:
        with campus_session.user_session():
            opened = True
    except cauth.CampusSessionExpiredError:
        opened = False
check("one transient mint failure -> retried, session opens",
      opened and transient.mint_calls == 2,
      f"opened={opened} mint_calls={transient.mint_calls}")

persistent = FakeCampus(ASSIGNMENTS, fail_mints=2)
app.campus = persistent
expired = False
with app.app_context():
    try:
        with campus_session.user_session():
            opened = True
    except cauth.CampusSessionExpiredError:
        expired = True
check("two consecutive mint failures -> CampusSessionExpiredError",
      expired and persistent.mint_calls == 2,
      f"expired={expired} mint_calls={persistent.mint_calls}")

# The broker release builds its own Campus (the #92 throwaway); patch
# the constructor to hand out a transient-failing instance.
transient_broker = FakeCampus(ASSIGNMENTS, fail_mints=1)
campus_python.Campus = lambda timeout=None: transient_broker  # noqa: ARG005
with app.app_context():
    data = cauth._broker_release([enrollment.COURSES_READONLY_SCOPE])
check("broker release retries a transient mint failure",
      data.get("access_token") == "broker-token-1"
      and transient_broker.mint_calls == 2,
      f"data={ {k: data.get(k) for k in ('access_token',)} } "
      f"mint_calls={transient_broker.mint_calls}")
campus_python.Campus = lambda timeout=None: FAKE_CAMPUS  # noqa: ARG005

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("All checks passed.")
