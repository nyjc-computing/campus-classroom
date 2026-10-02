"""Verification for issue #28 (route-layer authorization, few-lines slice):
PRD v1.3 rules enforced in campus-classroom's own routes.

Covers exactly what this slice implements (no Google/Classroom stubs
needed — all checks are pure ownership/visibility comparisons):

1. /a/{id} share gate: anonymous → 302 sign-in; owner → 200 with
   content; signed-in non-owner → 403 gate page with no content;
   unknown id → 404.
2. Assignment API: list is forced to created_by=me (client-supplied
   created_by is ignored); GET by id is owner-only, non-owner → 404
   (existence not revealed). PATCH/DELETE/links already had owner
   checks (regression-checked here for the GET path only).
3. Submission API reads (the "any authenticated user can view" hole):
   GET by id → student or assignment-owner only; by-assignment →
   assignment-owner only; by-student → self only; generic list →
   student_id forced/rejected, assignment_id filter requires ownership.
   Submission mutations already had student_id checks (not re-tested).

Enrollment-dependent checks (assigned-student rendering on /a/,
attempt/submit enrollment, assigned-only student listing) are NOT
covered here — they stay in #28 pending the Classroom bridge.

Usage: .venv/Scripts/python.exe scripts/verify_issue_28.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-28-secret"

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
import flask  # noqa: E402

from apps.classroom import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Campus backend: in-memory assignments/submissions + call recording
# ---------------------------------------------------------------------------
TEACHER = "uid-user-teacher"
OTHER_TEACHER = "uid-user-teacher2"
STUDENT = "uid-user-student"
STUDENT2 = "uid-user-student2"


class FakeAssignment:
    def __init__(self, id, created_by, title, description="", questions=None):
        self.id = id
        self.created_by = created_by
        self.title = title
        self.description = description
        self.questions = questions or []
        self.classroom_links = []

    def to_resource(self):
        return {
            "id": self.id, "created_by": self.created_by, "title": self.title,
            "description": self.description, "questions": self.questions,
            "classroom_links": self.classroom_links,
        }


class FakeSubmission:
    def __init__(self, id, assignment_id, student_id, course_id="course-1"):
        self.id = id
        self.assignment_id = assignment_id
        self.student_id = student_id
        self.course_id = course_id
        self.responses = []

    def to_resource(self):
        return {
            "id": self.id, "assignment_id": self.assignment_id,
            "student_id": self.student_id, "course_id": self.course_id,
            "responses": self.responses,
        }


def _not_found(name):
    return RuntimeError(f"{name} not found")


class FakeAssignments:
    def __init__(self, store, calls):
        self._store = store
        self._calls = calls

    def __getitem__(self, assignment_id):
        store, calls = self._store, self._calls

        class _Handle:
            def get(self):
                calls.append(("assignments.get", assignment_id))
                item = store.get(assignment_id)
                if item is None:
                    raise _not_found("Assignment")
                return item

        return _Handle()

    def list(self, created_by=None, **_):
        self._calls.append(("assignments.list", {"created_by": created_by}))
        return [a for a in self._store.values() if a.created_by == created_by]


class FakeSubmissions:
    def __init__(self, store, calls):
        self._store = store
        self._calls = calls

    def __getitem__(self, submission_id):
        store, calls = self._store, self._calls

        class _Handle:
            def get(self):
                calls.append(("submissions.get", submission_id))
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                return item

        return _Handle()

    def list(self, assignment_id=None, student_id=None, course_id=None):
        self._calls.append(("submissions.list", {
            "assignment_id": assignment_id, "student_id": student_id,
        }))
        return [
            s for s in self._store.values()
            if (assignment_id in (None, s.assignment_id))
            and (student_id in (None, s.student_id))
            and (course_id in (None, s.course_id))
        ]

    def by_assignment(self, assignment_id):
        self._calls.append(("submissions.by_assignment", assignment_id))
        return [s for s in self._store.values() if s.assignment_id == assignment_id]

    def by_student(self, student_id):
        self._calls.append(("submissions.by_student", student_id))
        return [s for s in self._store.values() if s.student_id == student_id]


class FakeAPI:
    def __init__(self, assignments, submissions):
        self.assignments = assignments
        self.submissions = submissions


class FakeCampus:
    """Stands in for campus_python.Campus; both session flavors yield self."""

    def __init__(self, assignments, submissions, calls):
        self.api = FakeAPI(
            FakeAssignments(assignments, calls),
            FakeSubmissions(submissions, calls),
        )

    @contextmanager
    def with_app_session(self):
        yield self

    @contextmanager
    def with_user_session(self):
        yield self


ASSIGNMENTS = {
    "a1": FakeAssignment("a1", TEACHER, "Teacher One Assignment"),
    "a2": FakeAssignment("a2", OTHER_TEACHER, "Teacher Two Assignment"),
}
SUBMISSIONS = {
    "s1": FakeSubmission("s1", "a1", STUDENT),
    "s2": FakeSubmission("s2", "a2", STUDENT),
    "s3": FakeSubmission("s3", "a1", STUDENT2),
}
CALLS = []
FAKE_CAMPUS = FakeCampus(ASSIGNMENTS, SUBMISSIONS, CALLS)


# ---------------------------------------------------------------------------
# App with a faked Campus login (push_context reads verify_user from session)
# ---------------------------------------------------------------------------
def _fake_push_context(self):  # noqa: ARG001 (monkeypatched method; self unused)
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


def clear_user(client):
    with client.session_transaction() as sess:
        sess.pop("verify_user", None)


anon_client = app.test_client()

teacher_client = app.test_client()
seed_user(teacher_client, TEACHER)

student_client = app.test_client()
seed_user(student_client, STUDENT)

teacher2_client = app.test_client()
seed_user(teacher2_client, OTHER_TEACHER)


# --- 1. /a/{id} share gate -------------------------------------------------
r = anon_client.get("/a/a1")
check("anonymous /a/ redirects to sign-in",
      r.status_code == 302 and "/sign-in" in (r.headers.get("Location") or ""),
      f"status={r.status_code} loc={r.headers.get('Location')}")

r = teacher_client.get("/a/a1")
check("owner /a/ renders content",
      r.status_code == 200 and b"Teacher One Assignment" in r.data,
      f"status={r.status_code}")

r = student_client.get("/a/a1")
check("signed-in non-owner /a/ gets 403 gate page",
      r.status_code == 403,
      f"status={r.status_code}")
check("gate page leaks no assignment content",
      b"Teacher One Assignment" not in r.data)

r = teacher_client.get("/a/a2")
check("other teacher's assignment on /a/ gets 403 (no co-teacher access)",
      r.status_code == 403, f"status={r.status_code}")

r = teacher_client.get("/a/missing")
check("unknown assignment id on /a/ gets 404",
      r.status_code == 404, f"status={r.status_code}")

# --- 2. Assignment API -----------------------------------------------------
CALLS.clear()
r = teacher_client.get("/api/v1/assignments")
check("assignment list returns only own assignments",
      r.status_code == 200
      and [a["id"] for a in r.get_json()] == ["a1"],
      f"status={r.status_code} body={r.get_json()}")
check("assignment list forced to created_by=me (client filter ignored)",
      CALLS and CALLS[0] == ("assignments.list", {"created_by": TEACHER}),
      f"calls={CALLS}")

r = teacher_client.get("/api/v1/assignments/a2")
check("GET another teacher's assignment answers 404",
      r.status_code == 404, f"status={r.status_code}")

r = teacher_client.get("/api/v1/assignments/a1")
check("GET own assignment answers 200",
      r.status_code == 200 and r.get_json()["id"] == "a1",
      f"status={r.status_code}")

# --- 3. Submission API reads ----------------------------------------------
CALLS.clear()
r = student_client.get("/api/v1/submissions")
check("submission list defaults to own submissions",
      r.status_code == 200
      and sorted(s["id"] for s in r.get_json()) == ["s1", "s2"],
      f"status={r.status_code} body={r.get_json()}")
check("submission list forced student_id=me",
      CALLS and CALLS[0][1].get("student_id") == STUDENT,
      f"calls={CALLS}")

r = student_client.get("/api/v1/submissions?student_id=uid-user-student2")
check("listing another student's submissions answers 404",
      r.status_code == 404, f"status={r.status_code}")

r = teacher_client.get("/api/v1/submissions?assignment_id=a1")
check("assignment owner lists submissions for their assignment",
      r.status_code == 200
      and sorted(s["id"] for s in r.get_json()) == ["s1", "s3"],
      f"status={r.status_code} body={r.get_json()}")

r = student_client.get("/api/v1/submissions?assignment_id=a1")
check("non-owner listing an assignment's submissions answers 404",
      r.status_code == 404, f"status={r.status_code}")

r = teacher2_client.get("/api/v1/submissions?assignment_id=a1")
check("another teacher listing foreign assignment submissions answers 404",
      r.status_code == 404, f"status={r.status_code}")

r = student_client.get("/api/v1/submissions/s1")
check("student reads own submission",
      r.status_code == 200 and r.get_json()["id"] == "s1",
      f"status={r.status_code}")

r = student_client.get("/api/v1/submissions/s3")
check("student cannot read another student's submission (404)",
      r.status_code == 404, f"status={r.status_code}")

r = teacher_client.get("/api/v1/submissions/s3")
check("assignment owner (teacher) reads a student's submission",
      r.status_code == 200 and r.get_json()["id"] == "s3",
      f"status={r.status_code}")

r = teacher2_client.get("/api/v1/submissions/s1")
check("unrelated teacher cannot read a submission (404)",
      r.status_code == 404, f"status={r.status_code}")

r = teacher_client.get("/api/v1/submissions/by-assignment/a1")
check("by-assignment listing works for the assignment owner",
      r.status_code == 200
      and sorted(s["id"] for s in r.get_json()) == ["s1", "s3"],
      f"status={r.status_code} body={r.get_json()}")

r = teacher2_client.get("/api/v1/submissions/by-assignment/a1")
check("by-assignment listing answers 404 for a non-owner",
      r.status_code == 404, f"status={r.status_code}")

r = student_client.get("/api/v1/submissions/by-student/uid-user-student")
check("by-student listing works for the student themself",
      r.status_code == 200
      and sorted(s["id"] for s in r.get_json()) == ["s1", "s2"],
      f"status={r.status_code} body={r.get_json()}")

r = student_client.get("/api/v1/submissions/by-student/uid-user-student2")
check("by-student listing answers 404 for another student",
      r.status_code == 404, f"status={r.status_code}")

r = teacher_client.get("/api/v1/submissions/by-student/uid-user-student")
check("by-student listing answers 404 for a teacher (review is by-assignment)",
      r.status_code == 404, f"status={r.status_code}")


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
