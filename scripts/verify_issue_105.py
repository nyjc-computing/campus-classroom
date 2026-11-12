"""Verification for issue #105: staff dashboard (my assignments with
submission counts) + the per-assignment submissions drill-down.

No network and no real Campus/Google account are involved. The seams
are faked like verify_issue_28/98/104 do them:

1. The campus session: an in-memory FakeCampus (assignments + submissions
   + auth.broker) installed at both `app.campus` and the
   `campus_python.Campus` constructor (the #92 throwaway the broker
   release builds per release).
2. The Classroom REST API: `ClassroomClient.request` is replaced with a
   scripted fake serving v1/courses (the enrollment bridge + the course
   picker) and courses.courseWork.get (the dashboard's draft/assigned
   badge), recording nothing and failing on demand.

Covers, per the issue checklist:

1. Header nav (#105): Assignments + New assignment render for a signed-in
   user, and do not render (Sign In instead) for an anonymous visitor.
2. Dashboard page: the placeholder is gone — the page server-renders the
   signed-in shell and client-fetches rule-4 data (the fetch URLs,
   handleSessionExpiry wiring and drill-down links are in the HTML).
3. Rule-4 listing feeds the dashboard: own + assigned-to-my-courses,
   newest first; submission counts' input shape (n submitted / m total)
   via by-assignment (owner-only 404 for others preserved).
4. Coursework-state endpoint: DRAFT vs ASSIGNED relayed per link
   (Send-to-Classroom creates a DRAFT students cannot see until the
   teacher clicks Assign); a Classroom failure stays a clean 502 JSON,
   never a raw 500.
5. Drill-down ACL on /assignments/<id>/submissions: the creator gets the
   page (title + id server-rendered), everyone else 404s (existence not
   revealed), unknown ids 404, anonymous redirects to login. Rows link
   to the existing /submissions/<id> review page.
6. Regressions: the review page itself still answers for the owner, and
   an unlinked assignment lists zero submissions.

Usage: .venv/Scripts/python.exe scripts/verify_issue_105.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-105-secret"

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import (  # noqa: E402
    create_app,
)

TEACHER = "uid-user-teacher"
STUDENT = "uid-user-student"
STUDENT2 = "uid-user-student2"
OTHER = "uid-user-other"

# The full connected grant (all MVP scopes) — coursework.get needs
# classroom.course-work.readonly, the course picker courses.readonly.
GRANT_SCOPES = list(cauth.CLASSROOM_SCOPES_MVP)

# --- fake Classroom backend ---------------------------------------------------

COURSES_BY_EMAIL: dict[str, list[dict]] = {}
# (course_id, coursework_id) -> scripted CourseWork record (state DRAFT
# vs ASSIGNED is the dashboard badge's input).
COURSEWORK_BY_ID: dict[tuple[str, str], dict] = {}
# When set, every coursework GET raises through this (the "Classroom
# down" degradation case).
FAIL_COURSEWORK: Exception | None = None


def _fake_classroom_request(self, method, path, *, params=None, json=None):  # noqa: ARG001
    """ClassroomClient.request stand-in: courses + coursework reads."""
    self._access_token()  # mirror the real flow: release before the call
    global FAIL_COURSEWORK
    if path == "v1/courses":
        email = self._creds["email"]
        return {"courses": COURSES_BY_EMAIL.get(email, [])}
    if "/courseWork/" in path and method == "GET":
        # v1/courses/<course>/courseWork/<coursework>
        parts = path.split("/")
        record = COURSEWORK_BY_ID.get((parts[2], parts[4]))
        if FAIL_COURSEWORK is not None:
            raise FAIL_COURSEWORK
        if record is None:
            raise cauth.ClassroomAPIError(404, "not found")
        return dict(record)
    raise AssertionError(f"unexpected Classroom call {method} {path!r}")


cauth.ClassroomClient.request = _fake_classroom_request

# --- fake Campus backend ------------------------------------------------------

CURRENT_EMAIL: str | None = None


class FakeBroker:
    """auth.broker stand-in: releases the scripted per-user grant."""

    def token(self, provider, integration, *, min_scopes=None):  # noqa: ARG002
        assert (provider, integration) == ("google", "classroom")
        return {
            "provider": "google.classroom",
            "user_id": CURRENT_EMAIL,
            "access_token": "broker-access-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": " ".join(GRANT_SCOPES),
        }


class FakeAssignment:
    def __init__(self, id, created_by, title, created_at,
                 classroom_links=None, description=""):
        self.id = id
        self.created_by = created_by
        self.title = title
        self.description = description
        self.created_at = created_at
        self.questions = [
            {"id": "q1", "question": "Sample?", "type": "free-response"},
            {"id": "q1.a", "question": "Part a?", "type": "free-response"},
        ]
        self.classroom_links = classroom_links or []

    def to_resource(self):
        return {
            "id": self.id,
            "created_by": self.created_by,
            "title": self.title,
            "description": self.description,
            "created_at": self.created_at,
            "questions": self.questions,
            "classroom_links": [
                {
                    "course_id": link.course_id,
                    "coursework_id": link.coursework_id,
                    "attachment_id": link.attachment_id,
                }
                for link in self.classroom_links
            ],
        }


class FakeSubmission:
    def __init__(self, id, assignment_id, student_id, course_id="course-1",
                 submitted_at=None, responses=None, feedback=None):
        self.id = id
        self.assignment_id = assignment_id
        self.student_id = student_id
        self.course_id = course_id
        self.submitted_at = submitted_at
        self.responses = responses or []
        self.feedback = feedback or []
        self.updated_at = "2026-11-12T00:00:00Z"

    def to_resource(self):
        return {
            "id": self.id,
            "assignment_id": self.assignment_id,
            "student_id": self.student_id,
            "course_id": self.course_id,
            "responses": self.responses,
            "feedback": self.feedback,
            "submitted_at": self.submitted_at,
            "updated_at": self.updated_at,
        }


class FakeAssignments:
    def __init__(self, store):
        self._store = store

    def list(self):
        return list(self._store.values())

    def __getitem__(self, assignment_id):
        record = self._store.get(assignment_id)
        if record is None:
            raise RuntimeError("Assignment not found")
        return SimpleNamespace(get=lambda: record)


class FakeSubmissions:
    def __init__(self, store):
        self._store = store

    def by_assignment(self, assignment_id):
        return [
            s for s in self._store.values()
            if s.assignment_id == assignment_id
        ]

    def __getitem__(self, submission_id):
        store = self._store

        class _Handle:
            def get(self):
                item = store.get(submission_id)
                if item is None:
                    raise RuntimeError("Submission not found")
                return item

        return _Handle()


class FakeAPI:
    def __init__(self, assignments, submissions):
        self.assignments = FakeAssignments(assignments)
        self.submissions = FakeSubmissions(submissions)


class FakeCampus:
    """Stands in for campus_python.Campus; sessions yield self."""

    def __init__(self, assignments, submissions):
        self.api = FakeAPI(assignments, submissions)
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
    "a-assigned": FakeAssignment(
        "a-assigned", TEACHER, "History Quiz", "2026-11-11T00:00:00Z",
        classroom_links=[SimpleNamespace(
            course_id="course-1", coursework_id="cw-2", attachment_id=None)],
    ),
    "a-linked": FakeAssignment(
        "a-linked", TEACHER, "Algebra Worksheet", "2026-11-10T00:00:00Z",
        classroom_links=[SimpleNamespace(
            course_id="course-1", coursework_id="cw-1", attachment_id=None)],
        description="Quadratic equations practice.",
    ),
    "a-unlinked": FakeAssignment(
        "a-unlinked", TEACHER, "Untitled practice", "2026-11-09T00:00:00Z",
    ),
    "a-student": FakeAssignment(
        "a-student", STUDENT, "Student's own practice", "2026-11-08T00:00:00Z",
    ),
}

SUBMISSIONS = {
    "sub-1": FakeSubmission(
        "sub-1", "a-linked", STUDENT, submitted_at="2026-11-11T09:00:00Z",
        responses=[
            {"question_id": "q1", "response_text": "x = 2"},
            {"question_id": "q1.a", "response_text": "a = 1"},
        ],
        feedback=[{"question_id": "q1", "feedback_text": "Well done."}],
    ),
    "sub-2": FakeSubmission(
        "sub-2", "a-linked", STUDENT2,
        responses=[{"question_id": "q1", "response_text": "draft answer"}],
    ),
    "sub-3": FakeSubmission(
        "sub-3", "a-linked", "uid-user-student3",
        submitted_at="2026-11-11T10:30:00Z",
    ),
}

FAKE_CAMPUS = FakeCampus(ASSIGNMENTS, SUBMISSIONS)

# Scripted enrollments + the draft/assigned states of the linked posts.
for email_user in (TEACHER, STUDENT, STUDENT2):
    COURSES_BY_EMAIL[f"{email_user}@nyjc.edu.sg"] = [
        {"id": "course-1", "name": "17S1-CSC"}]
COURSES_BY_EMAIL[f"{OTHER}@nyjc.edu.sg"] = []
COURSEWORK_BY_ID[("course-1", "cw-1")] = {
    "id": "cw-1", "title": "Algebra Worksheet", "state": "DRAFT"}
COURSEWORK_BY_ID[("course-1", "cw-2")] = {
    "id": "cw-2", "title": "History Quiz", "state": "ASSIGNED"}


# --- app + login seam ---------------------------------------------------------

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


teacher_client = app.test_client()
seed_user(teacher_client, TEACHER)

student_client = app.test_client()
seed_user(student_client, STUDENT)

student2_client = app.test_client()
seed_user(student2_client, STUDENT2)

other_client = app.test_client()
seed_user(other_client, OTHER)

anonymous_client = app.test_client()


# --- 1. header nav ------------------------------------------------------------
print("== header nav (#105) ==")

r = teacher_client.get("/dashboard")
html = r.get_data(as_text=True)
check("signed-in pages render the nav links",
      r.status_code == 200
      and 'href="/assignments"' in html
      and ">Assignments</a>" in html
      and 'href="/assignments/new"' in html
      and ">New assignment</a>" in html,
      f"status={r.status_code}")

r = anonymous_client.get("/")
html = r.get_data(as_text=True)
check("anonymous visitors get Sign In, no nav links",
      r.status_code == 200
      and ">Sign In<" in html
      and ">New assignment<" not in html
      and ">Assignments</a>" not in html,
      f"status={r.status_code}")

r = student_client.get("/assignments")
html = r.get_data(as_text=True)
check("nav renders on a sibling page too (base.html)",
      r.status_code == 200 and ">New assignment</a>" in html)


# --- 2. dashboard page ----------------------------------------------------------
print("== dashboard: placeholder replaced ==")

r = teacher_client.get("/dashboard")
html = r.get_data(as_text=True)
check("the Under Construction placeholder is gone",
      "Under Construction" not in html)
check("the page carries the my-assignments shell",
      "My assignments" in html)
check("the page client-fetches the rule-4 listing",
      "fetchJson('/api/v1/assignments')" in html)
check("the page client-fetches per-assignment submission counts (N+1 v1)",
      "/api/v1/submissions/by-assignment/" in html)
check("the page client-fetches coursework state per link",
      "/api/v1/classroom/coursework/" in html)
check("every new fetch answers to handleSessionExpiry (#98/#103)",
      html.count("handleSessionExpiry") >= 2)
check("dashboard rows link the submissions drill-down",
      "/assignments/${encodeURIComponent(a.id)}/submissions" in html)


# --- 3. rule-4 listing + counts data --------------------------------------------
print("== rule-4 listing feeds the dashboard ==")

r = teacher_client.get("/api/v1/assignments")
assignments = r.get_json(silent=True) or []
check("teacher sees their own assignments, newest first",
      r.status_code == 200
      and [a["id"] for a in assignments]
      == ["a-assigned", "a-linked", "a-unlinked"],
      f"status={r.status_code} ids={[a['id'] for a in assignments]}")

r = student_client.get("/api/v1/assignments")
assignments = r.get_json(silent=True) or []
check("student sees own + assigned-to-their-course",
      r.status_code == 200
      and {a["id"] for a in assignments} == {"a-linked", "a-assigned", "a-student"},
      f"status={r.status_code} ids={[a['id'] for a in assignments]}")

r = other_client.get("/api/v1/assignments")
check("a user with no courses and no assignments sees none",
      r.status_code == 200 and r.get_json(silent=True) == [],
      f"status={r.status_code} body={r.get_data(as_text=True)[:120]!r}")

r = teacher_client.get("/api/v1/submissions/by-assignment/a-linked")
submissions = r.get_json(silent=True) or []
check("by-assignment feeds the n/m counts (2 of 3 submitted)",
      r.status_code == 200
      and len(submissions) == 3
      and sum(1 for s in submissions if s["submitted_at"]) == 2,
      f"status={r.status_code} n={len(submissions)}")

r = student_client.get("/api/v1/submissions/by-assignment/a-linked")
check("by-assignment stays owner-only (enrolled student 404s)",
      r.status_code == 404, f"status={r.status_code}")

r = teacher_client.get("/api/v1/submissions/by-assignment/a-unlinked")
check("an unlinked assignment lists zero submissions",
      r.status_code == 200 and r.get_json(silent=True) == [],
      f"status={r.status_code}")


# --- 4. coursework-state endpoint ------------------------------------------------
print("== coursework state (draft/assigned badge) ==")

r = teacher_client.get("/api/v1/classroom/coursework/course-1/cw-1")
body = r.get_json(silent=True) or {}
check("a DRAFT post relays state DRAFT",
      r.status_code == 200 and body.get("state") == "DRAFT"
      and body.get("id") == "cw-1",
      f"status={r.status_code} body={str(body)[:120]}")

r = teacher_client.get("/api/v1/classroom/coursework/course-1/cw-2")
check("an assigned post relays state ASSIGNED",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("state") == "ASSIGNED",
      f"status={r.status_code}")

r = teacher_client.get("/api/v1/classroom/coursework/course-1/cw-x")
body = r.get_json(silent=True) or {}
check("an unknown coursework is a clean upstream 502 JSON "
      "(the ClassroomAuthError contract), never a raw 500",
      r.status_code == 502
      and body.get("error", {}).get("code") == "classroom_api_error",
      f"status={r.status_code} body={str(body)[:120]}")

global_fail_state = FAIL_COURSEWORK
FAIL_COURSEWORK = cauth.ClassroomAPIError(503, "backend error")
r = teacher_client.get("/api/v1/classroom/coursework/course-1/cw-1")
body = r.get_json(silent=True) or {}
check("a Classroom failure is a clean 502 JSON, never a raw 500",
      r.status_code == 502
      and body.get("error", {}).get("code") == "classroom_api_error",
      f"status={r.status_code} body={str(body)[:120]}")
FAIL_COURSEWORK = global_fail_state

r = anonymous_client.get("/api/v1/classroom/coursework/course-1/cw-1")
check("anonymous callers cannot probe coursework state",
      r.status_code == 302, f"status={r.status_code}")


# --- 5. drill-down page ACL -------------------------------------------------------
print("== drill-down ACL on /assignments/<id>/submissions ==")

r = teacher_client.get("/assignments/a-linked/submissions")
html = r.get_data(as_text=True)
check("the assignment's creator gets the page",
      r.status_code == 200, f"status={r.status_code}")
check("the page server-renders the assignment title",
      "Algebra Worksheet" in html)
check("the page pins the assignment id for the client fetch",
      'const assignmentId = "a-linked"' in html)
check("the page client-fetches the owner-only submissions API",
      "/api/v1/submissions/by-assignment/" in html
      and "handleSessionExpiry" in html)
check("rows link the existing review page",
      "/submissions/${encodeURIComponent(submission.id)}" in html)

for label, client in (("an enrolled student", student_client),
                      ("another student", student2_client),
                      ("an unrelated user", other_client)):
    r = client.get("/assignments/a-linked/submissions")
    check(f"{label} (non-owner) -> 404 (existence not revealed)",
          r.status_code == 404, f"status={r.status_code}")

r = student_client.get("/assignments/a-student/submissions")
check("a student drilling into their OWN assignment gets the page",
      r.status_code == 200, f"status={r.status_code}")

r = teacher_client.get("/assignments/does-not-exist/submissions")
check("unknown assignment id -> 404", r.status_code == 404,
      f"status={r.status_code}")

r = anonymous_client.get("/assignments/a-linked/submissions")
loc = r.headers.get("Location", "")
check("anonymous visitors are sent to sign in",
      r.status_code == 302 and "/login" in loc,
      f"status={r.status_code} location={loc!r}")


# --- 6. regressions -----------------------------------------------------------
print("== existing surfaces untouched ==")

r = teacher_client.get("/submissions/sub-1")
check("the review page still renders for the owner (drill-down target)",
      r.status_code == 200, f"status={r.status_code}")

r = teacher_client.get("/submissions/sub-2")
check("the review page still answers for the assignment owner",
      r.status_code == 200, f"status={r.status_code}")

r = student_client.get("/submissions/sub-1")
check("the owning student's review view is unchanged (read-only)",
      r.status_code == 200 and "const readOnly = true" in r.get_data(as_text=True))

r = teacher_client.get("/assignments")
check("the assignments list page still renders",
      r.status_code == 200, f"status={r.status_code}")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("All checks passed.")
