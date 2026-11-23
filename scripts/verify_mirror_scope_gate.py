"""Verification for the #104 mirror-scope reconnect gate on /a/.

No network and no real Google account are involved. The seams are faked
like verify_issue_104 does them:

1. The campus session: an in-memory FakeCampus installed at both
   `app.campus` and the `campus_python.Campus` constructor (the #92
   throwaway the broker release builds per release), with a FakeBroker
   that RECORDS every release ask (min_scopes) and serves per-user
   scripted grants.
2. The Classroom REST API: `ClassroomClient.request` is replaced with a
   fake that answers courses.list honoring teacherId=me (membership vs
   teaching are different fixtures), so `can_review` and the enrollment
   check discriminate students from co-tutors like the real API.

Covers, per the reconnect-gate design (#104 scope demand on the student
path, the campus#1095 cap widening making reconnect actually grant the
scope):

1. A student whose stored grant predates coursework.me is GATED on /a/
   with the reconnect variant of the connect prompt (deny + CTA, 403),
   and the credential release behind that page load was ASKED for
   coursework.me.
2. A success cached for the MVP-only demand (the rule-4 listing's
   courses read) does NOT satisfy the /a/ demand — distinct cache keys.
3. After the scripted "reconnect" (grant now carries coursework.me) the
   same student gets the answer sheet (200).
4. A student with no google.classroom connection at all still gets the
   ORIGINAL connect variant (not the reconnect one).
5. A co-tutor (teacher of a linked course) whose grant also predates
   coursework.me is NOT gated: staff keep the MVP-only demand, and no
   release ask for them carries the mirror scope.
6. Unchanged behavior: the owner previews without any Classroom call, a
   stranger gets the bare gate page, an anonymous visitor is sent to
   sign-in.

Usage: .venv/Scripts/python.exe scripts/verify_mirror_scope_gate.py
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

# Repo root FIRST on sys.path: for a worktree run, the venv's editable
# install points at the main checkout, and without this the script would
# silently import (and verify) the checkout's code, not the worktree's.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
# CLIENT_ID/CLIENT_SECRET dummies keep create_app() happy in worktrees
# without a checkout .env; no network happens at construction time.
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-mirror-scope-gate-secret"
os.environ.setdefault("CLIENT_ID", "verify-client-id")
os.environ.setdefault("CLIENT_SECRET", "verify-client-secret")

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402

from apps.classroom import (  # noqa: E402
    classroom_auth as cauth,
)
from apps.classroom import (  # noqa: E402
    create_app,
    enrollment,
)

TEACHER = "uid-user-teacher"      # a1's creator (owner preview)
COTUTOR = "uid-user-cotutor"      # teaches course-1, pre-widen grant
STUDENT_PRE = "uid-user-student-pre"   # in course-1, pre-widen grant
STUDENT_UNCONNECTED = "uid-user-student-unc"  # in course-1, no connection
STRANGER = "uid-user-stranger"    # no courses, full grant

COURSES_SCOPE = "https://www.googleapis.com/auth/classroom.courses.readonly"
MIRROR_SCOPE = "https://www.googleapis.com/auth/classroom.coursework.me"
LINK = [COURSES_SCOPE, MIRROR_SCOPE]
LINK_NO_MIRROR = [COURSES_SCOPE]  # the pre-widen grant

COURSE = {"id": "course-1", "name": "CampusClass"}

# --- fake Classroom state -----------------------------------------------------

# Email -> courses they are in (as student or teacher).
MEMBERS_BY_EMAIL: dict[str, list[dict]] = {}
# Email -> courses they teach (the teacherId=me answer).
TEACHERS_BY_EMAIL: dict[str, list[dict]] = {}


def _fake_classroom_request(self, method, path, *, params=None, json=None):  # noqa: ARG001
    """ClassroomClient.request stand-in: courses.list only."""
    self._access_token()  # mirror the real flow: release before the call
    if path == "v1/courses" and method == "GET":
        email = self._creds["email"]
        if params and params.get("teacherId") == "me":
            return {"courses": TEACHERS_BY_EMAIL.get(email, [])}
        return {"courses": MEMBERS_BY_EMAIL.get(email, [])}
    raise AssertionError(f"unexpected Classroom call {method} {path!r}")


cauth.ClassroomClient.request = _fake_classroom_request

# --- fake Campus backend ------------------------------------------------------

# Email -> granted scopes; emails missing from the dict have NO
# google.classroom connection (the broker raises NotConnectedError).
GRANTED_SCOPES_BY_EMAIL: dict[str, list[str]] = {}
# Every release ask: (email_or_None, tuple(min_scopes)).
ASKED_SCOPES: list[tuple[str | None, tuple[str, ...]]] = []


class FakeBroker:
    """auth.broker stand-in: releases the scripted per-user grant."""

    def token(self, provider, integration, *, min_scopes=None):  # noqa: ARG002
        assert (provider, integration) == ("google", "classroom")
        email = CURRENT_EMAIL
        ASKED_SCOPES.append((email, tuple(sorted(min_scopes or ()))))
        scopes = GRANTED_SCOPES_BY_EMAIL.get(email)
        if scopes is None:
            raise cauth.NotConnectedError()
        return {
            "provider": "google.classroom",
            "user_id": email,
            "access_token": "broker-access-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(scopes),
        }


class FakeAssignment:
    def __init__(self, id, created_by, title, classroom_links=None):
        self.id = id
        self.created_by = created_by
        self.title = title
        self.questions = []
        self.classroom_links = classroom_links or []

    def to_resource(self):
        return {
            "id": self.id, "title": self.title,
            "created_by": self.created_by, "created_at": "2026-11-23T00:00:00Z",
            "description": "", "questions": [], "classroom_links": [],
        }


class FakeAssignments:
    def __init__(self, store):
        self._store = store

    def __getitem__(self, assignment_id):
        record = self._store.get(assignment_id)
        if record is None:
            raise RuntimeError("Assignment not found")
        return SimpleNamespace(get=lambda: record)

    def list(self):
        return list(self._store.values())


class FakeAPI:
    def __init__(self, assignments):
        self.assignments = FakeAssignments(assignments)


class FakeCampus:
    """Stands in for campus_python.Campus; sessions yield self."""

    def __init__(self, assignments):
        self.api = FakeAPI(assignments)
        self.auth = SimpleNamespace(broker=FakeBroker())

    @contextmanager
    def with_app_session(self):
        yield self

    @contextmanager
    def with_user_session(self):
        global CURRENT_EMAIL
        CURRENT_EMAIL = getattr(flask.g.get("user"), "email", None)
        try:
            yield self
        finally:
            CURRENT_EMAIL = None


ASSIGNMENTS = {
    "a1": FakeAssignment(
        "a1", TEACHER, "Algebra Worksheet",
        classroom_links=[SimpleNamespace(
            course_id="course-1", coursework_id="cw-1", attachment_id=None)],
    ),
}

FAKE_CAMPUS = FakeCampus(ASSIGNMENTS)

# Scripted enrollments + grants (email-keyed; the fake user session
# publishes the email).
MEMBERS_BY_EMAIL[f"{STUDENT_PRE}@nyjc.edu.sg"] = [COURSE]
MEMBERS_BY_EMAIL[f"{STUDENT_UNCONNECTED}@nyjc.edu.sg"] = [COURSE]
MEMBERS_BY_EMAIL[f"{COTUTOR}@nyjc.edu.sg"] = [COURSE]
MEMBERS_BY_EMAIL[f"{STRANGER}@nyjc.edu.sg"] = []
TEACHERS_BY_EMAIL[f"{COTUTOR}@nyjc.edu.sg"] = [COURSE]
GRANTED_SCOPES_BY_EMAIL[f"{STUDENT_PRE}@nyjc.edu.sg"] = LINK_NO_MIRROR
GRANTED_SCOPES_BY_EMAIL[f"{COTUTOR}@nyjc.edu.sg"] = LINK_NO_MIRROR
GRANTED_SCOPES_BY_EMAIL[f"{STRANGER}@nyjc.edu.sg"] = LINK

# --- app + login seam ---------------------------------------------------------

def _fake_push_context(*_args):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context

CURRENT_EMAIL: str | None = None

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


def client_for(user_id):
    client = app.test_client()
    seed_user(client, user_id)
    return client


def asks_for(email):
    return [scopes for (asked, scopes) in ASKED_SCOPES if asked == email]


def last_ask(email):
    asks = asks_for(email)
    return asks[-1] if asks else None


# --- scenarios ------------------------------------------------------------------

# 1. Anonymous visitor: sent to sign-in (unchanged behavior).
anon = app.test_client()
resp = anon.get("/a/a1")
check("anonymous /a/ redirects to sign-in",
      resp.status_code == 302 and "/sign-in" in resp.headers.get("Location", ""),
      f"status={resp.status_code} location={resp.headers.get('Location')}")

# 2. Owner preview: 200, no Classroom release at all.
owner = client_for(TEACHER)
resp = owner.get("/a/a1")
check("owner preview renders 200", resp.status_code == 200,
      f"status={resp.status_code}")
check("owner preview made no broker ask", last_ask(f"{TEACHER}@nyjc.edu.sg") is None,
      f"asks={asks_for(f'{TEACHER}@nyjc.edu.sg')}")

# 3. Pre-widen student: warm the MVP-only cache through the rule-4
# listing FIRST, then open /a/ — the gate must still demand the mirror
# scope (distinct cache keys), never silently satisfied by the MVP hit.
student = client_for(STUDENT_PRE)
resp = student.get("/api/v1/assignments")
check("rule-4 listing works on the pre-widen grant (MVP demand only)",
      resp.status_code == 200, f"status={resp.status_code}")
mvp_ask = last_ask(f"{STUDENT_PRE}@nyjc.edu.sg")
check("listing ask carried courses.readonly only",
      mvp_ask == (COURSES_SCOPE,), f"ask={mvp_ask}")

ASKED_SCOPES.clear()
resp = student.get("/a/a1")
body = resp.get_data(as_text=True)
if os.environ.get("GATE_DEBUG"):
    print("DEBUG /a/ status:", resp.status_code)
    print("DEBUG asks:", list(ASKED_SCOPES))
    print("DEBUG cache:", dict(enrollment._courses_cache))
    print("DEBUG body[:160]:", body[:160].replace("\n", " "))
check("pre-widen student gated on /a/ (403)", resp.status_code == 403,
      f"status={resp.status_code}")
check("gate renders the RECONNECT variant",
      "One more Google Classroom permission needed" in body,
      "reconnect heading missing from the body")
check("gate keeps the integrations CTA",
      "/profile/integrations" in body, "CTA url missing")
check("gate does NOT render the not-connected heading",
      "Connect your Google Classroom first" not in body,
      "wrong variant rendered")
ask = last_ask(f"{STUDENT_PRE}@nyjc.edu.sg")
check("/a/ release ask carried courses.readonly + coursework.me",
      ask == tuple(sorted((COURSES_SCOPE, MIRROR_SCOPE))), f"ask={ask}")

# 4. Scripted reconnect: the grant now carries coursework.me — the same
# client (same MVP cache entries) gets the answer sheet.
GRANTED_SCOPES_BY_EMAIL[f"{STUDENT_PRE}@nyjc.edu.sg"] = LINK
ASKED_SCOPES.clear()
resp = student.get("/a/a1")
body = resp.get_data(as_text=True)
check("post-reconnect student gets the answer sheet (200)",
      resp.status_code == 200, f"status={resp.status_code}")
check("answer sheet renders the assignment", "Algebra Worksheet" in body,
      "assignment title missing")
check("post-reconnect render made no CTA page",
      "One more Google Classroom permission needed" not in body, "")

# 5. Unconnected student: the ORIGINAL connect variant (not reconnect).
unconnected = client_for(STUDENT_UNCONNECTED)
resp = unconnected.get("/a/a1")
body = resp.get_data(as_text=True)
check("unconnected student gated (403)", resp.status_code == 403,
      f"status={resp.status_code}")
check("unconnected gate renders the original connect variant",
      "Connect your Google Classroom first" in body
      and "One more Google Classroom permission needed" not in body,
      "wrong variant rendered")

# 6. Co-tutor with the SAME pre-widen grant: staff demand stays
# MVP-only — NOT gated, no mirror ask.
cotutor = client_for(COTUTOR)
ASKED_SCOPES.clear()
resp = cotutor.get("/a/a1")
check("co-tutor preview NOT gated (200)", resp.status_code == 200,
      f"status={resp.status_code}")
mirror_asks = [scopes for scopes in asks_for(f"{COTUTOR}@nyjc.edu.sg")
               if MIRROR_SCOPE in scopes]
check("no co-tutor ask carried the mirror scope", mirror_asks == [],
      f"asks={asks_for(f'{COTUTOR}@nyjc.edu.sg')}")

# 7. Stranger (full grant, no membership): bare gate (unchanged).
stranger = client_for(STRANGER)
resp = stranger.get("/a/a1")
check("stranger gets the bare gate (403)", resp.status_code == 403,
      f"status={resp.status_code}")

# --- summary ---------------------------------------------------------------------

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("All checks passed.")
