"""Verification for issue #12 (real iframe context, PRD §4.4 GC-6/GC-7).

The Classroom bridge is faked at its two seams — the campus.auth broker
release and the Classroom REST getAddOnContext — so no network and no
real Google account are involved. The app builds in **deployment mode**
(no FLASK_DEBUG), which is the path real Classroom traffic takes.

1. Session-cookie policy (issue #12's enabler): the deployment app gets
   SameSite=None + Secure + Partitioned (CHIPS); a FLASK_DEBUG app keeps
   Flask's Lax default and runs with debug True.
2. Real context on /addon/teacher: URL params validated server-side via
   getAddOnContext (attachmentId forwarded, add-on scope asked from the
   broker), context source "classroom", role TEACHER.
3. Param validation: missing/malformed params → 400; review requires
   submissionId; anonymous still redirects to Campus sign-in.
4. Role enforcement: getAddOnContext's userRole must match the view —
   student on the teacher view (and vice versa) → 403 role_mismatch.
5. Forgery: getAddOnContext 403/404 (post not visible to this user)
   → 403 context_rejected with a generic message that leaks nothing.
6. Bridge errors on /addon/*: NotConnectedError / missing scopes render
   the HTML connect prompt (403, CTA opens the integrations page in a
   new tab); a JSON Accept still gets the machine-readable error.
7. Mock gate: only a debug server serves mock context — and makes no
   broker calls at all.
8. CSRF guard: /api/v1/* mutations require the X-Requested-With header
   (the SameSite=None session crosses sites); GETs are unaffected.
9. Discovery: still public, no login, unchanged.

Usage: .venv/Scripts/python.exe scripts/verify_issue_12.py
"""

import contextlib
import os
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
# FLASK_DEBUG is deliberately NOT set: the primary app below builds in
# deployment mode (real-context path + SameSite=None cookies).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-12-secret"

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402

TEACHER = "uid-user-teacher"
STUDENT = "uid-user-student"
# The seams are keyed by the signed-in email (seed_user builds it as
# <id>@nyjc.edu.sg), which is what fetch_broker_credential publishes.
TEACHER_EMAIL = f"{TEACHER}@nyjc.edu.sg"
STUDENT_EMAIL = f"{STUDENT}@nyjc.edu.sg"

# --- Fake auth bridge seams (verify_issue_28 pattern) -----------------------

CURRENT_EMAIL: str | None = None
BROKER_CONNECTED_BY_EMAIL: dict[str, bool] = {}
BROKER_SCOPES_BY_EMAIL: dict[str, list[str]] = {}
ADDON_ROLE_BY_EMAIL: dict[str, str] = {}
ADDON_FAIL_BY_EMAIL: dict[str, tuple[int, str]] = {}
BROKER_CALLS: list[list[str]] = []
CLASSROOM_CALLS: list[tuple[str, str, dict]] = []

ALL_SCOPES = list(cauth.CLASSROOM_SCOPES_MVP)


class FakeBroker:
    def token(self, provider, integration, *, min_scopes=None):  # noqa: ARG002
        BROKER_CALLS.append(list(min_scopes or []))
        email = CURRENT_EMAIL
        if not BROKER_CONNECTED_BY_EMAIL.get(email, True):
            raise campus_errors.NotFoundError(
                404,
                f"No google.classroom credential for user {email}; "
                "complete the Classroom connect flow first (via the "
                "campus-profile integrations page)",
                details={},
            )
        return {
            "provider": "google.classroom",
            "user_id": email,
            "access_token": "broker-access-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(BROKER_SCOPES_BY_EMAIL.get(email, ALL_SCOPES)),
        }


def _fake_classroom_request(self, method, path, *, params=None, json=None):  # noqa: ARG001
    CLASSROOM_CALLS.append((method, path, dict(params or {})))
    self._access_token()  # mirror the real flow: release before the call
    if not path.endswith("/addOnContext"):
        raise AssertionError(f"unexpected Classroom path {path!r}")
    # Identity comes from the released credential (fetch_broker_credential
    # maps broker user_id → email) — CURRENT_EMAIL is already cleared by
    # the time the Classroom call runs (it sits outside with_user_session).
    email = self._creds["email"]
    fail = ADDON_FAIL_BY_EMAIL.get(email)
    if fail:
        raise cauth.ClassroomAPIError(fail[0], fail[1])
    item_id = path.split("/")[4]
    return {"itemId": item_id, "userRole": ADDON_ROLE_BY_EMAIL.get(email)}


