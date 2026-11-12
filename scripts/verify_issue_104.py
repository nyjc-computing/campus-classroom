"""Verification for issue #104: mirror Campus Classroom submissions into
Google Classroom (student turnIn/reclaim) + the review-page ACL.

No network and no real Google account are involved. The seams are faked
like verify_issue_28/98 do them:

1. The campus session: an in-memory FakeCampus (assignments +
   submissions + auth.broker) installed at both `app.campus` and the
   `campus_python.Campus` constructor (the #92 throwaway the broker
   release builds per release).
2. The Classroom REST API: `ClassroomClient.request` is replaced with
   a scripted fake serving v1/courses (the enrollment bridge) and the
   four student-verb calls the mirror makes
   (studentSubmissions.list / :modifyAttachments / :turnIn / :reclaim),
   recording every call and mutating a scripted Classroom state.

Covers, per the issue checklist:

1. Submit mirrors: modifyAttachments (url-only link, absolute
   canonical-origin review URL) + turnIn with the right course/
   coursework/submission ids; campus submit stays 200 and the response
   carries "mirror": "mirrored".
2. Already-TURNED_IN -> no duplicate turnIn (no-op).
3. Link already present (matched by url) -> no duplicate
   modifyAttachments, turnIn still happens.
4. Missing scope -> graceful skip ("skipped_missing_scope"), campus
   submit still 200 — both shapes: a grant without coursework.me
   (local gate -> MissingClassroomScopesError) and the pre-campus#1095
   vault-cap rejection of the ask itself (BrokerConfigError).
5. Unsubmit -> reclaim; reclaim's FAILED_PRECONDITION (nothing to
   reclaim) -> success.
6. Transient Classroom failure -> retried once, then mirrored;
   persistent failure -> "failed", campus submit STILL 200.
7. Review-page ACL on /submissions/<id>: staff owner 200 (editable
   view), owning student 200 read-only (no submit button, no auto-save,
   no draftGrade anywhere), everyone else 404.

Usage: .venv/Scripts/python.exe scripts/verify_issue_104.py
"""

import os
import re
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-104-secret"

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import (  # noqa: E402
    classroom_auth as cauth,
)
from apps.classroom import (
    create_app,
    submission_mirror,
)

TEACHER = "uid-user-teacher"
STUDENT = "uid-user-student"
STUDENT2 = "uid-user-student2"
STUDENT3 = "uid-user-student3"
OTHER = "uid-user-other"

COURSES_SCOPE = "https://www.googleapis.com/auth/classroom.courses.readonly"
MIRROR_SCOPE = "https://www.googleapis.com/auth/classroom.coursework.me"
REVIEW_URL_S1 = "http://localhost:5000/submissions/s1"

LINK = [COURSES_SCOPE, MIRROR_SCOPE]
LINK_NO_MIRROR = [COURSES_SCOPE]  # the pre-reconnect grant (#104)

# --- fake Classroom state (the #104 submission-mirror seam) ------------------

# (course_id, coursework_id) -> the scripted studentSubmission record.
CLASSROOM_STATE: dict = {}
# Every mirror verb call: (verb, course_id, coursework_id, submission_id, body)
MIRROR_CALLS: list = []
# Exceptions to raise (once each) before a call proceeds; and a latch
# for a persistent failure (the no-retry-helps case).
FAIL_ONCE: list = []
FAIL_ALWAYS = False


def reset_classroom(course_id="course-1", coursework_id="cw-1",
                    state="NEW", attachments=None, submission_id="gsub-1"):
    CLASSROOM_STATE.clear()
    CLASSROOM_STATE[(course_id, coursework_id)] = {
        "id": submission_id,
        "state": state,
        "assignmentSubmission": {"attachments": attachments or []},
    }
    MIRROR_CALLS.clear()
    FAIL_ONCE.clear()
    global FAIL_ALWAYS
    FAIL_ALWAYS = False


def mirror_verbs():
    return [call[0] for call in MIRROR_CALLS]


def _parse_ids(path: str) -> tuple[str, str]:
    # v1/courses/<c>/courseWork/<w>/...
    parts = path.split("/")
    return parts[2], parts[4]


