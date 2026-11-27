"""Rendered-HTML sweep: no root-absolute URL literals survive rendering
(issue #170 item 1, the CI-enforced regression guard).

The blocker class: hardcoded host-absolute literals — fetch('/api/v1/…'),
href="/sign-in", href="/dashboard", '/login?next=' — bypass url_for, so
behind campus-gateway they hit the gateway root (404, or worse: another
app's route) instead of this deployment's namespace. Every page URL now
composes from server-computed bases: the context processor's
api_base/login_url/logout_url/script_root, rendered once as the
window.CAMPUS_* constants in base.html and the standalone addon iframes.

This test renders every main page through the REAL app factory in both
shapes:

  origin        — script_root empty; the globals must degrade to root
                  shapes (CAMPUS_API_BASE = "/api/v1", static at /static).
  gateway       — X-Forwarded-Prefix -> SCRIPT_NAME via PrefixFix; every
                  emitted URL must carry the /classroom (or /apps)
                  namespace.

and asserts the rendered HTML contains no root-absolute href/fetch
literals. The CAMPUS_* constant assignments themselves are exempt:
assertions match href=/fetch( syntax, not raw script text, so tojson'd
constant values never false-positive.

Network seams are faked the scripts/verify_issue_*.py way: a fake
push_context seeds the signed-in user from the flask session, and the
enrollment helpers plus the campus session are stubbed with in-memory
doubles (nothing leaves the process).

Run:  python tests/test_no_root_absolute_urls.py   (exit 1 on violation)
"""

