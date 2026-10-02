"""Verification for issue #10: Send to Google Classroom (PRD §6.7, GC-8).

Runs the send-to-Classroom flow end-to-end against a local stub of the
Classroom REST API plus an in-memory fake Campus store (no network, no
real Google account):

1. Course picker source: GET /api/v1/classroom/courses lists the teacher's
   courses (courses.list, teacherMe).
2. Eligible multi-class send: two courses -> draft CourseWork (no
   materials) + add-on attachment each, view URIs point at the deployed
   origin, classroom_links stores course/coursework/attachment ids, and
   the assignment locks for editing (PATCH/DELETE -> 409).
3. Ineligible teacher (capability says no, or the preview API errors):
   CourseWork carries a Link Material to /a/{assignment_id}; link stored
   without attachment_id.
4. Eligible check passes but attachment creation is refused (app not
   Marketplace-approved yet): best-effort Link Material patch; link stored
   without attachment_id; outcome reports the degradation.
5. Partial failure: one course 403s, the other still posts.
6. Re-send: already-linked courses report already_linked.
7. Not connected / missing coursework-students scope: 403 JSON with
   authorize_url; the incremental authorize URL requests exactly the
   missing scope (allowlist includes the feature scope).
8. Input validation (empty course_ids -> 400) and ownership (403).
9. /a/{assignment_id} renders publicly (no Campus login), shows the
   assignment, sends no X-Frame-Options header; unknown id -> 404.
10. view.html renders the picker modal; the alert() stub is gone.

Usage: .venv/Scripts/python.exe scripts/verify_issue_10.py
"""

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-10-secret"
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-issue-10-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-issue-10-client-secret")
os.environ["WORKSPACE_DOMAIN"] = "nyjc.edu.sg"

import flask  # noqa: E402

import campus_python  # noqa: E402
import campus_python.auth.v1 as campus_auth_v1  # noqa: E402
from campus.model import Assignment, ClassroomLink  # noqa: E402

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402

CAMPUS_EMAIL = "teacher@nyjc.edu.sg"
OTHER_EMAIL = "other@nyjc.edu.sg"

SCOPE_BASE = "https://www.googleapis.com/auth/"
ALL_SCOPES = list(cauth.CLASSROOM_SCOPES_MVP) + list(cauth.CLASSROOM_SCOPES_SEND)
SEND_SCOPE = cauth.CLASSROOM_SCOPES_SEND[0]
ORIGIN = "http://localhost:5000"