def _fake_classroom_request(self, method, path, *, params=None, json=None):  # noqa: ARG001
    """ClassroomClient.request stand-in: enrollment + mirror verbs."""
    self._access_token()  # mirror the real flow: release before the call
    global FAIL_ALWAYS
    mirror_path = path != "v1/courses"
    if mirror_path and FAIL_ONCE:
        raise FAIL_ONCE.pop(0)
    if mirror_path and FAIL_ALWAYS:
        raise cauth.ClassroomAPIError(503, "backend error")
    if path == "v1/courses":
        email = self._creds["email"]
        return {"courses": COURSES_BY_EMAIL.get(email, [])}
    if path.endswith("/studentSubmissions") and method == "GET":
        course_id, coursework_id = _parse_ids(path)
        record = CLASSROOM_STATE.get((course_id, coursework_id))
        return {"studentSubmissions": [dict(record)] if record else []}
    match = re.search(r"/studentSubmissions/([^:/]+):(\w+)$", path)
    if match:
        submission_id, verb = match.groups()
        course_id, coursework_id = _parse_ids(path)
        record = CLASSROOM_STATE.get((course_id, coursework_id))
        if record is None:
            raise cauth.ClassroomAPIError(404, "not found")
        MIRROR_CALLS.append((verb, course_id, coursework_id, submission_id,
                             json))
        if verb == "modifyAttachments":
            record["assignmentSubmission"]["attachments"].extend(
                json["addAttachments"])
            return {}
        if verb == "turnIn":
            record["state"] = "TURNED_IN"
            return {}
        if verb == "reclaim":
            if record["state"] != "TURNED_IN":
                # FAILED_PRECONDITION: nothing to reclaim (NEW or
                # already reclaimed) — the mirror treats this as done.
                raise cauth.ClassroomAPIError(409, "FAILED_PRECONDITION")
            record["state"] = "RECLAIMED_BY_STUDENT"
            return {}
    raise AssertionError(f"unexpected Classroom call {method} {path!r}")


cauth.ClassroomClient.request = _fake_classroom_request

# --- fake Campus backend -----------------------------------------------------

CURRENT_EMAIL: str | None = None
COURSES_BY_EMAIL: dict[str, list[dict]] = {}
GRANTED_SCOPES_BY_EMAIL: dict[str, list[str]] = {}
# Emails whose broker rejects the coursework.me ask itself (the
# pre-campus#1095 vault cap: 400 AUTH_INVALID_SCOPE).
DENY_MIRROR_ASK: set[str] = set()


class FakeBroker:
    """auth.broker stand-in: releases the scripted per-user grant.

    min_scopes are ignored beyond the scripted DENY_MIRROR_ASK set —
    the mirror's local scope gate catches a token without
    coursework.me, which is exactly the #30-design enforcement point.
    """

    def token(self, provider, integration, *, min_scopes=None):  # noqa: ARG002
        assert (provider, integration) == ("google", "classroom")
        email = CURRENT_EMAIL
        if (email in DENY_MIRROR_ASK and min_scopes
                and MIRROR_SCOPE in min_scopes):
            raise campus_errors.BadRequestError(
                400,
                "Requested scopes exceed the google.classroom "
                "integration's configured scope cap",
                details={},
            )
        scopes = GRANTED_SCOPES_BY_EMAIL.get(email, LINK)
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


class FakeSubmission:
    def __init__(self, id, assignment_id, student_id, course_id="course-1"):
        self.id = id
        self.assignment_id = assignment_id
        self.student_id = student_id
        self.course_id = course_id
        self.responses = []
        self.feedback = []
        self.submitted_at = None
        self.updated_at = "2026-11-12T00:00:00Z"

    def to_resource(self):
        return {
            "id": self.id, "assignment_id": self.assignment_id,
            "student_id": self.student_id, "course_id": self.course_id,
            "responses": self.responses, "feedback": self.feedback,
            "submitted_at": self.submitted_at, "updated_at": self.updated_at,
        }


class FakeAssignments:
    def __init__(self, store):
        self._store = store

    def __getitem__(self, assignment_id):
        record = self._store.get(assignment_id)
        if record is None:
            raise RuntimeError("Assignment not found")
        return SimpleNamespace(get=lambda: record)


