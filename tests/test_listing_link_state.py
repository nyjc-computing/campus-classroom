"""The listing's link-state header + the /connect route rename (#180).

Two contracts:

  A. GET /api/v1/assignments names the bridge state in
     X-Classroom-Link-State so the dashboard can tell "not connected"
     (actionable — connect/reconnect) apart from a genuinely empty class
     (#180): mapping via _link_state(), exercised directly on the error
     types (constructing the full listing path needs the whole campus +
     Classroom fake stack; the mapping is the part that must not drift).
  B. The connection page moved from the app's /classroom route (which
     read as the awkward /classroom/classroom/ double shape behind the
     gateway) to /connect; the old origin path redirects, and the
     double-prefixed gateway shape resolves bounded — no loop.

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

# --- result -----------------------------------------------------------------

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