COURSES_PAYLOAD = {
    "courses": [
        {"id": "course_111", "name": "CS1101s", "section": "25S1-01"},
        {"id": "course_222", "name": "Computing Plus", "section": "25S1-02"},
    ]
}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Stub Google Classroom REST API (scriptable per-scenario behaviour)
# ---------------------------------------------------------------------------
class StubClassroom(BaseHTTPRequestHandler):
    """Minimal Classroom REST endpoints with scriptable outcomes."""

    capability_allowed: bool = True
    capability_errors: bool = False        # preview API unavailable -> 403
    coursework_403: set = set()            # course ids whose create 403s
    attachment_403: set = set()            # course ids whose attachment 403s
    patch_403: set = set()                 # course ids whose patch 403s

    coursework_creates: list = []          # (course_id, body)
    attachment_creates: list = []          # (course_id, coursework_id, body)
    coursework_patches: list = []          # (course_id, coursework_id, body)

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else "{}"
        return json.loads(raw or "{}")

    def do_GET(self):
        if urlsplit(self.path).path == "/v1/courses":
            self._send(COURSES_PAYLOAD)
        else:
            self._send({"error": {"code": 404, "message": "Not Found"}}, 404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.endswith(":checkUserCapability"):
            if self.capability_errors:
                self._send(
                    {"error": {"code": 403,
                               "message": "Preview capability unavailable"}}, 403)
                return
            self._send({"allowed": self.capability_allowed})
            return

        m = re.fullmatch(r"/v1/courses/([^/]+)/courseWork", path)
        if m:
            course_id = m.group(1)
            if course_id in self.coursework_403:
                self._send(
                    {"error": {"code": 403, "message": "Permission denied"}}, 403)
                return
            body = self._read_json()
            self.coursework_creates.append((course_id, body))
            self._send({"id": f"cw_{len(self.coursework_creates)}",
                        "courseId": course_id, **body})
            return

        m = re.fullmatch(r"/v1/courses/([^/]+)/courseWork/([^/]+)/addOnAttachments", path)
        if m:
            course_id, coursework_id = m.group(1), m.group(2)
            if course_id in self.attachment_403:
                self._send(
                    {"error": {"code": 403,
                               "message": "Developer project not approved"}}, 403)
                return
            body = self._read_json()
            self.attachment_creates.append((course_id, coursework_id, body))
            self._send({"id": f"att_{len(self.attachment_creates)}",
                        "courseId": course_id, **body})
            return

        self._send({"error": {"code": 404, "message": "Not Found"}}, 404)

    def do_PATCH(self):
        path = urlsplit(self.path).path
        m = re.fullmatch(r"/v1/courses/([^/]+)/courseWork/([^/]+)", path)
        if m:
            course_id, coursework_id = m.group(1), m.group(2)
            if course_id in self.patch_403:
                self._send(
                    {"error": {"code": 403, "message": "Permission denied"}}, 403)
                return
            body = self._read_json()
            self.coursework_patches.append((course_id, coursework_id, body))
            self._send({"id": coursework_id, "courseId": course_id, **body})
            return
        self._send({"error": {"code": 404, "message": "Not Found"}}, 404)


server = ThreadingHTTPServer(("127.0.0.1", 0), StubClassroom)
threading.Thread(target=server.serve_forever, daemon=True).start()

# Point the auth bridge at the stub (module-level constant, monkeypatched).
cauth.CLASSROOM_API_BASE = f"http://127.0.0.1:{server.server_address[1]}"


def reset_stub():
    StubClassroom.capability_allowed = True
    StubClassroom.capability_errors = False
    StubClassroom.coursework_403 = set()
    StubClassroom.attachment_403 = set()
    StubClassroom.patch_403 = set()
    StubClassroom.coursework_creates = []
    StubClassroom.attachment_creates = []
    StubClassroom.coursework_patches = []


# ---------------------------------------------------------------------------
# Fake Campus store (assignments + links) wired into with_user/_app_session
# ---------------------------------------------------------------------------
class FakeCampusStore:
    def __init__(self):
        self.assignments: dict = {}

    def new_assignment(self, assignment_id, *, created_by, title="Verify me",
                       description="desc", classroom_links=None):
        self.assignments[assignment_id] = Assignment(
            id=assignment_id,
            title=title,
            description=description,
            questions=[{"id": "1", "prompt": "p", "question": "What is 2+2?"}],
            created_by=created_by,
            classroom_links=list(classroom_links or []),
        )
        return self.assignments[assignment_id]


STORE = FakeCampusStore()


class FakeLinks:
    def __init__(self, assignment):
        self._assignment = assignment

    def add(self, *, course_id, coursework_id, attachment_id=None):
        self._assignment.classroom_links.append(
            ClassroomLink(course_id=course_id, coursework_id=coursework_id,
                          attachment_id=attachment_id))


class FakeAssignmentHandle:
    def __init__(self, assignment_id):
        self._id = assignment_id

    def get(self):
        if self._id not in STORE.assignments:
            raise RuntimeError("assignment not found")
        return STORE.assignments[self._id]

    def update(self, **updates):
        assignment = self.get()
        for key, value in updates.items():
            setattr(assignment, key, value)

    def delete(self):
        STORE.assignments.pop(self._id, None)

    @property
    def links(self):
        return FakeLinks(self.get())


class FakeAssignments:
    def __getitem__(self, assignment_id):
        return FakeAssignmentHandle(assignment_id)


FAKE_CAMPUS = SimpleNamespace(api=SimpleNamespace(assignments=FakeAssignments()))


@contextmanager
def fake_user_session(self):
    yield FAKE_CAMPUS


APP_SESSION_OK = True  # toggled to simulate deployments without the grant


@contextmanager
def fake_app_session(self):
    if not APP_SESSION_OK:
        raise RuntimeError("campus auth: no client_credentials grant")
    yield FAKE_CAMPUS


campus_python.Campus.with_user_session = fake_user_session
campus_python.Campus.with_app_session = fake_app_session


# App with a faked Campus login (push_context reads verify_user from session)
def _fake_push_context(self):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


campus_auth_v1.AuthRoot.push_context = _fake_push_context

app = create_app()


def seed_user(client, email=CAMPUS_EMAIL):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": email, "email": email}


def seed_credentials(client, scopes, *, expires_at=None):
    with client.session_transaction() as sess:
        sess["classroom_credentials"] = {
            "access_token": "seeded-access",
            "refresh_token": "seeded-refresh",
            "expires_at": expires_at if expires_at is not None else time.time() + 3600,
            "scopes": list(scopes),
            "email": CAMPUS_EMAIL,
        }


def stored_links(assignment_id):
    return STORE.assignments[assignment_id].classroom_links


STORE.new_assignment("a_eligible", created_by=CAMPUS_EMAIL)
STORE.new_assignment("a_ineligible", created_by=CAMPUS_EMAIL)
STORE.new_assignment("a_degraded", created_by=CAMPUS_EMAIL)
STORE.new_assignment("a_degraded2", created_by=CAMPUS_EMAIL)
STORE.new_assignment("a_partial", created_by=CAMPUS_EMAIL)
STORE.new_assignment("a_foreign", created_by=OTHER_EMAIL)

client = app.test_client()

# ---------------------------------------------------------------------------
# 1. Course picker source
# ---------------------------------------------------------------------------
seed_user(client)
seed_credentials(client, ALL_SCOPES)
resp = client.get("/api/v1/classroom/courses")
check("courses endpoint returns 200", resp.status_code == 200, str(resp.status_code))
courses = resp.get_json()["courses"]
check("courses endpoint lists the teacher's courses",
      [c["id"] for c in courses] == ["course_111", "course_222"]
      and courses[0]["name"] == "CS1101s",
      json.dumps(courses))

# ---------------------------------------------------------------------------
# 2. Eligible multi-class send: attachment path + GC-8 storage + lock
# ---------------------------------------------------------------------------
resp = client.post(
    "/api/v1/assignments/a_eligible/classroom/send",
    json={"course_ids": ["course_111", "course_222"]},
)
check("eligible send returns 200", resp.status_code == 200, str(resp.status_code))
results = resp.get_json()["results"]
check("both courses report attached",
      [r["status"] for r in results] == ["attached", "attached"],
      json.dumps(results))

creates = StubClassroom.coursework_creates
check("one draft CourseWork per class",
      [c[0] for c in creates] == ["course_111", "course_222"],
      str(creates))
check("CourseWork is created as a draft with the assignment title",
      all(c[1]["state"] == "DRAFT" and c[1]["title"] == "Verify me" for c in creates))
check("eligible CourseWork carries no materials",
      all("materials" not in c[1] for c in creates))

attachments = StubClassroom.attachment_creates
check("one add-on attachment per class",
      [a[0] for a in attachments] == ["course_111", "course_222"])
check("attachment view URIs point at the deployed origin",
      all(
          a[2]["teacherViewUri"]["uri"] == f"{ORIGIN}/addon/teacher"
          and a[2]["studentViewUri"]["uri"] == f"{ORIGIN}/addon/student"
          and a[2]["studentWorkReviewUri"]["uri"] == f"{ORIGIN}/addon/review"
          for a in attachments),
      json.dumps(attachments))

links = stored_links("a_eligible")
check("classroom_links stores every class (GC-8)",
      {l.course_id for l in links} == {"course_111", "course_222"}
      and all(l.coursework_id and l.attachment_id for l in links),
      str(links))
check("outcome carries coursework + attachment ids",
      all(r["coursework_id"] and r["attachment_id"] for r in results))

resp = client.patch(
    "/api/v1/assignments/a_eligible", json={"title": "edited"})
check("assignment locks after posting (PATCH -> 409)", resp.status_code == 409)
resp = client.delete("/api/v1/assignments/a_eligible")
check("assignment locks after posting (DELETE -> 409)", resp.status_code == 409)

resp = client.post(
    "/api/v1/assignments/a_eligible/classroom/send",
    json={"course_ids": ["course_111", "course_222"]},
)
statuses = [r["status"] for r in resp.get_json()["results"]]
check("re-send reports already_linked for posted classes",
      resp.status_code == 200 and statuses == ["already_linked", "already_linked"],
      str(statuses))
check("re-send creates no new CourseWork",
      len(StubClassroom.coursework_creates) == 2)

# ---------------------------------------------------------------------------
# 3. Ineligible teacher -> Link Material fallback
# ---------------------------------------------------------------------------
reset_stub()
StubClassroom.capability_allowed = False
resp = client.post(
    "/api/v1/assignments/a_ineligible/classroom/send",
    json={"course_ids": ["course_111"]},
)
check("ineligible send returns 200", resp.status_code == 200)
results = resp.get_json()["results"]
check("ineligible course reports linked (fallback)",
      results[0]["status"] == "linked" and results[0]["attachment_id"] is None,
      json.dumps(results))
creates = StubClassroom.coursework_creates
check("fallback CourseWork embeds a Link Material to /a/{id}",
      len(creates) == 1
      and creates[0][1]["materials"] == [{
          "link": {"url": f"{ORIGIN}/a/a_ineligible",
                   "title": "Verify me"}}],
      json.dumps(creates))
check("fallback stores no attachment ids",
      len(StubClassroom.attachment_creates) == 0)
links = stored_links("a_ineligible")
check("fallback link stored without attachment_id (GC-8)",
      len(links) == 1 and links[0].coursework_id and links[0].attachment_id is None)

# Preview API unavailable behaves the same (graceful degradation)
reset_stub()
StubClassroom.capability_errors = True
resp = client.post(
    "/api/v1/assignments/a_ineligible/classroom/send",
    json={"course_ids": ["course_222"]},
)
results = resp.get_json()["results"]
check("capability-check errors degrade to the fallback too",
      resp.status_code == 200 and results[0]["status"] == "linked",
      json.dumps(results))
check("capability errors add the Link Material",
      StubClassroom.coursework_creates[0][1]["materials"][0]["link"]["url"]
      == f"{ORIGIN}/a/a_ineligible")

# ---------------------------------------------------------------------------
# 4. Eligible but attachment refused -> best-effort material patch
# ---------------------------------------------------------------------------
reset_stub()
StubClassroom.attachment_403 = {"course_111"}
resp = client.post(
    "/api/v1/assignments/a_degraded/classroom/send",
    json={"course_ids": ["course_111"]},
)
check("attachment-refused send returns 200", resp.status_code == 200)
results = resp.get_json()["results"]
check("attachment refusal degrades to linked (not error)",
      results[0]["status"] == "linked" and results[0]["attachment_id"] is None,
      json.dumps(results))
check("refusal mentions the degradation",
      "refused" in results[0]["message"], results[0]["message"])
patches = StubClassroom.coursework_patches
check("existing draft patched with the Link Material",
      len(patches) == 1
      and patches[0][0] == "course_111"
      and patches[0][2]["materials"][0]["link"]["url"] == f"{ORIGIN}/a/a_degraded",
      str(patches))

# ... and if even the patch is refused, still no error and no attachment_id
reset_stub()
StubClassroom.attachment_403 = {"course_111"}
StubClassroom.patch_403 = {"course_111"}
resp = client.post(
    "/api/v1/assignments/a_degraded2/classroom/send",
    json={"course_ids": ["course_111"]},
)
results = resp.get_json()["results"]
check("fully-refused course still stores the coursework link",
      resp.status_code == 200
      and results[0]["status"] == "linked"
      and results[0]["coursework_id"]
      and results[0]["attachment_id"] is None
      and len(stored_links("a_degraded2")) == 1,
      json.dumps(results))

# ---------------------------------------------------------------------------
# 5. Partial failure: one class errors, the other posts
# ---------------------------------------------------------------------------
reset_stub()
StubClassroom.coursework_403 = {"course_222"}
resp = client.post(
    "/api/v1/assignments/a_partial/classroom/send",
    json={"course_ids": ["course_111", "course_222"]},
)
check("partial-failure send returns 200", resp.status_code == 200)
results = resp.get_json()["results"]
by_course = {r["course_id"]: r["status"] for r in results}
check("healthy class posts, failing class reports error",
      by_course == {"course_111": "attached", "course_222": "error"},
      json.dumps(results))
check("failing class message is user-safe",
      "failed" in results[1]["message"].lower(), results[1]["message"])
check("only the healthy class is linked",
      len(stored_links("a_partial")) == 1)

# ---------------------------------------------------------------------------
# 6. Not connected / missing scope -> structured 403 JSON
# ---------------------------------------------------------------------------
client_noc = app.test_client()
seed_user(client_noc)
resp = client_noc.post(
    "/api/v1/assignments/a_eligible/classroom/send",
    json={"course_ids": ["course_111"]},
)
check("not-connected send returns 403 JSON",
      resp.status_code == 403
      and resp.get_json()["error"]["code"] == "classroom_not_connected")

client_scope = app.test_client()
seed_user(client_scope)
seed_credentials(client_scope, cauth.CLASSROOM_SCOPES_MVP)  # no write scope
resp = client_scope.post(
    "/api/v1/assignments/a_eligible/classroom/send",
    json={"course_ids": ["course_111"]},
)
check("missing write scope returns 403 JSON", resp.status_code == 403)
err = resp.get_json()["error"]
check("missing scope names classroom.coursework.students",
      err["code"] == "classroom_missing_scopes"
      and err["missing_scopes"] == [SEND_SCOPE],
      json.dumps(err))
check("error carries an incremental authorize_url",
      "/classroom/authorize" in err["authorize_url"]
      and SEND_SCOPE in err["authorize_url"], err["authorize_url"])

# The authorize route allowlists the feature scope for incremental consent
from urllib.parse import urlencode  # noqa: E402

resp = client_scope.get(
    f"/classroom/authorize?{urlencode({'scopes': SEND_SCOPE})}")
loc = resp.headers["Location"]
params = {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}
check("incremental authorize requests the write scope (+ identity)",
      set(params["scope"].split())
      == {SEND_SCOPE} | set(cauth.GOOGLE_IDENTITY_SCOPES),
      params.get("scope", ""))

# ---------------------------------------------------------------------------
# 7. Input validation + ownership
# ---------------------------------------------------------------------------
resp = client.post(
    "/api/v1/assignments/a_eligible/classroom/send", json={"course_ids": []})
check("empty course_ids -> 400", resp.status_code == 400)
resp = client.post(
    "/api/v1/assignments/a_eligible/classroom/send", json={"course_ids": "x"})
check("non-list course_ids -> 400", resp.status_code == 400)
resp = client.post(
    "/api/v1/assignments/a_foreign/classroom/send",
    json={"course_ids": ["course_111"]})
check("non-owner send -> 403", resp.status_code == 403)
resp = client.post(
    "/api/v1/assignments/a_missing/classroom/send",
    json={"course_ids": ["course_111"]})
check("unknown assignment -> 404", resp.status_code == 404)

# ---------------------------------------------------------------------------
# 8. /a/{assignment_id}: public, render-only, iframe-friendly
# ---------------------------------------------------------------------------
client_public = app.test_client()  # deliberately no Campus login
resp = client_public.get("/a/a_eligible")
check("share page renders without login", resp.status_code == 200,
      str(resp.status_code))
html = resp.get_data(as_text=True)
check("share page shows the assignment content",
      "Verify me" in html and "What is 2+2?" in html)
check("share page sends no X-Frame-Options header",
      "X-Frame-Options" not in resp.headers,
      str(dict(resp.headers)))
resp = client_public.get("/a/does-not-exist")
check("unknown share id -> 404", resp.status_code == 404)

# Dev-deployment reality: with_app_session has no client_credentials grant
# there. The share page must fall back to the visitor's Campus session and
# send anonymous visitors to sign-in instead of erroring.
APP_SESSION_OK = False
resp = client_public.get("/a/a_eligible")
check("app-scope outage: anonymous share visit redirects to sign-in",
      resp.status_code == 302
      and resp.headers["Location"].endswith("/sign-in"),
      f"{resp.status_code} {resp.headers.get('Location')}")
client_signed_in = app.test_client()
seed_user(client_signed_in)
resp = client_signed_in.get("/a/a_eligible")
check("app-scope outage: signed-in visitor still sees the share page",
      resp.status_code == 200
      and "Verify me" in resp.get_data(as_text=True),
      str(resp.status_code))
APP_SESSION_OK = True

# ---------------------------------------------------------------------------
# 9. view.html renders the picker; the alert stub is gone
# ---------------------------------------------------------------------------
resp = client.get("/assignments/a_ineligible")
check("assignment view page renders", resp.status_code == 200)
html = resp.get_data(as_text=True)
check("view page embeds the course picker modal",
      "classroomPickerModal" in html and "openClassroomPicker" in html)
check("view page wires the send endpoint",
      "/classroom/send" in html)
check("the alert() placeholder is gone", "linkToClassroom" not in html)

# ---------------------------------------------------------------------------
server.shutdown()
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")