class FakeSubmissions:
    def __init__(self, store):
        self._store = store

    def __getitem__(self, submission_id):
        store = self._store

        class _Handle:
            def get(self):
                item = store.get(submission_id)
                if item is None:
                    raise RuntimeError("Submission not found")
                return item

            def submit(self):
                item = store.get(submission_id)
                if item is None:
                    raise RuntimeError("Submission not found")
                item.submitted_at = "2026-11-12T00:00:00Z"

            def unsubmit(self):
                item = store.get(submission_id)
                if item is None:
                    raise RuntimeError("Submission not found")
                item.submitted_at = None

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
    "a1": FakeAssignment(
        "a1", TEACHER, "Algebra Worksheet",
        classroom_links=[SimpleNamespace(
            course_id="course-1", coursework_id="cw-1", attachment_id=None)],
    ),
}
SUBMISSIONS = {
    f"s{n}": FakeSubmission(f"s{n}", "a1", STUDENT) for n in range(1, 7)
}
SUBMISSIONS["s_acl"] = FakeSubmission("s_acl", "a1", STUDENT)
SUBMISSIONS["s2"].student_id = STUDENT2
SUBMISSIONS["s3"].student_id = STUDENT3

FAKE_CAMPUS = FakeCampus(ASSIGNMENTS, SUBMISSIONS)

# Scripted enrollments + grants (email-keyed; the fake user session
# publishes the email).
for student in (STUDENT, STUDENT2, STUDENT3):
    COURSES_BY_EMAIL[f"{student}@nyjc.edu.sg"] = [
        {"id": "course-1", "name": "CampusClass"}]
COURSES_BY_EMAIL[f"{OTHER}@nyjc.edu.sg"] = []
for email in COURSES_BY_EMAIL:
    GRANTED_SCOPES_BY_EMAIL[email] = LINK
GRANTED_SCOPES_BY_EMAIL[f"{STUDENT2}@nyjc.edu.sg"] = LINK_NO_MIRROR
DENY_MIRROR_ASK.add(f"{STUDENT3}@nyjc.edu.sg")


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


teacher_client = app.test_client()
seed_user(teacher_client, TEACHER)
_ajaxify(teacher_client)

student_client = app.test_client()
seed_user(student_client, STUDENT)
_ajaxify(student_client)

student2_client = app.test_client()
seed_user(student2_client, STUDENT2)
_ajaxify(student2_client)

student3_client = app.test_client()
seed_user(student3_client, STUDENT3)
_ajaxify(student3_client)

other_client = app.test_client()
seed_user(other_client, OTHER)
_ajaxify(other_client)


# --- 1. scope plumbing --------------------------------------------------------
print("== scope plumbing ==")

check("CLASSROOM_SCOPES_MIRROR is the coursework.me scope",
      cauth.CLASSROOM_SCOPES_MIRROR == (MIRROR_SCOPE,))
check("the mirror scope is broker-askable",
      MIRROR_SCOPE in cauth._BROKER_ASKABLE)
check("the mirror scope is not part of the MVP connect set",
      MIRROR_SCOPE not in cauth.CLASSROOM_SCOPES_MVP)

# The local gate: a released credential without coursework.me must
# raise MissingClassroomScopesError (the #30-design enforcement point).
with app.app_context():
    flask.g.user = SimpleNamespace(
        id=STUDENT2, email=f"{STUDENT2}@nyjc.edu.sg")
    try:
        with cauth.with_classroom_session(
                required_scopes=cauth.CLASSROOM_SCOPES_MIRROR):
            gated = False
    except cauth.MissingClassroomScopesError as err:
        gated = err.missing == [MIRROR_SCOPE]
    except cauth.ClassroomAuthError:
        gated = False
check("credential without coursework.me -> MissingClassroomScopesError",
      gated)


# --- 2. submit mirrors: link chip + turnIn ------------------------------------
print("== submit mirrors (modifyAttachments + turnIn) ==")

reset_classroom(state="NEW")
r = student_client.post("/api/v1/submissions/s1/submit", json={})
body = r.get_json(silent=True) or {}
check("campus submit answers 200 with mirror: mirrored",
      r.status_code == 200 and body.get("mirror") == "mirrored",
      f"status={r.status_code} body={str(body)[:160]}")
check("campus submit committed (submitted_at set)",
      SUBMISSIONS["s1"].submitted_at is not None)
