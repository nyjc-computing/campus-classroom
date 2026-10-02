"""Verification for issue #13: Attachment Discovery iframe (GC-2, PRD §6.9).

Session-5 scope was resolved to the API-first variant (issue #13 scope
note, PRD §6.7 + §7.3 D1): teachers post assignments from the platform
via "Send to Google Classroom" (issue #10), so /addon/discovery is a
public pointer page, not an in-iframe picker.

Checks (local smoke test - the add-on is not registered yet (#11), so
nothing here talks to real Classroom):

1. GET /addon/discovery renders anonymously (200, no login redirect) -
   the Campus session cookie is not guaranteed inside a partitioned
   Classroom iframe, and a login wall would reproduce issue #24.
2. It is iframe-embeddable: no X-Frame-Options, no CSP frame-ancestors.
3. Anonymous page carries the platform-flow instructions and a
   target="_blank" rel="noopener" CTA to /sign-in.
4. Signed-in page swaps the CTA to /dashboard.
5. Classroom URL params (courseId, itemId, addOnToken) are accepted but
   the addOnToken value is never reflected into the HTML.
6. Auto-resize postMessage script is present, matching the other
   /addon/* templates.
7. The local test-iframe harness offers the discovery view.

Usage: .venv/Scripts/python.exe scripts/verify_issue_13.py
"""

import os
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-issue-13-secret"
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-issue-13-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-issue-13-client-secret")

import flask  # noqa: E402

import campus_python.auth.v1 as campus_auth_v1  # noqa: E402

from apps.classroom import create_app  # noqa: E402

CAMPUS_EMAIL = "teacher@nyjc.edu.sg"

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# App with a faked Campus login (push_context reads verify_user from session)
def _fake_push_context(self):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


campus_auth_v1.AuthRoot.push_context = _fake_push_context

app = create_app()
client = app.test_client()

# ---------------------------------------------------------------------------
# 1-2. Anonymous render + iframe-embeddable
# ---------------------------------------------------------------------------
resp = client.get("/addon/discovery")
check("anonymous GET /addon/discovery renders (200, no login redirect)",
      resp.status_code == 200, str(resp.status_code))
check("no X-Frame-Options header",
      "X-Frame-Options" not in resp.headers,
      str(resp.headers.get("X-Frame-Options")))
csp = resp.headers.get("Content-Security-Policy", "")
check("no CSP frame-ancestors blocking framing",
      "frame-ancestors" not in csp, csp)
html_anon = resp.get_data(as_text=True)

# ---------------------------------------------------------------------------
# 3. Anonymous copy + CTA
# ---------------------------------------------------------------------------
check("page explains assignments are posted from the platform",
      "posted from Campus Classroom" in html_anon)
check("page names the Send to Google Classroom flow",
      "Send to Google Classroom" in html_anon)
check("anonymous CTA links to /sign-in in a new tab",
      'href="/sign-in"' in html_anon
      and 'target="_blank"' in html_anon
      and 'rel="noopener"' in html_anon)
check("no dashboard link for anonymous visitors",
      'href="/dashboard"' not in html_anon)

# ---------------------------------------------------------------------------
# 4. Signed-in CTA
# ---------------------------------------------------------------------------
with client.session_transaction() as sess:
    sess["verify_user"] = {"id": CAMPUS_EMAIL, "email": CAMPUS_EMAIL}
resp = client.get("/addon/discovery")
html_auth = resp.get_data(as_text=True)
check("signed-in GET /addon/discovery renders (200)",
      resp.status_code == 200, str(resp.status_code))
check("signed-in CTA links to /dashboard in a new tab",
      'href="/dashboard"' in html_auth
      and 'target="_blank"' in html_auth
      and 'rel="noopener"' in html_auth)
check("signed-in page drops the sign-in link",
      'href="/sign-in"' not in html_auth)

# ---------------------------------------------------------------------------
# 5. Classroom params: accepted, token never reflected
# ---------------------------------------------------------------------------
resp = client.get(
    "/addon/discovery"
    "?courseId=course_999&itemId=cw_999&addOnToken=secret-token-xyz"
)
check("classroom params do not break the render",
      resp.status_code == 200, str(resp.status_code))
check("addOnToken value is never reflected into the page",
      "secret-token-xyz" not in resp.get_data(as_text=True))

# ---------------------------------------------------------------------------
# 6. Auto-resize postMessage
# ---------------------------------------------------------------------------
check("auto-resize postMessage script present",
      "postMessage" in html_anon and "resize" in html_anon)

# ---------------------------------------------------------------------------
# 7. Local test-iframe harness covers the discovery view
# ---------------------------------------------------------------------------
with open("apps/classroom/templates/test-iframe.html", encoding="utf-8") as fh:
    harness = fh.read()
check("test-iframe harness offers the discovery view",
      'value="discovery"' in harness)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    raise SystemExit(1)
print("All issue #13 checks passed.")