import logging
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Same dummy environment as the CI smoke job; PUBLIC_URL is FORCED (not
# setdefault — the smoke job exports its own) so the request-host
# comparisons below are exact: origin.example is the canonical host and
# the #173 forward never steals a request. EMBED_ORIGIN stays unset so
# assignment_view skips its per-question embed reads; EMBED_MODE starts
# unset (the classroom deployment).
for key, value in {
    "SECRET_KEY": "test",
    "CLIENT_ID": "ci-dummy",
    "CLIENT_SECRET": "ci-dummy",
    "BASE_URL": "https://campus.test",
}.items():
    os.environ.setdefault(key, value)
ORIGIN = "https://origin.example"
os.environ["PUBLIC_URL"] = ORIGIN
os.environ.pop("EMBED_MODE", None)
os.environ.pop("EMBED_ORIGIN", None)

import campus.flask_campus.context as flask_context  # noqa: E402
import flask  # noqa: E402

from apps.classroom import campus_session, create_app, enrollment  # noqa: E402

logging.getLogger("apps.classroom").setLevel(logging.CRITICAL)

checks = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    checks.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got: {got!r}"))


# --- fake seams ---------------------------------------------------------------

USER = "uid-user-sweep"
ASSIGNMENT_ID = "uid-assignment-sweep"
SUBMISSION_ID = "uid-submission-sweep"

FAKE_ASSIGNMENT = SimpleNamespace(
    id=ASSIGNMENT_ID,
    title="Sweep assignment",
    created_by="uid-user-owner",
    questions=[],
    classroom_links=[],
    to_resource=lambda: {"questions": []},
)
FAKE_SUBMISSION = SimpleNamespace(
    id=SUBMISSION_ID,
    assignment_id=ASSIGNMENT_ID,
    student_id="uid-user-student",
    responses=[],
    feedback=[],
)
FAKE_APP_RECORD = SimpleNamespace(
    slug="sweep-app",
    name="Sweep App",
    size=1024,
    updated_at="2026-11-20T10:00:00",
    uploaded_by_user_id=USER,  # the sweep user, so apps_edit's owner gate passes
)


def _fake_push_context(*_args):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("sweep_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context


class FakeRepo:
    """client.api.<resource>[<id>].get() -> the one canned record."""

    def __init__(self, record):
        self._record = record

    def __getitem__(self, _key):
        record = self._record
        return SimpleNamespace(get=lambda: record)


class FakeHostedApps:
    def get_meta(self, _slug):
        return FAKE_APP_RECORD

    def list(self):
        return [FAKE_APP_RECORD]


FAKE_CLIENT = SimpleNamespace(
    api=SimpleNamespace(
        assignments=FakeRepo(FAKE_ASSIGNMENT),
        submissions=FakeRepo(FAKE_SUBMISSION),
        hostedapps=FakeHostedApps(),
    )
)


@contextmanager
def fake_session():
    yield FAKE_CLIENT


campus_session.user_session = fake_session
campus_session.app_session = fake_session
enrollment.taught_courses = lambda: {"course-1": {"name": "Course 1"}}
enrollment.can_review = lambda _assignment, _user_id: True
enrollment.enrolled_courses = lambda _assignment, **_kwargs: [
    {"course_id": "course-1", "name": "Course 1"}
]


def seed(client):
    # https base_url: the factory sets SESSION_COOKIE_SECURE for non-debug
    # deployments, and the test client only replays Secure cookies over
    # https. Every request below uses the same base_url so the seeded
    # session round-trips (and matches PUBLIC_URL's host, so the #173
    # forward never steals a request).
    with client.session_transaction(base_url=ORIGIN) as sess:
        sess["sweep_user"] = {"id": USER, "email": f"{USER}@nyjc.edu.sg"}


# --- the sweep ----------------------------------------------------------------

# Root-absolute literals, matched only where URL syntax places them —
# fetch-call first arguments and href/src/action attributes — never raw
# script text (the CAMPUS_* constants legitimately carry root shapes in
# their tojson'd values).
#
# fetch-literal patterns are forbidden in BOTH shapes: a literal
# fetch('/api/v1/…') is the blocker everywhere, while root-absolute
# hrefs are url_for's correct output at the origin (script_root empty)
# and forbidden only behind the gateway, where every emitted URL must
# carry the namespace.
FORBIDDEN_FETCH = [
    (r'fetch\(\s*["\'`]/api/v1', "root-absolute fetch"),
    (r'fetchJson\(\s*["\'`]/api/v1', "root-absolute fetchJson"),
    (r'\.assign\(\s*["\'`]/(?:api/v1|login)', "root-absolute assign"),
]

FORBIDDEN_ATTRS = [
    (r'(?:href|src|action)="(?:/api/v1|/login|/sign-in|/dashboard'
     r"|/assignments/|/a/|/submissions/|/static/)",
     "root-absolute attribute"),
]

ROUTE_PAGES = [
    # "/" is not here: the index view redirects signed-in visitors to the
    # dashboard (portal entry), so it is swept as a direct template render
    # in DIRECT_PAGES instead of through the seeded route.
    ("/sign-in", "sign_in", True),
    ("/dashboard", "dashboard", True),
    ("/assignments", "assignments/list", True),
    ("/assignments/new", "assignments/new", True),
    (f"/assignments/{ASSIGNMENT_ID}", "assignments/view", True),
    (f"/assignments/{ASSIGNMENT_ID}/submissions", "assignments/submissions",
     True),
    (f"/a/{ASSIGNMENT_ID}", "assignments/share", True),
    ("/submissions", "submissions/list", True),
    (f"/submissions/{SUBMISSION_ID}", "submissions/view", True),
    ("/addon/discovery", "addon/discovery", False),
]

# (origin path, gateway path, name). The apphost's routes carry the
# literal /apps prefix on its own origin (published embed URLs are
# absolute), so behind the gateway its pages resolve at /apps/apps/...
# — the documented issue #170 item 4 shape, not changed by this PR.
APP_PAGES = [
    ("/apps/", "/apps/apps/", "apps_landing"),
    ("/apps/add", "/apps/apps/add", "apps_add"),
    ("/apps/edit/sweep-app", "/apps/apps/edit/sweep-app", "apps_edit"),
]

ADDON_CONTEXT = {
    "courseId": "course-1",
    "itemId": "cw-1",
    "attachmentId": "att-1",
    "submissionId": SUBMISSION_ID,
}

DIRECT_PAGES = [
    ("index.html", "index", True, {}),
    ("addon/teacher.html", "addon/teacher", True,
     {"context": ADDON_CONTEXT, "user_id": USER}),
    ("addon/student.html", "addon/student", True,
     {"context": ADDON_CONTEXT, "user_id": USER}),
    ("addon/review.html", "addon/review", False,
     {"context": ADDON_CONTEXT, "user_id": USER}),
]


def violations(html, gateway_shape):
    """All (pattern-name, matched-text) occurrences of forbidden literals."""
    found = []
    patterns = list(FORBIDDEN_FETCH)
    if gateway_shape:
        patterns += FORBIDDEN_ATTRS
    for pattern, label in patterns:
        for match in re.finditer(pattern, html):
            found.append((label, match.group(0)))
    return found


def render_via_route(client, path, gateway_prefix):
    headers = {}
    if gateway_prefix:
        headers["X-Forwarded-Prefix"] = gateway_prefix
    r = client.get(path, base_url=ORIGIN, headers=headers)
    return r.status_code, r.get_data(as_text=True)


def render_direct(app_, template, gateway_prefix, **ctx):
    base_url = ORIGIN + gateway_prefix if gateway_prefix else ORIGIN
    with app_.test_request_context("/sweep-probe", base_url=base_url):
        flask.g.user = SimpleNamespace(id=USER, email=f"{USER}@nyjc.edu.sg")
        return 200, flask.render_template(template, **ctx)


def sweep(name, html, gateway_prefix, expect_globals=True, namespace=""):
    """Assert one rendered page is literal-free in the requested shape.

    namespace: the deployment's gateway namespace ("/classroom" or
    "/apps") — which namespaced shapes the globals must carry.
    """
    bad = violations(html, gateway_prefix is not None)
    check(f"{name}: no root-absolute URL literals", bad, [])
    sweep_no_double_namespace(name, html)
    # Standalone iframe pages that fetch nothing (discovery, review) carry
    # neither the globals block nor anything that needs it; base.html pages
    # and the fetching addon pages always do.
    if not expect_globals:
        return
    if gateway_prefix:
        check(f"{name}: globals carry the namespace",
              f'window.CAMPUS_API_BASE = "{namespace}/api/v1"' in html, True)
        check(f"{name}: login global is namespaced",
              f'window.CAMPUS_LOGIN_URL = "{namespace}/login"' in html, True)
    else:
        check(f"{name}: globals degrade to root shapes",
              'window.CAMPUS_API_BASE = "/api/v1"' in html, True)
        check(f"{name}: static emits the root shape",
              'href="/static/css/style.css"' in html, True)


def sweep_no_double_namespace(name, html):
    """The #162 regression shape: the literal mount rode the route path
    AND PrefixFix's SCRIPT_NAME, emitting /classroom/classroom/static.
    (The apphost's /apps/apps/<route> shape is its documented literal
    prefix, issue #170 item 4 — only its static must stay single.)"""
    check(f"{name}: static carries no double namespace",
          "/classroom/classroom/static" not in html
          and "/apps/apps/static" not in html, True)


print("== classroom deployment (route-rendered pages) ==")
app = create_app()
client = app.test_client()
seed(client)

for path, name, expect_globals in ROUTE_PAGES:
    for prefix, shape in ((None, "origin"), ("/classroom", "gateway")):
        status, html = render_via_route(client, path, prefix)
        check(f"{name} [{shape}]: renders 200", status, 200)
        sweep(f"{name} [{shape}]", html, prefix, expect_globals,
              "/classroom")

print("== apphost deployment (EMBED_MODE=1, /apps surface) ==")
os.environ["EMBED_MODE"] = "1"
apphost_app = create_app()
apphost_client = apphost_app.test_client()
seed(apphost_client)

for origin_path, gateway_path, name in APP_PAGES:
    for req_path, prefix, shape in (
            (origin_path, None, "origin"),
            (gateway_path, "/apps", "gateway")):
        status, html = render_via_route(apphost_client, req_path, prefix)
        check(f"{name} [{shape}]: renders 200", status, 200)
        sweep(f"{name} [{shape}]", html, prefix, namespace="/apps")

os.environ.pop("EMBED_MODE", None)

print("== addon iframe templates (direct render; Classroom iframes) ==")
for template, name, expect_globals, ctx in DIRECT_PAGES:
    for shape in ("origin", "gateway"):
        prefix = "/classroom" if shape == "gateway" else None
        status, html = render_direct(app, template, prefix, **ctx)
        check(f"{name} [{shape}]: renders", status, 200)
        sweep(f"{name} [{shape}]", html, prefix, expect_globals,
              "/classroom")

print("== static JS sources ==")
js_dir = REPO_ROOT / "apps" / "classroom" / "static" / "js"
api_errors = (js_dir / "api-errors.js").read_text(encoding="utf-8")
check("api-errors.js: no '/login?next=' literal", "'/login?next='" in api_errors,
      False)
check("api-errors.js: composes from CAMPUS_LOGIN_URL",
      "window.CAMPUS_LOGIN_URL" in api_errors, True)

builder = (js_dir / "question-builder.js").read_text(encoding="utf-8")
check("question-builder.js: no '/api/v1/apps' literal",
      "'/api/v1/apps'" in builder, False)
check("question-builder.js: composes from CAMPUS_API_BASE",
      "window.CAMPUS_API_BASE" in builder, True)

# --- result -------------------------------------------------------------------

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