check("modifyAttachments called with course/coursework/submission ids "
      "and the url-only review link",
      ("modifyAttachments", "course-1", "cw-1", "gsub-1",
       {"addAttachments": [{"link": {"url": REVIEW_URL_S1}}]})
      in MIRROR_CALLS,
      f"calls={MIRROR_CALLS}")
check("turnIn called with the right ids",
      ("turnIn", "course-1", "cw-1", "gsub-1", {}) in MIRROR_CALLS,
      f"calls={MIRROR_CALLS}")
check("Classroom state flipped to TURNED_IN",
      CLASSROOM_STATE[("course-1", "cw-1")]["state"] == "TURNED_IN")

# --- 3. already TURNED_IN -> no duplicate turnIn ------------------------------
print("== already-TURNED_IN is a no-op ==")

reset_classroom(state="TURNED_IN")
r = student_client.post("/api/v1/submissions/s1/submit", json={})
check("re-submit with Classroom TURNED_IN -> 200 mirrored",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror") == "mirrored",
      f"status={r.status_code}")
check("no duplicate turnIn", "turnIn" not in mirror_verbs(),
      f"verbs={mirror_verbs()}")
check("no duplicate modifyAttachments either (full no-op)",
      "modifyAttachments" not in mirror_verbs(), f"verbs={mirror_verbs()}")

# --- 4. link already attached -> idempotent modify, turnIn still --------------
print("== attached link matched by url ==")

reset_classroom(state="NEW", attachments=[{"link": {"url": REVIEW_URL_S1}}])
r = student_client.post("/api/v1/submissions/s1/submit", json={})
check("link already present -> 200 mirrored",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror") == "mirrored",
      f"status={r.status_code}")
check("no duplicate modifyAttachments", "modifyAttachments" not in mirror_verbs(),
      f"verbs={mirror_verbs()}")
check("turnIn still happened", "turnIn" in mirror_verbs(),
      f"verbs={mirror_verbs()}")

# --- 5. missing scope -> graceful skip, campus submit still 200 ---------------
print("== missing scope degrades to a logged skip ==")

reset_classroom(state="NEW")
r = student2_client.post("/api/v1/submissions/s2/submit", json={})
check("grant without coursework.me -> 200 with mirror: "
      "skipped_missing_scope",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror")
      == "skipped_missing_scope",
      f"status={r.status_code} body={str(r.get_data(as_text=True))[:160]}")
check("campus submit committed despite the skip",
      SUBMISSIONS["s2"].submitted_at is not None)
check("no Classroom mirror verbs ran for the skipped submit",
      not MIRROR_CALLS, f"calls={MIRROR_CALLS}")

reset_classroom(state="NEW")
r = student3_client.post("/api/v1/submissions/s3/submit", json={})
check("pre-campus#1095 vault-cap rejection -> 200 with "
      "skipped_missing_scope",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror")
      == "skipped_missing_scope",
      f"status={r.status_code} body={str(r.get_data(as_text=True))[:160]}")
check("campus submit committed despite the vault-cap skip",
      SUBMISSIONS["s3"].submitted_at is not None)

# --- 6. unsubmit -> reclaim (and already-reclaimed -> success) ----------------
print("== unsubmit mirrors (reclaim) ==")

reset_classroom(state="TURNED_IN")
r = student_client.post("/api/v1/submissions/s1/unsubmit", json={})
check("unsubmit after a mirrored turnIn -> 200 mirrored (reclaim ran)",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror") == "mirrored"
      and SUBMISSIONS["s1"].submitted_at is None,
      f"status={r.status_code}")
check("reclaim called with the right ids",
      ("reclaim", "course-1", "cw-1", "gsub-1", {}) in MIRROR_CALLS,
      f"calls={MIRROR_CALLS}")
check("Classroom state flipped to RECLAIMED_BY_STUDENT",
      CLASSROOM_STATE[("course-1", "cw-1")]["state"] == "RECLAIMED_BY_STUDENT")

reset_classroom(state="NEW")  # never turned in: reclaim precondition-fails
r = student_client.post("/api/v1/submissions/s4/unsubmit", json={})
check("already-reclaimed/NEW (FAILED_PRECONDITION) -> 200 mirrored",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror") == "mirrored",
      f"status={r.status_code} body={str(r.get_data(as_text=True))[:160]}")
