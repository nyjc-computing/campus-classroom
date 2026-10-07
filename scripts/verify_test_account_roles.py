"""Verify the Classroom role split of the dev test accounts (#767 §1).

Google's consent screens cannot tell you a user's Classroom role (see
campus#845: teacher and student accounts get identical canonical scope
descriptions). The role gates — add-ons checks, `teacherId=me` queries
— only surface at Classroom API-call time. This script calls the
Classroom API as each test account and reports whether it acts as a
teacher and/or a student, so the account setup (staff=teacher,
student=student) is verified where it actually matters.

HOW SIGN-IN WORKS (dev only — this script is never run in CI):

  RFC 8628 device flow against campus.auth. The script prints a
  verification URL; you open it in a browser, sign in AS the test
  account, and confirm the user code. Since campus#846 the identity
  login always stops at Google's account chooser (prompt=select_account),
  so the account pick is explicit — no silent carry-over from whatever
  the browser was signed in as before. The script then polls for the
  Campus token, releases the account's stored google.classroom
  credential via the token broker, and calls the Classroom API.

  No passwords are needed here or anywhere: the browser leg is the
  real Google login (test-account passwords live in .env only for
  browser-driven E2E, which this script is not).

PREREQUISITES:
  1. The account has done the google.classroom connect flow at least
     once on dev (campus-profile → integrations → Connect), so
     campus.auth holds its upstream credential to release.
  2. The releasing client — this app's CLIENT_ID from .env — is
     confidential, token_bridge-flagged, has a non-empty
     upstream_scopes entry for google.classroom, and its
     allowed_scopes include the device-flow defaults ["read",
     "write"] (the device grant hardcodes them). The dev client was
     widened once on 2026-10-07; if the environment is rebuilt,
     re-apply with the operator Basic credentials:
       PATCH /auth/v1/clients/<client_id>/
       {"allowed_scopes": ["campus.profile", "read", "write"]}

FLOW:
  POST /auth/v1/oauth/device_authorize   → device_code + verification URL
  (browser: sign in as the test account, confirm code)
  POST /auth/v1/oauth/token              (device_code grant poll;
        access token in `access_token`)
  POST /auth/v1/broker/google/classroom/ (Bearer user token; releases
        the upstream Google token; min_scopes=classroom.courses.readonly)
  GET  classroom.googleapis.com/v1/courses?teacherId=me / studentId=me

Tokens are held in memory only (invariant D1) and never printed.
Cleanup revokes the Campus access token at the end.

Usage:
  python scripts/verify_test_account_roles.py staff
  python scripts/verify_test_account_roles.py student

Stdlib only — no venv needed (any Python 3.11+).
Exit code: 0 = role verified as expected; 1 = connected but role
evidence contradicts the expectation; 2 = setup/auth error.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_BASE_DEFAULT = "https://campusauth-development.up.railway.app"
COURSES_URL = "https://classroom.googleapis.com/v1/courses"
MIN_SCOPES = ["https://www.googleapis.com/auth/classroom.courses.readonly"]


def load_dotenv() -> dict[str, str]:
    """Parse the repo-root .env (repo convention: real env wins)."""
    values: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        values.setdefault(key.strip(), value)
    return values


_DOTENV = load_dotenv()


def cfg(key: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(key) or _DOTENV.get(key) or default
    if required and not value:
        sys.exit(
            f"error: {key} is not set. Add it to .env (gitignored) or "
            "export it in the shell."
        )
    return value or ""


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    """One JSON round trip. Returns (status, parsed body)."""
    data = None
    all_headers = {"Accept": "application/json"}
    if headers:
        all_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"raw": raw[:400]}


def basic_auth(user: str, secret: str) -> str:
    encoded = base64.b64encode(f"{user}:{secret}".encode()).decode()
    return f"Basic {encoded}"


def request_device_code(auth_base: str, client_id: str) -> dict:
    status, body = http_json(
        "POST",
        f"{auth_base}/auth/v1/oauth/device_authorize",
        body={"client_id": client_id},
    )
    if status != 200:
        hint = ""
        if "AUTH_INVALID_SCOPE" in json.dumps(body):
            hint = (
                "\n  The client's allowed_scopes do not include the "
                "device-flow defaults ['read', 'write']. Widen them (see "
                "the module docstring)."
            )
        sys.exit(
            f"error: device_authorize failed (HTTP {status}): "
            f"{json.dumps(body)[:300]}{hint}"
        )
    return body


def poll_for_token(
    auth_base: str, client_id: str, client_secret: str, device: dict
) -> str:
    """Poll the device_code grant until authorized. Returns the access token."""
    interval = int(device.get("interval", 5))
    deadline = time.monotonic() + int(device.get("expires_in", 600)) + 10
    auth_header = basic_auth(client_id, client_secret)
    url = f"{auth_base}/auth/v1/oauth/token"
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, body = http_json(
            "POST",
            url,
            headers={"Authorization": auth_header},
            body={
                "grant_type": "urn:ietf:params:oauth:grant-type=device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            },
        )
        if status == 200:
            return body["access_token"]
        error = body.get("oauth_error") or body.get("error", {}).get("code", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5  # RFC 8628 §3.5; server enforces the raised floor
            continue
        sys.exit(
            f"error: device token poll failed (HTTP {status}, "
            f"{error}): {json.dumps(body)[:300]}"
        )
    sys.exit("error: device code expired before authorization completed.")


def classroom_courses(google_token: str, *, role_param: str) -> list[dict]:
    status, body = http_json(
        "GET",
        f"{COURSES_URL}?{role_param}=me&pageSize=20",
        headers={"Authorization": f"Bearer {google_token}"},
    )
    if status != 200:
        detail = (
            body.get("error", {}).get("message", json.dumps(body)[:200])
            if isinstance(body, dict)
            else str(body)
        )
        sys.exit(
            f"error: courses.list ({role_param}=me) returned HTTP "
            f"{status}: {detail}"
        )
    return body.get("courses", [])


def describe_courses(courses: list[dict]) -> str:
    if not courses:
        return "(none)"
    return "; ".join(
        f"{course.get('name', '?')} [{course.get('id', '?')}]" for course in courses
    )


def main() -> int:
    role = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if role not in ("staff", "student"):
        sys.exit("usage: python scripts/verify_test_account_roles.py staff|student")
    upper = role.upper()

    client_id = cfg("CLIENT_ID", required=True)
    client_secret = cfg("CLIENT_SECRET", required=True)
    auth_base = cfg("CAMPUS_AUTH_BASE_URL", default=AUTH_BASE_DEFAULT).rstrip("/")
    expected_user = cfg(f"CAMPUS_TEST_{upper}_USERNAME", required=True)

    print(f"[{role}] expected account: {expected_user}")

    # 1. Device flow: print the URL, wait for the browser authorization.
    device = request_device_code(auth_base, client_id)
    print(
        f"[{role}] open this URL in a browser, sign in AS {expected_user}, "
        "and confirm the code:"
    )
    print(f"  {device.get('verification_uri_complete') or device['verification_uri']}")
    campus_access_token = poll_for_token(
        auth_base, client_id, client_secret, device
    )

    # 2. Release the stored google.classroom credential via the broker.
    status, released = http_json(
        "POST",
        f"{auth_base}/auth/v1/broker/google/classroom/",
        headers={"Authorization": f"Bearer {campus_access_token}"},
        body={"min_scopes": MIN_SCOPES},
    )
    if status != 200:
        hint = ""
        if status == 404:
            hint = (
                " (no google.classroom connection for this user — run the "
                "connect flow from campus-profile integrations first)"
            )
        sys.exit(
            f"error: broker release failed (HTTP {status}){hint}: "
            f"{json.dumps(released)[:300]}"
        )
    google_token = released["access_token"]
    print(
        f"[{role}] broker released google.classroom token "
        f"(expires_in={released.get('expires_in', '?')}s, "
        f"scope has courses.readonly="
        f"{MIN_SCOPES[0] in released.get('scope', '')})"
    )

    # 3. Role evidence via the Classroom API.
    as_teacher = classroom_courses(google_token, role_param="teacherId")
    as_student = classroom_courses(google_token, role_param="studentId")
    print(f"[{role}] courses as TEACHER : {describe_courses(as_teacher)}")
    print(f"[{role}] courses as STUDENT : {describe_courses(as_student)}")

    # 4. Cleanup: revoke the Campus access token (best-effort).
    revoke_status, _ = http_json(
        "POST",
        f"{auth_base}/auth/v1/oauth/revoke",
        headers={"Authorization": basic_auth(client_id, client_secret)},
        body={"token": campus_access_token, "client_id": client_id},
    )
    print(f"[{role}] cleanup: campus token revoked (HTTP {revoke_status})")

    expected = as_teacher if role == "staff" else as_student
    if expected:
        print(
            f"[{role}] PASS: account acts as Classroom {role} "
            f"({len(expected)} course(s))"
        )
        return 0
    print(
        f"[{role}] FAIL: no courses as {role}. Fix the account's role on "
        "the test class (People → role) and/or its membership, then rerun."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
