"""Verification for issue #14 (link-path student experience, PRD v1.3/§6.9):
rule 4 assigned-only listing + answering/submitting on the /a/ share page.
The add-on iframe lane is parked (licence-blocked, #11); the Classroom
bridge is faked at its two seams (campus.auth broker release, Classroom
REST courses.list) so no network and no real Google account are involved:

1. Rule 4 listing (GET /api/v1/assignments): a student sees own +
   assigned-via-their-courses only (never the enumerable index Campus
   API returns); a teacher in a shared course sees the assignment even
   though they did not create it; a user whose Classroom bridge is
   disconnected degrades to own-only without a 500; newest first.
2. /a/ student answer sheet: an enrolled non-owner gets the answer form
   (textareas + actions + status); a ?course= pin from the per-course
   Link Material URL lands in the page context; an unknown pin is
   ignored; a student sharing several courses gets the class picker
   context; the owner gets a render-only preview with a manage link and
   no form; the #28 gates (unenrolled 403, disconnected connect prompt)
   still hold.
3. The API sequence the page drives: for-student 404 → create (course
   keyed) → PATCH responses (full replace) → submit → teacher review
   via by-assignment → feedback added → for-student resumes with
   feedback → unsubmit → resubmit.
4. Send flow pinning (classroom_sync): each course's Link Material URL
   is /a/{id}?course=<courseId>; already-linked courses are skipped.
5. courses.list pagination: ClassroomClient.courses_list follows
   nextPageToken (the #14 listing must see courses beyond page 1).

Usage: .venv/Scripts/python.exe scripts/verify_issue_14.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-14-secret"

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import classroom_sync as csync  # noqa: E402
from apps.classroom import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Fake Campus backend: in-memory assignments/submissions + call recording
# ---------------------------------------------------------------------------
TEACHER = "uid-user-teacher"
OTHER_TEACHER = "uid-user-teacher2"
STUDENT = "uid-user-student"
STUDENT2 = "uid-user-student2"
STUDENT3 = "uid-user-student3"

QUESTIONS = [
    {"id": "q1", "prompt": None, "question": "Root question"},
    {"id": "q1.a", "prompt": None, "question": "Nested part a"},
    {"id": "q2", "prompt": "Hint available", "question": "Second root"},
]


class FakeAssignment:
    def __init__(self, id, created_by, title, description="", questions=None,
                 created_at="2026-10-20T00:00:00Z"):
        self.id = id
        self.created_by = created_by
        self.title = title
        self.description = description
        self.questions = questions or []
        self.classroom_links = []
        self.created_at = created_at

    def to_resource(self):
        return {
            "id": self.id, "created_by": self.created_by, "title": self.title,
            "description": self.description, "questions": self.questions,
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

        class _Links:
            def add(self, course_id, coursework_id, attachment_id=None):
                calls.append(("assignments.links.add", {
                    "assignment_id": assignment_id, "course_id": course_id,
                    "coursework_id": coursework_id,
                    "attachment_id": attachment_id,
                }))

        class _Handle:
            def get(self):
                calls.append(("assignments.get", assignment_id))
                item = store.get(assignment_id)
                if item is None:
                    raise _not_found("Assignment")
                return item

            @property
            def links(self):
                return _Links()

        return _Handle()

    def list(self, created_by=None, **_):
        self._calls.append(("assignments.list", {"created_by": created_by}))
        if created_by is None:
            return list(self._store.values())  # rule 4 filters app-side
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
            def responses(self):
                return _Responses()

            @property
            def feedback(self):
                return _Feedback()

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
                item.submitted_at = "2026-10-27T00:00:00Z"

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
            "course_id": course_id,
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


# --- Fake auth bridge seams --------------------------------------------------
# Keyed by the signed-in user's email, which the fake user session publishes.
# COURSES_PAGES_BY_EMAIL holds PAGES of courses (courses.list pagination).

CURRENT_EMAIL: str | None = None
BROKER_CONNECTED_BY_EMAIL: dict[str, bool] = {}
COURSES_PAGES_BY_EMAIL: dict[str, list[list[dict]]] = {}


def _courses(email):
    pages = COURSES_PAGES_BY_EMAIL.get(email, [])
    return [course for page in pages for course in page]


class FakeBroker:
    """auth.broker stand-in: releases a courses.readonly-scoped token,
    or 404s (NotFoundError) when that user is scripted as disconnected."""

    def token(self, provider, integration, *, min_scopes=None):  # noqa: ARG002
        assert (provider, integration) == ("google", "classroom")
        email = CURRENT_EMAIL
        if not BROKER_CONNECTED_BY_EMAIL.get(email, True):
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
    """ClassroomClient.request stand-in: paged courses.list per user."""
    self._access_token()  # mirror the real flow: release before the call
    assert path == "v1/courses", f"unexpected Classroom path {path!r}"
    email = self._creds["email"]
    pages = COURSES_PAGES_BY_EMAIL.get(email, [])
    page_index = int((params or {}).get("pageToken") or 0)
    data: dict = {"courses": pages[page_index] if page_index < len(pages) else []}
    if page_index + 1 < len(pages):
        data["nextPageToken"] = str(page_index + 1)
    return data


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
                         questions=QUESTIONS,
                         created_at="2026-10-20T00:00:00Z"),
    "a2": FakeAssignment("a2", OTHER_TEACHER, "Geometry Quiz",
                         created_at="2026-10-21T00:00:00Z"),
    "a3": FakeAssignment("a3", TEACHER, "Unlinked Assignment",
                         created_at="2026-10-19T00:00:00Z"),
}
SUBMISSIONS = {}
CALLS = []
FAKE_CAMPUS = FakeCampus(ASSIGNMENTS, SUBMISSIONS, CALLS)

cauth.ClassroomClient.request = _fake_classroom_request

TEACHER_EMAIL = f"{TEACHER}@nyjc.edu.sg"
OTHER_EMAIL = f"{OTHER_TEACHER}@nyjc.edu.sg"
STUDENT_EMAIL = f"{STUDENT}@nyjc.edu.sg"
STUDENT2_EMAIL = f"{STUDENT2}@nyjc.edu.sg"
STUDENT3_EMAIL = f"{STUDENT3}@nyjc.edu.sg"

# Courses: STUDENT is in course-1 (one page); STUDENT2 only in course-2;
# STUDENT3 shares course-1 AND course-2 (two pages — exercises pagination);
# TEACHER teaches course-2.
COURSES_PAGES_BY_EMAIL.update({
    STUDENT_EMAIL: [[{"id": "course-1", "name": "CampusClass"}]],
    STUDENT2_EMAIL: [[{"id": "course-2", "name": "Other Class"}]],
    STUDENT3_EMAIL: [
        [{"id": "course-1", "name": "CampusClass"}],
        [{"id": "course-2", "name": "Other Class"}],
    ],
    TEACHER_EMAIL: [[{"id": "course-2", "name": "Other Class"}]],
})

# a1 assigned to course-1 (STUDENT/STUDENT3's class); a2 to course-2.
ASSIGNMENTS["a1"].classroom_links = [
    SimpleNamespace(course_id="course-1", coursework_id="cw-1")
]
ASSIGNMENTS["a2"].classroom_links = [
    SimpleNamespace(course_id="course-2", coursework_id="cw-2")
]


# --- App with a faked Campus login -------------------------------------------
def _fake_push_context(self):  # noqa: ARG001 (monkeypatched method)
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

student2_client = app.test_client()
seed_user(student2_client, STUDENT2)
_ajaxify(student2_client)

student3_client = app.test_client()
seed_user(student3_client, STUDENT3)
_ajaxify(student3_client)

teacher_client = app.test_client()
seed_user(teacher_client, TEACHER)
_ajaxify(teacher_client)

# --- 1. Rule 4: assigned-only listing ----------------------------------------
CALLS.clear()
r = student_client.get("/api/v1/assignments")
ids = [a["id"] for a in (r.get_json() or [])]
check("student sees assignments assigned via their courses (a1)",
      r.status_code == 200 and ids == ["a1"],
      f"status={r.status_code} ids={ids}")
check("student does NOT see other classes' or unlinked assignments (a2, a3)",
      "a2" not in ids and "a3" not in ids, f"ids={ids}")
check("listing reads Campus unfiltered; rule 4 filtering is app-side",
      CALLS and CALLS[0] == ("assignments.list", {"created_by": None}),
      f"calls={CALLS}")

r = teacher_client.get("/api/v1/assignments")
ids = [a["id"] for a in (r.get_json() or [])]
check("teacher sees own assignments (a1, a3) plus ones assigned to a "
      "course they teach (a2 via course-2)",
      r.status_code == 200 and set(ids) == {"a1", "a2", "a3"},
      f"status={r.status_code} ids={ids}")

# Newest first: teacher's set spans Oct 19/20/21.
r = teacher_client.get("/api/v1/assignments")
ids = [a["id"] for a in (r.get_json() or [])]
check("listing is newest first", ids == ["a2", "a1", "a3"], f"ids={ids}")

BROKER_CONNECTED_BY_EMAIL[STUDENT2_EMAIL] = False
r = student2_client.get("/api/v1/assignments")
check("disconnected user's listing degrades to own-only (no 500, no leak)",
      r.status_code == 200 and r.get_json() == [],
      f"status={r.status_code} body={r.get_json()}")
del BROKER_CONNECTED_BY_EMAIL[STUDENT2_EMAIL]

# --- 2. /a/ student answer sheet ---------------------------------------------
r = student_client.get("/a/a1")
ok = r.status_code == 200
check("enrolled student /a/a1 renders the answer sheet", ok,
      f"status={r.status_code}")
check("answer sheet has per-question textareas",
      ok and b"answer-input" in r.data)
check("answer sheet has the action area (save/submit)",
      ok and b"share-actions" in r.data)
check("question content is server-rendered",
      ok and b"Root question" in r.data and b"Nested part a" in r.data)

r = student_client.get("/a/a1?course=course-1")
check("pinned ?course= from the Link Material URL lands in the page",
      r.status_code == 200 and b'pinnedCourse: "course-1"' in r.data,
      f"status={r.status_code}")

r = student_client.get("/a/a1?course=bogus")
check("unknown ?course= pin is ignored (falls back)",
      r.status_code == 200 and b'pinnedCourse: null' in r.data,
      f"status={r.status_code}")

r = student3_client.get("/a/a1")
check("student sharing one linked course gets no multi-class picker need",
      r.status_code == 200, f"status={r.status_code}")

# STUDENT3 is in both course-1 and course-2; link a1 to course-2 as well
# so the share page must hand the form a two-class context.
ASSIGNMENTS["a1"].classroom_links.append(
    SimpleNamespace(course_id="course-2", coursework_id="cw-2"))
r = student3_client.get("/a/a1")
check("student sharing several linked courses gets the class picker",
      r.status_code == 200
      and b"share-course" in r.data and b'pinnedCourse: null' in r.data,
      f"status={r.status_code}")
r = student3_client.get("/a/a1?course=course-2")
check("picker context honors a valid pin (course-2)",
      r.status_code == 200 and b'pinnedCourse: "course-2"' in r.data,
      f"status={r.status_code}")
ASSIGNMENTS["a1"].classroom_links = [
    SimpleNamespace(course_id="course-1", coursework_id="cw-1")]

r = teacher_client.get("/a/a1")
check("owner /a/a1 is a render-only preview (no form)",
      r.status_code == 200 and b"You own this assignment" in r.data
      and b"data-qid=" not in r.data,
      f"status={r.status_code}")
check("owner preview links to the manage page",
      b"/assignments/a1" in r.data)

r = student2_client.get("/a/a1")
check("unenrolled student /a/a1 still gets the 403 gate page",
      r.status_code == 403, f"status={r.status_code}")
check("gate page leaks no assignment content",
      b"Algebra Worksheet" not in r.data)

BROKER_CONNECTED_BY_EMAIL[STUDENT2_EMAIL] = False
r = student2_client.get("/a/a1")
check("disconnected student gets the connect prompt (403)",
      r.status_code == 403 and b"Connect" in r.data, f"status={r.status_code}")
check("connect prompt leaks no assignment content",
      b"Algebra Worksheet" not in r.data)
del BROKER_CONNECTED_BY_EMAIL[STUDENT2_EMAIL]

# --- 3. The API sequence the answer sheet drives ------------------------------
CALLS.clear()
r = student_client.get("/api/v1/submissions/for-student?assignment_id=a1")
check("for-student answers 404 before the first attempt",
      r.status_code == 404, f"status={r.status_code}")

r = student_client.post("/api/v1/submissions", json={
    "assignment_id": "a1", "course_id": "course-1",
    "responses": [{"question_id": "q1", "response_text": "first try"}],
})
check("answer sheet creates the submission, keyed to the shared course",
      r.status_code == 201 and r.get_json()["course_id"] == "course-1",
      f"status={r.status_code} body={r.get_json()}")
submission_id = r.get_json()["id"]

r = student_client.patch(f"/api/v1/submissions/{submission_id}", json={
    "responses": [
        {"question_id": "q1", "response_text": "revised q1"},
        {"question_id": "q1.a", "response_text": "part a"},
    ],
})
check("save draft full-replaces responses (resumable)",
      r.status_code == 200 and len(r.get_json()["responses"]) == 2,
      f"status={r.status_code} body={r.get_json()}")

r = student_client.get("/api/v1/submissions/for-student?assignment_id=a1")
check("reopening the page resumes the draft",
      r.status_code == 200 and r.get_json()["id"] == submission_id
      and len(r.get_json()["responses"]) == 2,
      f"status={r.status_code} body={r.get_json()}")

r = student_client.post(f"/api/v1/submissions/{submission_id}/submit")
check("submit sets submitted_at",
      r.status_code == 200 and r.get_json()["submitted_at"],
      f"status={r.status_code} body={r.get_json()}")

r = teacher_client.get("/api/v1/submissions/by-assignment/a1")
check("teacher review sees the submitted work",
      r.status_code == 200
      and [s["id"] for s in r.get_json()] == [submission_id],
      f"status={r.status_code} body={r.get_json()}")

r = teacher_client.post(f"/api/v1/submissions/{submission_id}/feedback", json={
    "question_id": "q1", "feedback_text": "Watch the sign in step 2.",
})
check("teacher feedback lands on the submission",
      r.status_code == 200
      and r.get_json()["feedback"][0]["feedback_text"] == "Watch the sign in step 2.",
      f"status={r.status_code} body={r.get_json()}")

r = student_client.get("/api/v1/submissions/for-student?assignment_id=a1")
check("student resume view carries the released feedback",
      r.status_code == 200 and len(r.get_json()["feedback"]) == 1,
      f"status={r.status_code} body={r.get_json()}")

r = student_client.post(f"/api/v1/submissions/{submission_id}/unsubmit")
check("unsubmit reopens the attempt",
      r.status_code == 200 and r.get_json()["submitted_at"] is None,
      f"status={r.status_code} body={r.get_json()}")

r = student_client.post(f"/api/v1/submissions/{submission_id}/submit")
check("resubmit works", r.status_code == 200 and r.get_json()["submitted_at"],
      f"status={r.status_code}")

# --- 4. Send flow pins the course in the Link Material URL -------------------
created_coursework = []
added_links = []


class _SendClassroom:
    def check_user_capability(self, capability):  # noqa: ARG002
        return False  # link-material path

    def coursework_create(self, course_id, body):
        created_coursework.append((course_id, body))
        return {"id": f"cw-new-{len(created_coursework)}"}

    def coursework_patch(self, course_id, coursework_id, body):  # noqa: ARG002
        return {}


class _SendLinks:
    def add(self, course_id, coursework_id, attachment_id=None):
        added_links.append((course_id, coursework_id, attachment_id))


class _SendAssignmentHandle:
    def get(self):
        return ASSIGNMENTS["a1"]

    @property
    def links(self):
        return _SendLinks()


class _SendAssignments:
    def __getitem__(self, assignment_id):
        return _SendAssignmentHandle()


class _SendCampusClient:
    api = SimpleNamespace(assignments=_SendAssignments())


outcomes = csync.send_assignment_to_classroom(
    _SendCampusClient(), _SendClassroom(), ASSIGNMENTS["a1"],
    ["course-8", "course-9"],  # fresh courses: a1 is linked to course-1
)
check("send posts a draft CourseWork per course",
      [o["status"] for o in outcomes] == ["linked", "linked"]
      and len(created_coursework) == 2,
      f"outcomes={outcomes}")
urls = [body.get("materials", [{}])[0].get("link", {}).get("url", "")
        for _, body in created_coursework]
check("Link Material URL pins its course (?course=)",
      urls == ["http://localhost:5000/a/a1?course=course-8",
               "http://localhost:5000/a/a1?course=course-9"],
      f"urls={urls}")
check("every sent course's link is persisted",
      sorted(c for c, _, _ in added_links) == ["course-8", "course-9"],
      f"links={added_links}")

# --- 5. courses.list pagination ----------------------------------------------
client = cauth.ClassroomClient(required=("https://www.googleapis.com/auth/classroom.courses.readonly",))
client._creds = {"access_token": "x", "expires_at": float("inf"),
                 "scopes": [], "email": STUDENT3_EMAIL}
paged = client.courses_list(page_size=100)
check("courses_list follows nextPageToken across pages",
      [c["id"] for c in paged] == ["course-1", "course-2"],
      f"paged={paged}")


print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
