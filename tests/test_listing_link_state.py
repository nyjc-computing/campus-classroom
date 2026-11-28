"""The listing's link_state envelope + the /connect route rename (#180/#184).

Three contracts:

  A. GET /api/v1/assignments names the bridge state so the dashboard can
     tell "not connected" (actionable — connect/reconnect) apart from a
     genuinely empty class (#180): mapping via _link_state(), exercised
     directly on the error types (constructing the full listing path
     needs the whole campus + Classroom fake stack; the mapping is the
     part that must not drift). The state travels IN THE BODY —
     {"assignments": [...], "link_state": ...} (#184 supersedes the
     #181 response header; one mechanism, discoverable in any JSON
     viewer, matching the in-body "degraded" convention).
  B. The connection page moved from the app's /classroom route (which
     read as the awkward /classroom/classroom/ double shape behind the
     gateway) to /connect; the old origin path redirects, and the
     double-prefixed gateway shape resolves bounded — no loop.
  C. The portal entry (#180 follow-up, user directive 2026-11-28):
     campus.nyjc.app/classroom lands a signed-in visitor on their work
     and keeps the marketing page for anonymous visitors.
  D. Static pin (#184): the header is gone — the route builds the
     envelope, every consumer reads the body fields, and no
     X-Classroom-Link-State reference remains anywhere under apps/.

Run:  python tests/test_listing_link_state.py   (exit 1 on violation)
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

for key, value in {
    "SECRET_KEY": "test",
    "CLIENT_ID": "ci-dummy",
    "CLIENT_SECRET": "ci-dummy",
    "PUBLIC_URL": "http://localhost:5000",
    "BASE_URL": "https://campus.test",
}.items():
    os.environ.setdefault(key, value)

import logging  # noqa: E402

logging.getLogger("apps.classroom").setLevel(logging.CRITICAL)

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import create_app  # noqa: E402
from apps.classroom.routes.assignments import _link_state  # noqa: E402

checks = []


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    checks.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got: {got!r}"))


# --- A. the bridge-state mapping --------------------------------------------

check("no connection -> not-connected",
      _link_state(cauth.NotConnectedError()), "not-connected")
check("missing scopes -> reconnect-needed",
      _link_state(cauth.MissingClassroomScopesError(["scope:x"], [])),
      "reconnect-needed")
check("expired campus session -> reauth-needed",
      _link_state(cauth.CampusSessionExpiredError()), "reauth-needed")
check("anything else -> unavailable",
      _link_state(cauth.BrokerConfigError("allowlist mismatch")), "unavailable")

# --- B. the /connect rename ---------------------------------------------------

client = create_app().test_client()

r = client.get("/connect")
check("connect page: anonymous request redirects to sign-in", r.status_code, 302)
check("… at the root-mounted login with the connect next",
      r.headers.get("Location", ""),
      lambda loc: "/login" in loc and "next=/connect" in loc)

for path in ("/classroom", "/classroom/"):
    r = client.get(path)
    check(f"old origin path {path} redirects (bounded, not 404)", r.status_code, 302)
    check("… to /connect",
          r.headers.get("Location", "").endswith("/connect"),
          True)

# Behind the gateway the old double shape (/classroom/classroom/) strips
# to the app's /classroom alias — it must resolve to the canonical page
# in ONE bounded hop, never loop (this was the live student-dead-end URL).
# Host must carry PUBLIC_URL's port or the pre-flip forward arm (hosts
# "differ") would answer first — that arm also lands here eventually.
r = client.get("/classroom/classroom/",
               headers={"X-Forwarded-Prefix": "/classroom",
                        "Host": "localhost:5000"})
check("old gateway double shape resolves bounded", r.status_code, 302)
check("… to the namespaced canonical path",
      r.headers.get("Location", ""), lambda loc: "/classroom/connect" in loc)

# --- C. the portal entry (#180 follow-up, user directive 2026-11-28) --------

# campus.nyjc.app/classroom must land a signed-in visitor on their work
# (the dashboard shows assignments, or the connect panel), and keep the
# marketing page for anonymous visitors.
from types import SimpleNamespace  # noqa: E402

import flask  # noqa: E402

app = create_app()
with app.test_request_context("/", base_url="https://gateway.example"):
    flask.g.user = SimpleNamespace(id="u1", email="u1@nyjc.edu.sg")
    resp = app.view_functions["index"]()
    check("signed-in portal entry redirects to the assignments list",
          (resp.status_code,
           resp.headers.get("Location", "")),
          lambda got: got[0] == 302 and got[1].endswith("/assignments"))
with app.test_request_context("/", base_url="https://gateway.example"):
    rendered = app.view_functions["index"]()
    check("anonymous portal entry keeps the marketing page",
          isinstance(rendered, str) and "Sign In" in rendered, True)

# --- D. the #184 envelope, pinned statically ----------------------------------

route_src = (REPO_ROOT / "apps" / "classroom" / "routes"
             / "assignments.py").read_text(encoding="utf-8")
apps_tree = (REPO_ROOT / "apps" / "classroom")
header_refs = [
    str(p.relative_to(REPO_ROOT))
    for p in apps_tree.rglob("*")
    if p.is_file() and "__pycache__" not in p.parts
    and "X-Classroom-Link-State" in p.read_text(encoding="utf-8",
                                                errors="ignore")
]
check("no X-Classroom-Link-State reference remains under apps/",
      header_refs, [])
check("route wraps the listing in the envelope body",
      '"assignments": [a.to_resource() for a in visible]' in route_src
      and '"link_state": link_state' in route_src, True)
check("route no longer writes a response header",
      'response.headers' not in route_src, True)

dashboard_src = (apps_tree / "templates" / "dashboard.html").read_text(
    encoding="utf-8")
list_src = (apps_tree / "templates" / "assignments" / "list.html").read_text(
    encoding="utf-8")
teacher_src = (apps_tree / "templates" / "addon" / "teacher.html").read_text(
    encoding="utf-8")
for name, src in (("dashboard", dashboard_src), ("list", list_src),
                  ("teacher picker", teacher_src)):
    check(f"{name} reads the items from payload.assignments",
          "payload.assignments" in src or ").assignments" in src, True)
check("dashboard reads the state from the body",
      "payload.link_state" in dashboard_src, True)
check("list reads the state from the body",
      "payload.link_state" in list_src, True)

# --- result -----------------------------------------------------------------

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
