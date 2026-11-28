"""Verify the issue #63 fix: students can no longer write teacher feedback.

Two halves (the issue's findings):

1. PATCH /api/v1/submissions/<id> applied a `feedback` key verbatim
   after only checking that the caller IS the submission's student — so
   the student could fabricate or overwrite the feedback that later
   renders to the teacher as teacher-authored. Fix: the key is rejected
   with 403 `feedback_forbidden` and the WHOLE request fails (a mixed
   responses+feedback payload must not half-apply); responses and
   submit/unsubmit remain the student's to drive.
2. POST /api/v1/submissions/<id>/feedback was open to ANY authenticated
   user (the issue's second finding); reviewer-gated since #130 via
   enrollment.can_review. This script pins that gate: the submission's
   student and a same-class student both 404 (existence hidden), while
   the creator and a co-tutor land their feedback.

The Classroom bridge is faked at its two seams (campus.auth broker
release, Classroom REST courses.list honoring teacherId=me) so no
network and no real Google account are involved — CI-safe, in the
verify_issue_130 style.

Usage: python scripts/verify_issue_63.py
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-63-secret"
os.environ["CLIENT_ID"] = "verify-issue-63"
os.environ["CLIENT_SECRET"] = "verify-issue-63"

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Campus backend: in-memory assignments/submissions + call recording
# ---------------------------------------------------------------------------
TEACHER = "uid-user-teacher"      # a1's creator, teaches course-1
COTUTOR = "uid-user-cotutor"      # teaches course-1, did not create a1
STUDENT = "uid-user-student"      # in course-1, owns the submission
CLASSMATE = "uid-user-classmate"  # in course-1 as a student (not a reviewer)

QUESTIONS = [
    {"id": "q1", "prompt": None, "question": "Root question"},
    {"id": "q2", "prompt": None, "question": "Second root"},
]


class FakeAssignment:
    def __init__(self, id, created_by, title, questions=None):
        self.id = id
        self.created_by = created_by
        self.title = title
        self.questions = questions or []
        self.classroom_links = []
        self.created_at = "2026-11-20T00:00:00Z"

    def to_resource(self):
        return {
            "id": self.id, "created_by": self.created_by, "title": self.title,
            "description": None, "questions": self.questions,
            "classroom_links": [], "created_at": self.created_at,
        }


class FakeSubmission:
    def __init__(self, id, assignment_id, student_id, course_id="course-1"):
        self.id = id
        self.assignment_id = assignment_id
        self.student_id = student_id
        self.course_id = course_id
        self.responses = []
        self.feedback = []
        self.submitted_at = None

    def to_resource(self):
        return {
            "id": self.id, "assignment_id": self.assignment_id,
            "student_id": self.student_id, "course_id": self.course_id,
            "responses": self.responses, "feedback": self.feedback,
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


class FakeSubmissions:
    def __init__(self, store, calls):
        self._store = store
        self._calls = calls

    def __getitem__(self, submission_id):
        store, calls = self._store, self._calls

        class _Feedback:
            def add(self, question_id, feedback_text):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                item.feedback = [f for f in item.feedback
                                 if f["question_id"] != question_id]
                item.feedback.append({
                    "question_id": question_id,
                    "feedback_text": feedback_text,
                })

        class _Handle:
            def get(self):
                calls.append(("submissions.get", submission_id))
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                return item

            @property
            def feedback(self):
                return _Feedback()

            def update(self, **updates):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                for key, value in updates.items():
                    setattr(item, key, value)

            def unsubmit(self):
                item = store.get(submission_id)
                if item is None:
                    raise _not_found("Submission")
                item.submitted_at = None

        return _Handle()

    def new(self, assignment_id, student_id, course_id, responses=None):
        self._calls.append(("submissions.new", {
            "assignment_id": assignment_id, "student_id": student_id,
            "course_id": course_id,
        }))
        submission_id = f"s-new-{len(self._store) + 1}"
        item = FakeSubmission(submission_id, assignment_id, student_id,
                              course_id)
        if responses:
            item.responses = responses
        self._store[submission_id] = item
        return item


class FakeAPI:
    def __init__(self, assignments, submissions):
        self.assignments = assignments
        self.submissions = submissions


# --- Fake auth bridge seams ---------------------------------------------------
# Keyed by the signed-in user's email, which the fake user session publishes.
# Each course carries the signer's role in it; courses_list honors
# teacherId=me (the taught_courses demand behind can_review).

CURRENT_EMAIL: str | None = None
COURSES_BY_EMAIL: dict[str, list[dict]] = {}


class FakeBroker:
    """auth.broker stand-in: releases a courses.readonly-scoped token."""

    def token(self, provider, integration, *, min_scopes=None):  # noqa: ARG002
        assert (provider, integration) == ("google", "classroom")
        email = CURRENT_EMAIL
        if email not in COURSES_BY_EMAIL:
            raise campus_errors.NotFoundError(
                404,
                f"No google.classroom credential for user {email}",
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


def _fake_classroom_request(self, method, path, *, params=None, json=None):  # noqa: ARG001
    """ClassroomClient.request stand-in: courses.list, honoring teacherId=me."""
    self._access_token()  # mirror the real flow: release before the call
    assert path == "v1/courses", f"unexpected Classroom path {path!r}"
    email = self._creds["email"]
    courses = COURSES_BY_EMAIL.get(email, [])
    if (params or {}).get("teacherId") == "me":
        courses = [c for c in courses if c.get("role") == "TEACHER"]
    return {"courses": [
        {k: v for k, v in c.items() if k != "role"} for c in courses
    ]}


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
    "a1": FakeAssignment("a1", TEACHER, "Algebra Worksheet",
                         questions=QUESTIONS),
}
SUBMISSIONS = {}
CALLS: list = []
FAKE_CAMPUS = FakeCampus(ASSIGNMENTS, SUBMISSIONS, CALLS)

cauth.ClassroomClient.request = _fake_classroom_request

TEACHER_EMAIL = f"{TEACHER}@nyjc.edu.sg"
COTUTOR_EMAIL = f"{COTUTOR}@nyjc.edu.sg"
STUDENT_EMAIL = f"{STUDENT}@nyjc.edu.sg"
CLASSMATE_EMAIL = f"{CLASSMATE}@nyjc.edu.sg"

# a1 is linked to course-1: STUDENT and CLASSMATE are students of it;
# TEACHER (its creator) and COTUTOR teach it.
COURSES_BY_EMAIL.update({
    STUDENT_EMAIL: [{"id": "course-1", "name": "CampusClass",
                     "role": "STUDENT"}],
    CLASSMATE_EMAIL: [{"id": "course-1", "name": "CampusClass",
                       "role": "STUDENT"}],
    TEACHER_EMAIL: [{"id": "course-1", "name": "CampusClass",
                     "role": "TEACHER"}],
    COTUTOR_EMAIL: [{"id": "course-1", "name": "CampusClass",
                     "role": "TEACHER"}],
})
ASSIGNMENTS["a1"].classroom_links = [
    SimpleNamespace(course_id="course-1", coursework_id="cw-1")
]


# --- App with a faked Campus login ---------------------------------------------
def _fake_push_context(*_args):  # noqa: ARG001 (context seam patch; args unused)
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context

app = create_app()
app.campus = FAKE_CAMPUS
# campus_session.user_session() runs on a throwaway Campus (#153) — the
# constructor is the session-side seam; hand it FAKE_CAMPUS too.
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


student_client = app.test_client()
seed_user(student_client, STUDENT)
_ajaxify(student_client)

classmate_client = app.test_client()
seed_user(classmate_client, CLASSMATE)
_ajaxify(classmate_client)

teacher_client = app.test_client()
seed_user(teacher_client, TEACHER)
_ajaxify(teacher_client)

cotutor_client = app.test_client()
seed_user(cotutor_client, COTUTOR)
_ajaxify(cotutor_client)

# --- Setup: the student attempts a1 and the teacher leaves feedback -----------
r = student_client.post("/api/v1/submissions", json={
    "assignment_id": "a1", "course_id": "course-1",
    "responses": [{"question_id": "q1", "response_text": "my answer"}],
})
check("student creates a submission on a1",
      r.status_code == 201 and r.get_json()["student_id"] == STUDENT,
      f"status={r.status_code} body={r.get_json()}")
submission_id = r.get_json()["id"]

r = teacher_client.post(f"/api/v1/submissions/{submission_id}/feedback", json={
    "question_id": "q1", "feedback_text": "Watch the sign in step 2.",
})
check("creator feedback lands via POST .../feedback (reviewer path)",
      r.status_code == 200
      and r.get_json()["feedback"][0]["feedback_text"]
      == "Watch the sign in step 2.",
      f"status={r.status_code} body={r.get_json()}")

# --- 1. The PATCH hole (#63) is closed -----------------------------------------
r = student_client.patch(f"/api/v1/submissions/{submission_id}", json={
    "feedback": [{"question_id": "q1",
                  "feedback_text": "Flawless work, says your teacher."}],
})
check("student PATCH with a feedback key is rejected (403)",
      r.status_code == 403 and r.get_json().get("code") == "feedback_forbidden",
      f"status={r.status_code} body={r.get_json()}")
check("rejection names the reviewer path",
      "reviewer" in (r.get_json().get("error") or "").lower(),
      f"error={r.get_json().get('error')!r}")

r = student_client.get(f"/api/v1/submissions/{submission_id}")
feedback = r.get_json()["feedback"]
check("the fabricated feedback did NOT land — teacher's text intact",
      len(feedback) == 1
      and feedback[0]["feedback_text"] == "Watch the sign in step 2.",
      f"feedback={feedback}")

# Mixed payload: the whole request must fail, nothing half-applies.
r = student_client.patch(f"/api/v1/submissions/{submission_id}", json={
    "responses": [{"question_id": "q1", "response_text": "smuggled"}],
    "feedback": [{"question_id": "q1", "feedback_text": "forged"}],
})
check("mixed responses+feedback PATCH is rejected wholesale (403)",
      r.status_code == 403 and r.get_json().get("code") == "feedback_forbidden",
      f"status={r.status_code} body={r.get_json()}")
r = student_client.get(f"/api/v1/submissions/{submission_id}")
check("rejected mixed payload left responses untouched",
      r.get_json()["responses"] == [
          {"question_id": "q1", "response_text": "my answer"}],
      f"responses={r.get_json()['responses']}")

# --- 2. The student's legitimate PATCH surface is unchanged ---------------------
r = student_client.patch(f"/api/v1/submissions/{submission_id}", json={
    "responses": [{"question_id": "q1", "response_text": "revised answer"}],
})
check("responses-only PATCH still works for the student",
      r.status_code == 200 and r.get_json()["responses"] == [
          {"question_id": "q1", "response_text": "revised answer"}],
      f"status={r.status_code} body={r.get_json()}")

r = student_client.patch(f"/api/v1/submissions/{submission_id}", json={
    "submitted_at": None,
})
check("unsubmit-via-PATCH (submitted_at null) still works",
      r.status_code == 200 and r.get_json()["submitted_at"] is None,
      f"status={r.status_code} body={r.get_json()}")

r = classmate_client.patch(f"/api/v1/submissions/{submission_id}", json={
    "responses": [{"question_id": "q1", "response_text": "hijack"}],
})
check("non-owner still gets the ownership 403 (rule unchanged)",
      r.status_code == 403, f"status={r.status_code}")

# --- 3. POST .../feedback stays reviewer-only (#130 gate pinned) -----------------
r = student_client.post(f"/api/v1/submissions/{submission_id}/feedback", json={
    "question_id": "q2", "feedback_text": "self-reviewed, five stars",
})
check("submission's student cannot self-post feedback (404)",
      r.status_code == 404, f"status={r.status_code} body={r.get_json()}")

r = classmate_client.post(f"/api/v1/submissions/{submission_id}/feedback", json={
    "question_id": "q2", "feedback_text": "classmate meddles",
})
check("same-class student is not a reviewer (404, existence hidden)",
      r.status_code == 404, f"status={r.status_code}")

r = cotutor_client.post(f"/api/v1/submissions/{submission_id}/feedback", json={
    "question_id": "q1", "feedback_text": "Co-tutor note: check q1 step 3.",
})
check("co-tutor (teacher of a linked course) can review (#130 contract)",
      r.status_code == 200
      and r.get_json()["feedback"][0]["feedback_text"]
      == "Co-tutor note: check q1 step 3.",
      f"status={r.status_code} body={r.get_json()}")

r = student_client.get(f"/api/v1/submissions/{submission_id}")
texts = [f["feedback_text"] for f in r.get_json()["feedback"]]
check("student sees only reviewer-authored feedback, nothing forged",
      texts == ["Co-tutor note: check q1 step 3."], f"texts={texts}")

# --- 4. Static: the vulnerable pattern is gone, the gates are pinned ------------
source = (Path(app.root_path) / "routes" / "submissions.py").read_text(
    encoding="utf-8")
patch_slice = source[source.index("def api_update_submission"):
                     source.index("def api_delete_submission")]
feedback_slice = source[source.index("def api_add_feedback"):
                        source.index("def api_finalize_submission")]
check("PATCH no longer applies the feedback key (pre-#63 pattern gone)",
      'updates["feedback"]' not in source)
check("PATCH carries the feedback_forbidden rejection",
      '"feedback" in data' in patch_slice and "feedback_forbidden" in patch_slice)
check("POST .../feedback is can_review-gated (#130)",
      "can_review" in feedback_slice)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
