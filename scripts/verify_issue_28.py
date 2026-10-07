"""Verification for issue #28 (route-layer authorization, PRD v1.3 rules
enforced in campus-classroom's own routes).

Covers the ownership/visibility slice (PR #29) and the enrollment slice
(this change — the Classroom bridge is faked at its two seams, the
campus.auth broker release and the Classroom REST `courses.list`, so no
network and no real Google account are involved):

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
4. Enrollment gate (PRD v1.3 rules 2+3):
   /a/{id} renders for a signed-in student enrolled in a linked course;
   an unenrolled student still gets the 403 gate; a signed-in user
   without a google.classroom connection gets the connect prompt
   (403 + CTA, no content).
   POST /api/v1/submissions requires enrollment (unknown assignment →
   404, unenrolled → 403 not_enrolled, disconnected → classroom
   not-connected 403 JSON); submission mutations (responses, submit,
   unsubmit, update, delete) require it too — an enrollment revoked
   after creation closes the attempt. Reads are unchanged.

Assigned-only student listing (rule 4) stays with #14 (Student View).

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
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
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
        self.submitted_at = None

    def to_resource(self):
        return {
            "id": self.id, "assignment_id": self.assignment_id,
            "student_id": self.student_id, "course_id": self.course_id,
            "responses": self.responses,
            "submitted_at": self.submitted_at,
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

        class _Responses:
            def add(self, question_id, response_text):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                item.responses.append({
                    "question_id": question_id,
                    "response_text": response_text,
                })

        class _Handle:
            def get(self):
                calls.append(("submissions.get", submission_id))
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                return item

            @property
            def responses(self):
                return _Responses()

            def update(self, **updates):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                for key, value in updates.items():
                    setattr(item, key, value)

            def submit(self):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                item.submitted_at = "2026-10-07T00:00:00Z"

            def unsubmit(self):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                item.submitted_at = None

            def delete(self):
                if submission_id not in store:
                    raise _not_found("Submission")
                del store[submission_id]

        return _Handle()

    def new(self, assignment_id, student_id, course_id, responses=None):
        self._calls.append(("submissions.new", {
            "assignment_id": assignment_id, "student_id": student_id,
        }))
        submission_id = f"s-new-{len(self._store) + 1}"
        item = FakeSubmission(submission_id, assignment_id, student_id, course_id)
        if responses:
            item.responses = responses
        self._store[submission_id] = item
        return item

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


# --- Fake auth bridge seams -------------------------------------------------
# The enrollment check talks to two seams: campus.auth's broker release
# (campus.with_user_session() → auth.broker.token) and the Classroom REST
# courses.list (ClassroomClient.request). Both are faked here — keyed by
# the signed-in user's email, which the fake user session publishes.

CURRENT_EMAIL: str | None = None
BROKER_CONNECTED_BY_EMAIL: dict[str, bool] = {}
COURSES_BY_EMAIL: dict[str, list[dict]] = {}


class FakeBroker:
    """auth.broker stand-in: releases a courses.readonly-scoped token for
    the signed-in user, or 404s (NotFoundError, the class the client
    library raises) when that user is scripted as disconnected."""

    def token(self, provider, integration, *, min_scopes=None):
        assert (provider, integration) == ("google", "classroom")
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
            "scope": " ".join([
                "https://www.googleapis.com/auth/classroom.courses.readonly",
            ]),
        }


def _fake_classroom_request(self, method, path, *, params=None, json=None):
    """ClassroomClient.request stand-in: courses.list per signed-in user."""
    self._access_token()  # mirror the real flow: release before the call
    assert path == "v1/courses", f"unexpected Classroom path {path!r}"
    return {"courses": COURSES_BY_EMAIL.get(self._creds["email"], [])}


class FakeCampus:
    """Stands in for campus_python.Campus; both session flavors yield self."""

    def __init__(self, assignments, submissions, calls):
        self.api = FakeAPI(
            FakeAssignments(assignments, calls),
            FakeSubmissions(submissions, calls),
        )
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
    "a1": FakeAssignment("a1", TEACHER, "Teacher One Assignment"),
    "a2": FakeAssignment("a2", OTHER_TEACHER, "Teacher Two Assignment"),
    "a3": FakeAssignment("a3", TEACHER, "Unlinked Assignment"),
}
SUBMISSIONS = {
    "s1": FakeSubmission("s1", "a1", STUDENT),
    "s2": FakeSubmission("s2", "a2", STUDENT),
    "s3": FakeSubmission("s3", "a1", STUDENT2),
}
CALLS = []
FAKE_CAMPUS = FakeCampus(ASSIGNMENTS, SUBMISSIONS, CALLS)

# Classroom bridge scripting: who is connected, and whose courses.list
# returns what. (STUDENT is in course-1, STUDENT2 only in course-2.)
cauth.ClassroomClient.request = _fake_classroom_request


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
      and [a["id"] for a in r.get_json()] == ["a1", "a3"],
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

# --- 4. Enrollment gate (PRD v1.3 rules 2+3) -------------------------------
# Script the Classroom bridge: STUDENT is in course-1, STUDENT2 only in
# course-2, the teachers in neither (owner checks short-circuit before
# the bridge anyway). Then link a1 to course-1.
STAFF_EMAIL = f"{TEACHER}@nyjc.edu.sg"
OTHER_EMAIL = f"{OTHER_TEACHER}@nyjc.edu.sg"
STUDENT_EMAIL = f"{STUDENT}@nyjc.edu.sg"
STUDENT2_EMAIL = f"{STUDENT2}@nyjc.edu.sg"
COURSES_BY_EMAIL.update({
    STUDENT_EMAIL: [{"id": "course-1", "name": "CampusClass"}],
    STUDENT2_EMAIL: [{"id": "course-2", "name": "Other Class"}],
})
ASSIGNMENTS["a1"].classroom_links = [
    SimpleNamespace(course_id="course-1", coursework_id="cw-1")
]

r = student_client.get("/a/a1")
check("enrolled student /a/ renders content",
      r.status_code == 200 and b"Teacher One Assignment" in r.data,
      f"status={r.status_code}")

r = student2_client = app.test_client()
seed_user(student2_client, STUDENT2)
r = student2_client.get("/a/a1")
check("unenrolled student /a/ still gets the 403 gate page",
      r.status_code == 403, f"status={r.status_code}")
check("gate page leaks no assignment content (unenrolled)",
      b"Teacher One Assignment" not in r.data)

BROKER_CONNECTED_BY_EMAIL[OTHER_EMAIL] = False
r = teacher2_client.get("/a/a1")
check("signed-in user without a google.classroom connection gets the "
      "connect prompt (403)",
      r.status_code == 403 and b"Connect" in r.data, f"status={r.status_code}")
check("connect prompt leaks no assignment content",
      b"Teacher One Assignment" not in r.data)
del BROKER_CONNECTED_BY_EMAIL[OTHER_EMAIL]

# Rule 3: attempt/submit. Create.
r = student_client.post("/api/v1/submissions", json={
    "assignment_id": "a1", "course_id": "course-1",
})
check("enrolled student creates a submission (201)",
      r.status_code == 201 and r.get_json()["assignment_id"] == "a1",
      f"status={r.status_code} body={r.get_json()}")
new_submission_id = (r.get_json() or {}).get("id")

r = student_client.post("/api/v1/submissions", json={
    "assignment_id": "missing", "course_id": "course-1",
})
check("creating a submission for an unknown assignment answers 404",
      r.status_code == 404, f"status={r.status_code}")

r = student2_client.post("/api/v1/submissions", json={
    "assignment_id": "a1", "course_id": "course-2",
})
check("unenrolled student's attempt answers 403 not_enrolled",
      r.status_code == 403
      and r.get_json().get("code") == "not_enrolled",
      f"status={r.status_code} body={r.get_json()}")

r = student2_client.post("/api/v1/submissions", json={
    "assignment_id": "a3", "course_id": "course-2",
})
check("attempt on an assignment with no classroom_links answers 403",
      r.status_code == 403
      and r.get_json().get("code") == "not_enrolled",
      f"status={r.status_code} body={r.get_json()}")

BROKER_CONNECTED_BY_EMAIL[STUDENT2_EMAIL] = False
r = student2_client.post("/api/v1/submissions", json={
    "assignment_id": "a1", "course_id": "course-2",
})
check("attempt without a google.classroom connection answers the "
      "not-connected 403 JSON",
      r.status_code == 403
      and r.get_json().get("error", {}).get("code") == "classroom_not_connected",
      f"status={r.status_code} body={r.get_json()}")
del BROKER_CONNECTED_BY_EMAIL[STUDENT2_EMAIL]

# Rule 3: mutations on an existing submission.
r = student_client.post(f"/api/v1/submissions/{new_submission_id}/responses",
                        json={"question_id": "q1", "response_text": "answer"})
check("enrolled student adds a response (200)",
      r.status_code == 200, f"status={r.status_code} body={r.get_json()}")

r = student_client.post(f"/api/v1/submissions/{new_submission_id}/submit")
check("enrolled student submits (200)",
      r.status_code == 200, f"status={r.status_code} body={r.get_json()}")

r = student_client.patch(f"/api/v1/submissions/{new_submission_id}",
                         json={"responses": [{"question_id": "q1",
                                              "response_text": "revised"}]})
check("enrolled student updates their submission (200)",
      r.status_code == 200, f"status={r.status_code} body={r.get_json()}")

r = student_client.post(f"/api/v1/submissions/{new_submission_id}/unsubmit")
check("enrolled student unsubmits (200)",
      r.status_code == 200, f"status={r.status_code} body={r.get_json()}")

# s3 is STUDENT2's own submission on a1 — but STUDENT2 is only in
# course-2, so a revoked/absent enrollment closes the attempt too.
r = student2_client.post("/api/v1/submissions/s3/responses",
                         json={"question_id": "q1", "response_text": "x"})
check("unenrolled student cannot add responses (403 not_enrolled)",
      r.status_code == 403
      and r.get_json().get("code") == "not_enrolled",
      f"status={r.status_code} body={r.get_json()}")

r = student2_client.post("/api/v1/submissions/s3/submit")
check("unenrolled student cannot submit (403 not_enrolled)",
      r.status_code == 403
      and r.get_json().get("code") == "not_enrolled",
      f"status={r.status_code} body={r.get_json()}")

r = student2_client.delete("/api/v1/submissions/s3")
check("unenrolled student cannot delete their submission (403 not_enrolled)",
      r.status_code == 403
      and r.get_json().get("code") == "not_enrolled",
      f"status={r.status_code} body={r.get_json()}")

r = student2_client.patch("/api/v1/submissions/s3",
                          json={"responses": []})
check("unenrolled student cannot update their submission (403 not_enrolled)",
      r.status_code == 403
      and r.get_json().get("code") == "not_enrolled",
      f"status={r.status_code} body={r.get_json()}")

# Reads stay open to the submission's student regardless of enrollment.
r = student_client.get("/api/v1/submissions/s2")
check("student still reads their own submission on an unlinked assignment",
      r.status_code == 200 and r.get_json()["id"] == "s2",
      f"status={r.status_code}")


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
