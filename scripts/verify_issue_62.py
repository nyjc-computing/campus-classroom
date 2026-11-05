"""Verification for issue #62's document-type slice (#78): the
`document` question type end to end, offline (fake Campus backend +
faked login, the #28 harness pattern — no network, no real Google
account).

Covers:

1. documents.py primitives: embed-token mint/verify (tamper, expiry,
   question binding), export-nonce mint/verify (expiry, question
   binding), the nonce-meta extraction, and the snapshot sanitiser
   (scripts, on* handlers, javascript: URLs stripped; the nonce meta
   and benign markup preserved).
2. Enforcement at the submission boundary: a nonce-stamped snapshot is
   sanitised + canonicalised on create and on full-replace PATCH; a
   stale/wrong nonce, a text-only answer to a document question, and a
   missing snapshot all answer 422 invalid_snapshot; text questions
   pass through byte-identical.
3. Export challenges: enrollment-gated, document-questions-only.
4. /a/{id} share page: a document question renders a sandboxed iframe
   pointing at the docs deployment (never the doc HTML inline); with
   EMBED_ORIGIN unset it degrades to a notice; unsupported types render
   a notice instead of a wrong input.
5. /embed: 404 without EMBED_MODE=1; token-gated 200 with the injected
   export runtime and a main-origin-only CSP; 403 on a bad token.

Usage: .venv/Scripts/python.exe scripts/verify_issue_62.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-62-secret"
for var in ("EMBED_MODE", "EMBED_ORIGIN", "EMBED_PARENT_ORIGIN",
            "EMBED_TOKEN_SECRET"):
    os.environ.pop(var, None)

import campus.flask_campus.context as flask_context  # noqa: E402
import flask  # noqa: E402

from apps.classroom import (  # noqa: E402
    create_app,
    documents,
    enrollment,
)

TEACHER = "uid-user-teacher"
STUDENT = "uid-user-student"
ASSIGNMENT_ID = "a1"
DOCS_ORIGIN = "https://docs.example"

DOC_HTML = ("<!DOCTYPE html><html><head><title>Doc</title></head>"
            "<body><h1>Explore</h1><script>alert(1)</script>"
            "<p onclick=\"evil()\">x</p>"
            "<a href=\"javascript:evil()\">y</a></body></html>")

TEXT_QUESTION = SimpleNamespace(
    id="q2", prompt="", question="Explain in your own words.",
    type="free-response", data=None,
)
DOC_QUESTION = SimpleNamespace(
    id="q1", prompt="", question="Explore the document, then submit.",
    type="document", data={"html": DOC_HTML},
)

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Part 1 — documents.py primitives
# ---------------------------------------------------------------------------

token = documents.mint_embed_token("a1", "q1")
check("embed token verifies for its assignment+question",
      documents.verify_embed_token(token, "a1", "q1"))
check("embed token rejects another question",
      not documents.verify_embed_token(token, "a1", "q2"))
check("embed token rejects tampering",
      not documents.verify_embed_token(token[:-2] + "xx", "a1", "q1"))
check("embed token rejects garbage",
      not documents.verify_embed_token("not-a-token", "a1", "q1"))
expired, _ = documents._mint("embed", "a1", "q1", -10)
check("embed token rejects expiry",
      not documents.verify_embed_token(expired, "a1", "q1"))

nonce, exp = documents.mint_export_nonce("a1", "q1")
check("export nonce verifies",
      documents.verify_export_nonce(nonce, "a1", "q1"))
check("export nonce binds the question",
      not documents.verify_export_nonce(nonce, "a1", "q2"))
stale, _ = documents._mint("export", "a1", "q1", -10)
check("export nonce rejects expiry",
      not documents.verify_export_nonce(stale, "a1", "q1"))

stamped = DOC_HTML.replace(
    "</head>", f'<meta name="{documents.NONCE_META_NAME}" content="{nonce}"></head>')
check("snapshot nonce extraction",
      documents._snapshot_nonce(stamped) == nonce)
check("snapshot nonce extraction absent when unstamped",
      documents._snapshot_nonce(DOC_HTML) is None)

clean = documents.sanitize_snapshot(stamped)
check("sanitiser strips scripts",
      "<script" not in clean and "alert(1)" not in clean)
check("sanitiser strips inline event handlers", "onclick" not in clean)
check("sanitiser neutralises javascript: URLs",
      "javascript:" not in clean)
check("sanitiser preserves benign markup",
      "<h1>Explore</h1>" in clean and "<p>x</p>" in clean)
check("sanitiser preserves the nonce meta",
      documents._snapshot_nonce(clean) == nonce)


# ---------------------------------------------------------------------------
# Fake backend (assignment store + submission store + call recording)
# ---------------------------------------------------------------------------

class FakeQuestion:
    def __init__(self, id, type, data=None, prompt="", question=""):
        self.id = id
        self.type = type
        self.data = data
        self.prompt = prompt
        self.question = question


class FakeAssignment:
    def __init__(self, id, created_by, questions):
        self.id = id
        self.created_by = created_by
        self.questions = questions
        self.classroom_links = []

    def to_resource(self):
        return {
            "id": self.id, "created_by": self.created_by,
            "title": "Doc assignment",
            "questions": [
                {"id": q.id, "prompt": q.prompt, "question": q.question,
                 "type": q.type, "data": q.data}
                for q in self.questions
            ],
            "classroom_links": self.classroom_links,
        }


class FakeSubmission:
    def __init__(self, id, assignment_id, student_id, responses=None):
        self.id = id
        self.assignment_id = assignment_id
        self.student_id = student_id
        self.course_id = "course-1"
        self.responses = responses or []
        self.feedback = []
        self.submitted_at = None

    def to_resource(self):
        return {
            "id": self.id, "assignment_id": self.assignment_id,
            "student_id": self.student_id, "course_id": self.course_id,
            "responses": self.responses, "feedback": self.feedback,
            "submitted_at": self.submitted_at,
        }


class FakeAssignments:
    def __init__(self, store, calls):
        self._store = store
        self._calls = calls

    def __getitem__(self, assignment_id):
        return FakeAssignmentResource(self._store, self._calls, assignment_id)


class FakeAssignmentResource:
    def __init__(self, store, calls, assignment_id):
        self._store = store
        self._calls = calls
        self._id = assignment_id

    def get(self):
        self._calls.append(("assignments.get", self._id))
        if self._id not in self._store:
            raise Exception("assignment not found")
        return self._store[self._id]


class FakeSubmissions:
    def __init__(self, store, calls):
        self._store = store
        self._calls = calls
        self._seq = 0

    def __getitem__(self, submission_id):
        return FakeSubmissionResource(self._store, self._calls, submission_id)

    def new(self, *, assignment_id, student_id, course_id,  # noqa: ARG002
            responses=None):
        self._seq += 1
        submission = FakeSubmission(
            f"s-new-{self._seq}", assignment_id, student_id,
            responses=[dict(r) for r in (responses or [])],
        )
        self._store[submission.id] = submission
        self._calls.append(("submissions.new", submission.id, responses))
        return submission

    def list(self, *, assignment_id=None, student_id=None,
             course_id=None):  # noqa: ARG002 (mirror of the SDK signature)
        return [
            s for s in self._store.values()
            if (assignment_id in (None, s.assignment_id))
            and (student_id in (None, s.student_id))
        ]

    def by_assignment(self, assignment_id):  # noqa: ARG002 (SDK signature)
        return [s for s in self._store.values()
                if s.assignment_id == assignment_id]


class FakeSubmissionResource:
    def __init__(self, store, calls, submission_id):
        self._store = store
        self._calls = calls
        self._id = submission_id

    def _current(self):
        if self._id not in self._store:
            raise Exception("submission not found")
        return self._store[self._id]

    def get(self):
        self._calls.append(("submissions.get", self._id))
        return self._current()

    def update(self, **updates):
        self._calls.append(("submissions.update", self._id, updates))
        submission = self._current()
        if "responses" in updates:
            submission.responses = updates["responses"]

    def submit(self):
        self._calls.append(("submissions.submit", self._id))
        self._current().submitted_at = "2026-11-05T00:00:00Z"

    def unsubmit(self):
        self._calls.append(("submissions.unsubmit", self._id))
        self._current().submitted_at = None


class FakeAPI:
    def __init__(self, assignments, submissions):
        self.assignments = assignments
        self.submissions = submissions


class FakeCampus:
    """Both session flavors yield self (the #28 harness idiom)."""

    def __init__(self, api):
        self.api = api

    @contextmanager
    def with_app_session(self):
        yield self

    @contextmanager
    def with_user_session(self):
        yield self


ASSIGNMENTS = {
    ASSIGNMENT_ID: FakeAssignment(
        ASSIGNMENT_ID, TEACHER, [DOC_QUESTION, TEXT_QUESTION]),
}
SUBMISSIONS = {
    "s1": FakeSubmission("s1", ASSIGNMENT_ID, STUDENT),
}
CALLS = []

app = create_app()
app.campus = FakeCampus(FakeAPI(FakeAssignments(ASSIGNMENTS, CALLS),
                                FakeSubmissions(SUBMISSIONS, CALLS)))

# Enrollment seams (the bridge is never exercised offline).
def _enrolled(_assignment):
    return ENROLLED


def _enrolled_courses(_assignment):
    return ([{"course_id": "course-1", "name": "CampusClass"}]
            if ENROLLED else [])


enrollment.is_enrolled = _enrolled
enrollment.enrolled_courses = _enrolled_courses
ENROLLED = True

# Faked login: push_context reads verify_user from the session (#28 idiom).


def _fake_push_context(*_args):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context


def seed_user(client, user_id):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": user_id, "email": f"{user_id}@nyjc.edu.sg"}


def _ajaxify(client):
    for method in ("post", "patch", "put", "delete"):
        original = getattr(client, method)

        def wrapper(path, *a, _orig=original, **kw):
            headers = kw.pop("headers", None) or {}
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            kw["headers"] = headers
            return _orig(path, *a, **kw)

        setattr(client, method, wrapper)


anon_client = app.test_client()
student_client = app.test_client()
seed_user(student_client, STUDENT)
_ajaxify(student_client)


def snapshot_with(nonce_value, html=DOC_HTML):
    return {
        "question_id": "q1",
        "question_type": "document",
        "response_data": {"html": html.replace(
            "</head>", f'<meta name="{documents.NONCE_META_NAME}" '
                       f'content="{nonce_value}"></head>')},
    }


def last_call(name):
    for call in reversed(CALLS):
        if call[0] == name:
            return call
    return None


# ---------------------------------------------------------------------------
# Part 2 — export challenges
# ---------------------------------------------------------------------------

os.environ.pop("EMBED_ORIGIN", None)

# The CSRF guard runs ahead of auth (#12): an anonymous mutation
# without the header hits the 403 csrf guard, never the login redirect.
resp = anon_client.post(f"/api/v1/assignments/{ASSIGNMENT_ID}/export-challenge",
                        json={"question_id": "q1"})
check("anonymous export challenge hits the CSRF guard first (403)",
      resp.status_code == 403
      and resp.get_json()["error"]["code"] == "csrf_header_missing",
      f"status={resp.status_code}")

resp = student_client.post(
    f"/api/v1/assignments/{ASSIGNMENT_ID}/export-challenge",
    json={"question_id": "q1"})
body = resp.get_json()
check("export challenge mints a nonce for a document question",
      resp.status_code == 200 and body
      and documents.verify_export_nonce(body["nonce"], ASSIGNMENT_ID, "q1"),
      f"status={resp.status_code} body={body}")
check("export challenge carries an expiry",
      body and isinstance(body.get("expires_at"), int))

resp = student_client.post(
    f"/api/v1/assignments/{ASSIGNMENT_ID}/export-challenge",
    json={"question_id": "q2"})
check("export challenge refuses non-document questions",
      resp.status_code == 400, f"status={resp.status_code}")

resp = student_client.post(
    f"/api/v1/assignments/{ASSIGNMENT_ID}/export-challenge",
    json={"question_id": "q_ghost"})
check("export challenge refuses unknown questions",
      resp.status_code == 400, f"status={resp.status_code}")

ENROLLED = False
resp = student_client.post(
    f"/api/v1/assignments/{ASSIGNMENT_ID}/export-challenge",
    json={"question_id": "q1"})
check("export challenge is enrollment-gated",
      resp.status_code == 403 and resp.get_json().get("code") == "not_enrolled",
      f"status={resp.status_code}")
ENROLLED = True

# ---------------------------------------------------------------------------
# Part 3 — snapshot enforcement at the submission boundary
# ---------------------------------------------------------------------------

CALLS.clear()
nonce, _ = documents.mint_export_nonce(ASSIGNMENT_ID, "q1")
resp = student_client.post("/api/v1/submissions", json={
    "assignment_id": ASSIGNMENT_ID,
    "course_id": "course-1",
    "responses": [snapshot_with(nonce),
                  {"question_id": "q2", "response_text": "My answer"}],
})
check("create accepts a nonce-stamped snapshot + text answer",
      resp.status_code == 201,
      f"status={resp.status_code} {resp.get_data(as_text=True)[:200]}")
stored = last_call("submissions.new")
snap = stored[2][0] if stored else {}
check("stored snapshot is sanitised (script stripped)",
      bool(stored) and "<script" not in snap["response_data"]["html"])
check("stored snapshot is canonicalised (question_type set, no text mirror)",
      snap.get("question_type") == "document"
      and "response_text" not in snap)
check("text answer passes through byte-identical",
      bool(stored) and stored[2][1] == {"question_id": "q2",
                                        "response_text": "My answer"})
new_submission_id = resp.get_json().get("id") if resp.status_code == 201 else None

CALLS.clear()
stale_nonce, _ = documents._mint("export", ASSIGNMENT_ID, "q1", -10)
resp = student_client.patch(f"/api/v1/submissions/{new_submission_id}",
                            json={"responses": [snapshot_with(stale_nonce)]})
check("stale nonce answers 422 invalid_snapshot",
      resp.status_code == 422
      and resp.get_json()["error"]["code"] == "invalid_snapshot",
      f"status={resp.status_code}")

CALLS.clear()
wrong_q_nonce, _ = documents.mint_export_nonce(ASSIGNMENT_ID, "q2")
resp = student_client.patch(f"/api/v1/submissions/{new_submission_id}",
                            json={"responses": [snapshot_with(wrong_q_nonce)]})
check("nonce minted for another question answers 422",
      resp.status_code == 422, f"status={resp.status_code}")

resp = student_client.patch(
    f"/api/v1/submissions/{new_submission_id}",
    json={"responses": [{"question_id": "q1",
                         "response_text": "typed, not exported"}]})
check("text-only answer to a document question answers 422",
      resp.status_code == 422, f"status={resp.status_code}")

resp = student_client.patch(
    f"/api/v1/submissions/{new_submission_id}",
    json={"responses": [{"question_id": "q1",
                         "response_data": {"html": DOC_HTML}}]})
check("unstamped snapshot answers 422",
      resp.status_code == 422, f"status={resp.status_code}")

CALLS.clear()
fresh_nonce, _ = documents.mint_export_nonce(ASSIGNMENT_ID, "q1")
resp = student_client.patch(f"/api/v1/submissions/{new_submission_id}",
                            json={"responses": [snapshot_with(fresh_nonce)]})
recorded = last_call("submissions.update")
check("PATCH with a fresh nonce forwards a sanitised full replace",
      resp.status_code == 200 and bool(recorded)
      and "<script" not in recorded[2]["responses"][0]["response_data"]["html"]
      and recorded[2]["responses"][0]["response_data"]["html"].count("<h1>") == 1,
      f"status={resp.status_code}")

resp = student_client.patch(f"/api/v1/submissions/{new_submission_id}",
                            json={"responses": [
                                {"question_id": "q2",
                                 "response_text": "Edited answer"}]})
recorded = last_call("submissions.update")
check("text-only full replace is unaffected (byte-compat)",
      resp.status_code == 200 and bool(recorded)
      and recorded[2]["responses"] == [
          {"question_id": "q2", "response_text": "Edited answer"}],
      f"status={resp.status_code}")

# ---------------------------------------------------------------------------
# Part 4 — /a/ share page rendering
# ---------------------------------------------------------------------------

os.environ["EMBED_ORIGIN"] = DOCS_ORIGIN
resp = student_client.get(f"/a/{ASSIGNMENT_ID}")
page = resp.get_data(as_text=True)
check("document question renders an iframe to the docs deployment",
      resp.status_code == 200
      and f'src="{DOCS_ORIGIN}/embed/{ASSIGNMENT_ID}/q1?t=' in page,
      f"status={resp.status_code}")
check("the iframe is sandboxed allow-scripts allow-same-origin",
      'sandbox="allow-scripts allow-same-origin"' in page)
check("doc HTML is never inlined into the page", "alert(1)" not in page)
check("text question still renders a textarea",
      'data-qid="q2"' in page and "answer-input" in page)

os.environ.pop("EMBED_ORIGIN", None)
resp = student_client.get(f"/a/{ASSIGNMENT_ID}")
page = resp.get_data(as_text=True)
check("without EMBED_ORIGIN the doc question degrades to a notice",
      resp.status_code == 200 and "not configured" in page
      and "/embed/" not in page)

# Unsupported type rendering (campus may know more than this app does).
ASSIGNMENTS[ASSIGNMENT_ID].questions.append(
    FakeQuestion("q9", "parsons", data={"blocks": []}))
resp = student_client.get(f"/a/{ASSIGNMENT_ID}")
page = resp.get_data(as_text=True)
check("unsupported question type renders a notice, not a wrong input",
      resp.status_code == 200 and "not supported in this app yet" in page)
ASSIGNMENTS[ASSIGNMENT_ID].questions.pop()

os.environ["EMBED_ORIGIN"] = DOCS_ORIGIN

# ---------------------------------------------------------------------------
# Part 5 — /embed on the docs deployment
# ---------------------------------------------------------------------------

resp = student_client.get(f"/embed/{ASSIGNMENT_ID}/q1")
check("/embed 404s without EMBED_MODE on this deployment",
      resp.status_code == 404, f"status={resp.status_code}")

os.environ["EMBED_MODE"] = "1"
os.environ["EMBED_PARENT_ORIGIN"] = "http://localhost:5000"

# campus-apphost containment (#81): EMBED_MODE deployments serve
# /embed and nothing else — no login, no API, no share pages.
check("apphost mode refuses every non-embed route",
      student_client.get("/").status_code == 404
      and student_client.get(f"/a/{ASSIGNMENT_ID}").status_code == 404
      and student_client.get("/api/v1/assignments").status_code == 404
      and student_client.get("/sign-in").status_code == 404,
      "GET / /a/{id} /api/v1/assignments /sign-in")

resp = student_client.get(f"/embed/{ASSIGNMENT_ID}/q1")
check("/embed 403s without a token", resp.status_code == 403,
      f"status={resp.status_code}")

good_token = documents.mint_embed_token(ASSIGNMENT_ID, "q1")
resp = student_client.get(
    f"/embed/{ASSIGNMENT_ID}/q1?t={good_token}")
body = resp.get_data(as_text=True)
check("valid token serves the document",
      resp.status_code == 200 and "<h1>Explore</h1>" in body,
      f"status={resp.status_code}")
check("the export runtime is injected with its config",
      "CAMPUS_EMBED" in body and '"qid": "q1"' in body
      and "campus-export" in body)
check("runtime scripts are the only additions (page CSP frame-ancestors)",
      resp.headers.get("Content-Security-Policy", "")
      == "frame-ancestors http://localhost:5000; base-uri 'none'; "
         "form-action 'none'"
      and resp.headers.get("Cache-Control") == "no-store",
      resp.headers.get("Content-Security-Policy", ""))

resp = student_client.get(
    f"/embed/{ASSIGNMENT_ID}/q1?t={documents.mint_embed_token(ASSIGNMENT_ID, 'q2')}")
check("a token for another question 403s", resp.status_code == 403,
      f"status={resp.status_code}")

ghost_token = documents.mint_embed_token(ASSIGNMENT_ID, "q_ghost")
resp = student_client.get(f"/embed/{ASSIGNMENT_ID}/q_ghost?t={ghost_token}")
check("a valid token for an unknown question 404s",
      resp.status_code == 404, f"status={resp.status_code}")

os.environ.pop("EMBED_MODE", None)
os.environ.pop("EMBED_PARENT_ORIGIN", None)
check("without EMBED_MODE the full app serves again",
      student_client.get(f"/a/{ASSIGNMENT_ID}").status_code == 200)

# ---------------------------------------------------------------------------
print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    raise SystemExit(1)
print("All checks passed.")