# --- Minimal assignment fakes for the CSRF-guard checks ----------------------

ASSIGNMENTS: dict = {}


class FakeAssignment:
    def __init__(self, id, created_by, title):  # noqa: A002
        self.id, self.created_by, self.title = id, created_by, title

    def to_resource(self):
        return {
            "id": self.id, "created_by": self.created_by, "title": self.title,
            "description": None, "questions": [], "classroom_links": [],
        }


class FakeAssignments:
    def new(self, title, description=None, questions=None, classroom_links=None):  # noqa: ARG002
        assignment_id = f"a-new-{len(ASSIGNMENTS) + 1}"
        ASSIGNMENTS[assignment_id] = FakeAssignment(assignment_id, CURRENT_EMAIL, title)
        return ASSIGNMENTS[assignment_id]

    def list(self, created_by=None, **_):  # noqa: ARG002
        return [a for a in ASSIGNMENTS.values() if a.created_by == created_by]


class FakeAPI:
    def __init__(self):
        self.assignments = FakeAssignments()


class FakeCampus:
    """auth.broker host; with_user_session mirrors the library contract."""

    def __init__(self):
        self.auth = SimpleNamespace(broker=FakeBroker())
        self.api = FakeAPI()

    @staticmethod
    def _current_email():
        user = flask.g.get("user")
        return getattr(user, "email", None) if user else None

    @contextlib.contextmanager
    def with_user_session(self):
        global CURRENT_EMAIL
        CURRENT_EMAIL = self._current_email()
        try:
            yield self
        finally:
            CURRENT_EMAIL = None

    def with_app_session(self):
        return self.with_user_session()


FAKE_CAMPUS = FakeCampus()

cauth.ClassroomClient.request = _fake_classroom_request


# --- App with a faked Campus login (push_context reads verify_user) ----------

def _fake_push_context(self):  # noqa: ARG001
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


campus_auth_v1.AuthRoot.push_context = _fake_push_context

app = create_app()
app.campus = FAKE_CAMPUS

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
    """Mutations carry the X-Requested-With header the app now requires."""
    for method in ("post", "patch", "put", "delete"):
        original = getattr(client, method)

        def wrapper(path, *a, _orig=original, **kw):
            headers = kw.pop("headers", None) or {}
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            kw["headers"] = headers
            return _orig(path, *a, **kw)

        setattr(client, method, wrapper)


anon_client = app.test_client()
teacher_client = app.test_client()
seed_user(teacher_client, TEACHER)
_ajaxify(teacher_client)
student_client = app.test_client()
seed_user(student_client, STUDENT)
_ajaxify(student_client)

# Script the bridge: both accounts connected with the full grant; the
# Classroom context reports each caller's real role.
BROKER_CONNECTED_BY_EMAIL.update({TEACHER_EMAIL: True, STUDENT_EMAIL: True})
ADDON_ROLE_BY_EMAIL.update({TEACHER_EMAIL: "TEACHER", STUDENT_EMAIL: "STUDENT"})

TEACHER_PARAMS = "courseId=c1&itemId=i1&attachmentId=att1"

# --- 1. Session-cookie policy ------------------------------------------------
check("deployment app: session cookie SameSite=None",
      app.config["SESSION_COOKIE_SAMESITE"] == "None",
      f"got {app.config.get('SESSION_COOKIE_SAMESITE')}")
check("deployment app: session cookie Secure",
      app.config["SESSION_COOKIE_SECURE"] is True)
check("deployment app: session cookie Partitioned (CHIPS)",
      app.config["SESSION_COOKIE_PARTITIONED"] is True)
check("deployment app: debug off (mock unreachable)",
      app.debug is False)

