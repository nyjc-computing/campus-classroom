"""Broker release isolation (#168).

`_broker_release` opens a user session on a throwaway Campus inside the
route's own `campus_session.user_session()` block — the save, submit and
export routes run their enrollment gate (is_enrolled / can_review) from
there. campus.auth's login-session token route accepts the client's Basic
credentials only (campus#963/A10): a mint presented with a user bearer is
403 FORBIDDEN, which the routes answer with 502.

The contract:

  1. The nested session's entry mint authenticates as the client
     (Basic), never with the outer session's bearer.
  2. The broker release itself carries the nested session's bearer.
  3. The outer session's bearer is intact once the release returns.
  4. Tripwire: without the suspension the mint inherits the outer
     bearer on the pinned SDK. If this check starts failing, the SDK
     fixed its entry mint (campus-api-python#193) — drop the shim
     (_context_bearer_suspended) in apps/classroom/classroom_auth.py.

Run:  python tests/test_broker_release_isolation.py   (exit 1 on violation)
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Same dummy environment as the CI smoke job: campus_python reads
# CLIENT_ID/CLIENT_SECRET at import and resolves service origins from
# BASE_URL. No request leaves the process — the transport is faked below.
for key, value in {
    "SECRET_KEY": "test",
    "CLIENT_ID": "ci-dummy",
    "CLIENT_SECRET": "ci-dummy",
    "PUBLIC_URL": "http://localhost:5000",
    "BASE_URL": "https://campus.test",
}.items():
    os.environ.setdefault(key, value)

import campus_python  # noqa: E402
import requests  # noqa: E402

from apps.classroom import classroom_auth  # noqa: E402

checks = []


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    checks.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got: {got!r}"))


# --- fake transport ---------------------------------------------------------

calls = []  # (path, authorization) in request order


class _Response:
    status_code = 200
    headers: dict = {}
    text = ""

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _fake_request(self, _method, url, **kwargs):
    headers = dict(self.headers)
    headers.update(kwargs.get("headers") or {})
    path = "/" + url.split("//", 1)[1].split("/", 1)[1].split("?")[0]
    calls.append((path, headers.get("Authorization") or ""))
    if "/broker/" in path:
        return _Response({
            "provider": "google", "user_id": "u", "access_token": "google-token",
            "token_type": "Bearer", "expires_in": 3600, "scope": "s",
        })
    return _Response({})  # only the outer session's own calls land here


class _Token:
    def __init__(self, access_token):
        self.access_token = access_token


def _mint(self):
    """Stand-in for the SDK's entry mint: one request issued through
    _send, which is what decides the Authorization header."""
    self.auth.client.post("/logins/s1/token")
    return _Token(f"user{len(calls)}")


requests.Session.request = _fake_request
campus_python.Campus._get_token_from_session = _mint


def release_inside_user_session():
    """The route shape: an outer user session, then a broker release."""
    outer = campus_python.Campus(timeout=60)
    with outer.with_user_session():
        creds = classroom_auth._broker_release(["scope:a"])
        outer.auth.client.post("/api/v1/outer-after")
    return creds


# --- with the suspension ----------------------------------------------------

calls.clear()
creds = release_inside_user_session()
mints = [auth for path, auth in calls if path == "/logins/s1/token"]
broker = [auth for path, auth in calls if "/broker/" in path]
after = [auth for path, auth in calls if path == "/api/v1/outer-after"]

check("release completes inside an outer user session",
      creds.get("access_token"), "google-token")


def is_client(auth):
    return auth.startswith("Basic ")


check("outer entry mint authenticates as the client", mints[0], is_client)
check("nested entry mint authenticates as the client", mints[1], is_client)
check("broker release carries the nested session's bearer",
      broker[0], lambda a: a.startswith("Bearer ") and a != "Bearer user1")
check("outer session's bearer intact after the release", after[0], "Bearer user1")

# --- tripwire: the SDK defect the suspension exists for ---------------------

saved, classroom_auth._sdk_bearer_override = classroom_auth._sdk_bearer_override, None
try:
    calls.clear()
    release_inside_user_session()
finally:
    classroom_auth._sdk_bearer_override = saved
unsuspended = [auth for path, auth in calls if path == "/logins/s1/token"]

check("tripwire: without the suspension the nested mint presents the outer "
      "bearer (if this fails, the SDK fixed its entry mint — drop the shim)",
      unsuspended[1], "Bearer user1")

# --- restore ----------------------------------------------------------------

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
