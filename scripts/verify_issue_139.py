"""Verification for issue #139: Campus API failures must never surface
as a raw 500 + the answer sheet's bare fallback banner.

The 2026-11-22 prod incident: a student's submission create died on the
missing `submissions.admin` circle grant (campus#1196) as an uncaught
AccessDeniedError — Flask 500 HTML — so /a/'s apiError had no JSON to
parse and showed only "Could not start your submission". The fix is the
app-wide CampusAPIError handler (campus_session.register_error_handlers,
a sibling of the ClassroomAuthError handler). No network and no real
Campus are involved — the Campus client is faked at the session seams
and the enrollment bridge is stubbed (it is #28/#14's own concern):

1. The incident shape: AccessDeniedError from submissions.new on POST
   /api/v1/submissions answers 403 campus_access_denied JSON with the
   friendly grant message and the SDK request_id (was: 500).
2. Status mapping: ServerError -> 502 campus_api_unavailable (retry
   message), RateLimitExceededError -> 429 campus_rate_limited,
   ValidationError -> 422 passthrough with the field-error list
   formatApiError renders.
3. The #98 contract survives: an AccessDeniedError that
   is_reauth_required flags (sessions:read) answers 401
   campus_session_expired JSON, and browser paths redirect into
   /login?next= — the handler never swallows the seam's semantics.
4. Browser paths flash + redirect back (referrer, else /) instead of
   500ing the page render.
5. Hook-phase failures (#126's class): an AccessDeniedError raised in a
   before_request hook — flask_campus's context push lives there — is
   funnelled through Flask's handle_exception, so the handler answers
   it too; the "no app-level handler sees it" assumption in #126 does
   not hold.
6. Non-SDK exceptions still surface as 500 (behavior preserved).

Usage: .venv/Scripts/python.exe scripts/verify_issue_139.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-139-secret"

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402
from campus_python.errors import FieldError  # noqa: E402

from apps.classroom import create_app, enrollment  # noqa: E402

TEACHER = "uid-user-teacher"

DENIED_403 = campus_errors.AccessDeniedError(
    403,
    "Requires the submissions.admin circle (create submissions)",
    request_id="req-incident-139",
)
SESSIONS_READ_DENIED = campus_errors.AccessDeniedError(
    403, "No access grant designates this user for 'sessions:read' (#883)")

# The enrollment bridge is #28/#14's concern, not the error handler's:
# grant every caller enrolled/reviewer status so the SDK seam is what
# each scenario exercises.
enrollment.is_enrolled = lambda _assignment: True
enrollment.can_review = lambda _assignment, _user_id: True


class _Assignments:
    """client.api.assignments stand-in serving one scripted assignment."""

    def __init__(self, assignments):
        self._assignments = assignments

    def __getitem__(self, assignment_id):
        if assignment_id not in self._assignments:
            raise campus_errors.NotFoundError(404, "Assignment not found")
        record = self._assignments[assignment_id]
        return SimpleNamespace(get=lambda: record)


class _ScriptedAssignments:
    """Optional override of what assignments[id].get() raises/returns."""

    def __init__(self, get_result):
        self._get_result = get_result

    def __getitem__(self, _assignment_id):
        return SimpleNamespace(get=self._get_result)


class FakeCampus:
    """campus_python.Campus stand-in: working sessions, scripted SDK."""

    def __init__(self):
        self._assignments_get = None
        self._submissions_new = lambda **_kw: None

    @contextmanager
    def with_app_session(self):
        yield self._client()

    @contextmanager
    def with_user_session(self):
        yield self._client()

    def _client(self):
        assignments = (
            _ScriptedAssignments(self._assignments_get)
            if self._assignments_get is not None
            else _Assignments(ASSIGNMENTS)
        )
        return SimpleNamespace(
            api=SimpleNamespace(
                assignments=assignments,
                submissions=SimpleNamespace(new=self._submissions_new)),
        )


ASSIGNMENTS = {
    "a1": SimpleNamespace(
        id="a1", created_by=TEACHER, title="Algebra Worksheet",
        description="", questions=[], classroom_links=[
            SimpleNamespace(course_id="course-1", coursework_id="cw-1"),
        ],
        created_at="2026-11-09T00:00:00Z",
    ),
}


def make_client(campus, before_request=None):
    """Fresh app per scenario: Flask forbids hook setup after request 1."""
    app = create_app()
    app.campus = campus
    campus_python.Campus = lambda timeout=None: campus  # noqa: ARG005
    if before_request is not None:
        app.before_request(before_request)
    client = app.test_client()
    for method in ("post", "patch", "put", "delete"):
        original = getattr(client, method)

        def wrapper(path, *a, _orig=original, **kw):
            headers = kw.pop("headers", None) or {}
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            kw["headers"] = headers
            return _orig(path, *a, **kw)

        setattr(client, method, wrapper)
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": TEACHER, "email": f"{TEACHER}@nyjc.edu.sg"}
    return client


def _fake_push_context(*_args):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond:
        failures.append(name)


CREATE_BODY = {"assignment_id": "a1", "course_id": "course-1", "responses": []}


def raises(err):
    return lambda **_kw: (_ for _ in ()).throw(err)


# --- 1. the incident shape ----------------------------------------------------
print("== AccessDeniedError on submission create (the 2026-11-22 prod shape) ==")

denied = FakeCampus()
denied._submissions_new = raises(DENIED_403)
client = make_client(denied)

r = client.post("/api/v1/submissions", json=CREATE_BODY)
payload = r.get_json(silent=True) or {}
err = payload.get("error", {})
check("denied submission create -> 403 campus_access_denied (not 500)",
      r.status_code == 403 and err.get("code") == "campus_access_denied",
      f"status={r.status_code} payload={payload}")
check("friendly grant message (answer sheet renders it verbatim)",
      "access grant" in err.get("message", ""))
check("SDK request_id surfaced for support/diagnosis",
      err.get("request_id") == "req-incident-139")

# --- 2. status mapping ----------------------------------------------------------
print("== status mapping ==")

down = FakeCampus()
down._submissions_new = raises(
    campus_errors.ServerError(500, "mongo unavailable"))
client = make_client(down)
r = client.post("/api/v1/submissions", json=CREATE_BODY)
payload = r.get_json(silent=True) or {}
check("ServerError -> 502 campus_api_unavailable with retry message",
      r.status_code == 502
      and payload.get("error", {}).get("code") == "campus_api_unavailable"
      and "try again" in payload.get("error", {}).get("message", ""),
      f"status={r.status_code} payload={payload}")

limited = FakeCampus()
limited._submissions_new = raises(
    campus_errors.RateLimitExceededError(429, "slow down"))
client = make_client(limited)
r = client.post("/api/v1/submissions", json=CREATE_BODY)
payload = r.get_json(silent=True) or {}
check("RateLimitExceededError -> 429 campus_rate_limited",
      r.status_code == 429
      and payload.get("error", {}).get("code") == "campus_rate_limited",
      f"status={r.status_code} payload={payload}")

bad_payload = FakeCampus()
bad_payload._submissions_new = raises(
    campus_errors.ValidationError(
        422, "responses: invalid",
        errors=[FieldError("responses", "INVALID_FORMAT", "must be a list")]))
client = make_client(bad_payload)
r = client.post("/api/v1/submissions", json=CREATE_BODY)
payload = r.get_json(silent=True) or {}
check("ValidationError -> 422 passthrough incl. field errors",
      r.status_code == 422
      and payload.get("error", {}).get("code") == "campus_validation_failed"
      and payload.get("error", {}).get("errors")
      and payload["error"]["errors"][0]["field"] == "responses",
      f"status={r.status_code} payload={payload}")

# --- 3. the #98 contract survives ------------------------------------------------
print("== reauth-shaped denials keep the #98 contract ==")

reauth = FakeCampus()
reauth._submissions_new = raises(SESSIONS_READ_DENIED)
client = make_client(reauth)
r = client.post("/api/v1/submissions", json=CREATE_BODY)
payload = r.get_json(silent=True) or {}
check("sessions:read denial -> 401 campus_session_expired (not 403)",
      r.status_code == 401
      and payload.get("error", {}).get("code") == "campus_session_expired",
      f"status={r.status_code} payload={payload}")


def _raise_reauth_denial():
    if flask.request.path == "/dashboard":
        raise SESSIONS_READ_DENIED


client = make_client(FakeCampus(), before_request=_raise_reauth_denial)
r = client.get("/dashboard")
loc = r.headers.get("Location", "")
check("reauth-shaped denial on a browser path -> 302 into /login?next=",
      r.status_code == 302 and loc.startswith("/login?next="),
      f"status={r.status_code} location={loc!r}")

# --- 4. browser paths ------------------------------------------------------------
print("== browser paths flash + redirect back ==")

# /assignments/<id>/submissions hits the SDK unconditionally (unlike the
# /assignments/<id> view, whose embed block is EMBED_ORIGIN-gated).
denied_page = FakeCampus()
denied_page._assignments_get = raises(DENIED_403)
client = make_client(denied_page)
r = client.get("/assignments/a1/submissions")
with client.session_transaction() as sess:
    flashes = sess.get("_flashes") or []
check("denied page render -> 302 back to / (not 500), flash carries message",
      r.status_code == 302 and r.headers.get("Location", "").endswith("/")
      and any("access grant" in message for _, message in flashes),
      f"status={r.status_code} location={r.headers.get('Location')!r} "
      f"flashes={flashes}")

# --- 5. hook-phase failures (#126's class) ---------------------------------------
print("== before_request-hook denials reach the handler (#126) ==")


def _raise_hook_denial():
    if flask.request.path.startswith("/api/v1/"):
        raise DENIED_403


client = make_client(FakeCampus(), before_request=_raise_hook_denial)
r = client.get("/api/v1/assignments")
payload = r.get_json(silent=True) or {}
check("hook-phase AccessDeniedError -> 403 JSON (not 500)",
      r.status_code == 403
      and payload.get("error", {}).get("code") == "campus_access_denied",
      f"status={r.status_code} payload={payload}")

# --- 6. non-SDK exceptions still 500 ---------------------------------------------
print("== non-SDK exceptions keep 500ing ==")


def _raise_runtime_error():
    if flask.request.path == "/dashboard":
        raise RuntimeError("genuinely unexpected")


client = make_client(FakeCampus(), before_request=_raise_runtime_error)
r = client.get("/dashboard")
check("RuntimeError on a browser path -> still 500 (behavior preserved)",
      r.status_code == 500, f"status={r.status_code}")

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("All #139 checks passed.")