os.environ["FLASK_DEBUG"] = "1"
debug_app = create_app()
debug_app.campus = FAKE_CAMPUS
check("debug app: session cookie keeps the unset default (browser Lax)",
      debug_app.config.get("SESSION_COOKIE_SAMESITE") in (None, "Lax"),
      f"got {debug_app.config.get('SESSION_COOKIE_SAMESITE')}")
check("debug app: debug on (mock allowed)",
      debug_app.debug is True)
os.environ.pop("FLASK_DEBUG", None)

# --- 2. Real context, happy path ---------------------------------------------
CLASSROOM_CALLS.clear()
BROKER_CALLS.clear()
FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6IjEyMyJ9.fake-payload.sig"
r = teacher_client.get(
    f"/addon/teacher?{TEACHER_PARAMS}&addOnToken={FAKE_TOKEN}")
page = r.data
check("teacher view renders on valid params", r.status_code == 200,
      f"status={r.status_code} body={page[:300]}")
check('context source is "classroom"', b'"source": "classroom"' in page
      or b'"source":"classroom"' in page, f"body={page[:400]}")
check("context carries the Classroom role", b'"userRole": "TEACHER"' in page
      or b'"userRole":"TEACHER"' in page, f"body={page[:400]}")
addon_calls = [c for c in CLASSROOM_CALLS if c[1].endswith("/addOnContext")]
check("getAddOnContext called once with token + attachment",
      len(addon_calls) == 1
      and addon_calls[0][2].get("attachmentId") == "att1"
      and addon_calls[0][2].get("addOnToken") == FAKE_TOKEN,
      f"calls={CLASSROOM_CALLS}")
check("broker asked for the teacher add-on scope",
      len(BROKER_CALLS) == 1
      and "https://www.googleapis.com/auth/classroom.addons.teacher"
      in BROKER_CALLS[0],
      f"broker calls={BROKER_CALLS}")
check("addOnToken value is never reflected into the page",
      FAKE_TOKEN.encode() not in page)

# --- 3. Param validation ------------------------------------------------------
r = teacher_client.get("/addon/teacher?courseId=c1&itemId=i1")
check("missing attachmentId rejected with 400",
      r.status_code == 400 and r.get_json()["error"]["code"] == "invalid_params",
      f"status={r.status_code} body={r.get_json()}")
r = teacher_client.get("/addon/teacher?courseId=../etc&itemId=i1&attachmentId=a")
check("malformed courseId rejected with 400",
      r.status_code == 400, f"status={r.status_code}")
r = teacher_client.get("/addon/review?courseId=c1&itemId=i1&attachmentId=a")
check("review without submissionId rejected with 400",
      r.status_code == 400, f"status={r.status_code}")
r = teacher_client.get(
    f"/addon/teacher?courseId={'c' * 3000}&itemId=i1&attachmentId=a")
check("overlong courseId rejected with 400",
      r.status_code == 400, f"status={r.status_code}")
r = anon_client.get(f"/addon/teacher?{TEACHER_PARAMS}")
check("anonymous iframe request still redirects to Campus sign-in",
      r.status_code == 302 and "/login" in (r.headers.get("Location") or ""),
      f"status={r.status_code} loc={r.headers.get('Location')}")

# --- 4. Role enforcement -------------------------------------------------------
r = student_client.get(f"/addon/teacher?{TEACHER_PARAMS}")
check("student on the teacher view → 403 role_mismatch",
      r.status_code == 403
      and r.get_json()["error"]["code"] == "role_mismatch",
      f"status={r.status_code} body={r.get_json()}")
r = teacher_client.get(f"/addon/student?{TEACHER_PARAMS}")
check("teacher on the student view → 403 role_mismatch",
      r.status_code == 403
      and r.get_json()["error"]["code"] == "role_mismatch",
      f"status={r.status_code}")
r = student_client.get(f"/addon/student?{TEACHER_PARAMS}")
check("student view renders for the student", r.status_code == 200
      and b'"source": "classroom"' in r.data, f"status={r.status_code}")
r = teacher_client.get(f"/addon/review?{TEACHER_PARAMS}&submissionId=s1")
check("review view renders for the teacher with submissionId",
      r.status_code == 200 and b'"userRole": "TEACHER"' in r.data,
      f"status={r.status_code}")