check("campus unsubmit committed", SUBMISSIONS["s4"].submitted_at is None)

# --- 7. transient retry once; persistent failure stays non-blocking ----------
print("== best-effort contract: retry once, then log + report ==")

reset_classroom(state="NEW")
FAIL_ONCE.append(cauth.ClassroomAPIError(503, "backend error"))
r = student_client.post("/api/v1/submissions/s5/submit", json={})
check("one transient 503 -> retried once, mirror: mirrored",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror") == "mirrored"
      and SUBMISSIONS["s5"].submitted_at is not None,
      f"status={r.status_code}")
check("the retried pass completed turnIn",
      "turnIn" in mirror_verbs()
      and CLASSROOM_STATE[("course-1", "cw-1")]["state"] == "TURNED_IN",
      f"verbs={mirror_verbs()}")

reset_classroom(state="NEW")
FAIL_ALWAYS = True
r = student_client.post("/api/v1/submissions/s6/submit", json={})
check("persistent Classroom failure -> campus submit STILL 200, "
      "mirror: failed",
      r.status_code == 200
      and (r.get_json(silent=True) or {}).get("mirror") == "failed"
      and SUBMISSIONS["s6"].submitted_at is not None,
      f"status={r.status_code} body={str(r.get_data(as_text=True))[:160]}")
FAIL_ALWAYS = False

# A submission whose course is not among the assignment's links cannot
# resolve a CourseWork: reported as failed, campus action untouched.
status = submission_mirror.mirror_submit(
    ASSIGNMENTS["a1"],
    SimpleNamespace(id="sx", course_id="course-99"),
)
check("unresolvable coursework -> status failed (never raised)",
      status == "failed", f"status={status!r}")

# --- 8. review-page ACL ---------------------------------------------------------
print("== review-page ACL on /submissions/<id> ==")

reset_classroom(state="NEW")  # s_acl is a draft; classroom state irrelevant

r = teacher_client.get("/submissions/s_acl")
staff_html = r.get_data(as_text=True)
check("staff owner -> 200",
      r.status_code == 200, f"status={r.status_code}")
check("staff owner keeps the editable view (readOnly false, submit "
      "button present on a draft)",
      "const readOnly = false" in staff_html
      and "Submit Assignment" in staff_html,
      f"readOnly={'const readOnly = false' in staff_html} "
      f"submit={'Submit Assignment' in staff_html}")

r = student_client.get("/submissions/s_acl")
student_html = r.get_data(as_text=True)
check("owning student -> 200",
      r.status_code == 200, f"status={r.status_code}")
check("owning student view is read-only",
      "const readOnly = true" in student_html,
      f"readOnly marker in html={'const readOnly = true' in student_html}")
check("owning student's render paths are gated on readOnly "
      "(no button, no auto-save)",
      "${(!submission.submitted_at && !readOnly)" in student_html
      and "if (readOnly) return" in student_html,
      f"button gate={'${(!submission.submitted_at && !readOnly)' in student_html} "
      f"save guard={'if (readOnly) return' in student_html}")
check("owning student gets no draftGrade anywhere",
      "draftGrade" not in student_html and "draft_grade" not in student_html)

r = other_client.get("/submissions/s_acl")
check("unrelated signed-in user -> 404 (existence not revealed)",
      r.status_code == 404, f"status={r.status_code}")

r = other_client.get("/submissions/does-not-exist")
check("unknown submission id -> 404", r.status_code == 404,
      f"status={r.status_code}")

r = student2_client.get("/submissions/s_acl")
check("a different student's submission -> 404 for them too",
      r.status_code == 404, f"status={r.status_code}")

# The ACL blocks at the page, while the JSON API keeps its own rules —
# the student's data read remains theirs (verified: 200 for the owner
# student, 404 for the unrelated user).
r = student_client.get("/api/v1/submissions/s_acl")
r_other = other_client.get("/api/v1/submissions/s_acl")
check("submission JSON API ACL unchanged (student 200, other 404)",
      r.status_code == 200 and r_other.status_code == 404,
      f"student={r.status_code} other={r_other.status_code}")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("All checks passed.")