# --- 5. Forgery ----------------------------------------------------------------
ADDON_FAIL_BY_EMAIL[TEACHER_EMAIL] = (403, "The caller does not have permission")
r = teacher_client.get("/addon/teacher?courseId=cF&itemId=iF&attachmentId=aF")
check("getAddOnContext 403 (forged post) → 403 context_rejected",
      r.status_code == 403
      and r.get_json()["error"]["code"] == "context_rejected",
      f"status={r.status_code} body={r.get_json()}")
ADDON_FAIL_BY_EMAIL[TEACHER_EMAIL] = (404, "Requested entity was not found")
r = teacher_client.get("/addon/teacher?courseId=cF&itemId=iF&attachmentId=aF")
check("getAddOnContext 404 → 403 too (existence not revealed)",
      r.status_code == 403
      and b"cF" not in r.data and b"iF" not in r.data,
      f"status={r.status_code} body={r.data[:300]}")
ADDON_FAIL_BY_EMAIL.clear()

# --- 6. Bridge errors on /addon/* ----------------------------------------------
BROKER_CONNECTED_BY_EMAIL[TEACHER_EMAIL] = False
r = teacher_client.get(f"/addon/teacher?{TEACHER_PARAMS}")
check("not-connected teacher gets the HTML connect prompt (403)",
      r.status_code == 403
      and b"Connect your Google Classroom first" in r.data,
      f"status={r.status_code}")
check("connect CTA opens the integrations page in a new tab",
      b'target="_blank"' in r.data)
check("connect prompt leaks no context", b'"courseId"' not in r.data)

r = teacher_client.get(f"/addon/teacher?{TEACHER_PARAMS}",
                       headers={"Accept": "application/json"})
check("JSON Accept keeps the machine-readable error on /addon/*",
      r.status_code == 403
      and r.get_json()["error"]["code"] == "classroom_not_connected",
      f"status={r.status_code} body={r.get_json()}")
BROKER_CONNECTED_BY_EMAIL[TEACHER_EMAIL] = True

BROKER_SCOPES_BY_EMAIL[TEACHER_EMAIL] = [
    s for s in ALL_SCOPES
    if s != "https://www.googleapis.com/auth/classroom.addons.teacher"
]
r = teacher_client.get(f"/addon/teacher?{TEACHER_PARAMS}")
check("missing add-on scope renders the connect prompt too",
      r.status_code == 403
      and b"Connect your Google Classroom first" in r.data,
      f"status={r.status_code} body={r.data[:300]}")
BROKER_SCOPES_BY_EMAIL.clear()

# --- 7. Mock gate (debug only) --------------------------------------------------
BROKER_CALLS.clear()
debug_teacher = debug_app.test_client()
seed_user(debug_teacher, TEACHER)
r = debug_teacher.get("/addon/teacher?courseId=c1&itemId=i1&attachmentId=a1")
check("debug server serves mock context",
      r.status_code == 200 and b'"source": "mock"' in r.data,
      f"status={r.status_code} body={r.data[:300]}")
check("debug mock path makes no broker calls", BROKER_CALLS == [],
      f"broker calls={BROKER_CALLS}")

# --- 8. CSRF guard ---------------------------------------------------------------
plain_teacher = app.test_client()  # no ajaxify: simulates a bare cross-site post
seed_user(plain_teacher, TEACHER)
r = plain_teacher.post("/api/v1/assignments", json={"title": "No header"})
check("mutation without X-Requested-With → 403 csrf_header_missing",
      r.status_code == 403
      and r.get_json()["error"]["code"] == "csrf_header_missing",
      f"status={r.status_code} body={r.get_json()}")
r = teacher_client.post("/api/v1/assignments", json={"title": "With header"})
check("mutation with the header passes the guard",
      r.status_code == 201, f"status={r.status_code} body={r.get_json()}")
r = teacher_client.get("/api/v1/assignments")
check("GETs are not subject to the guard",
      r.status_code == 200, f"status={r.status_code}")

# --- 9. Discovery (unchanged, public) ---------------------------------------------
r = anon_client.get("/addon/discovery")
check("discovery stays public", r.status_code == 200,
      f"status={r.status_code}")

# ----------------------------------------------------------------------------------
print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print("All checks passed.")
