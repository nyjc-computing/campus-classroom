"""One-off cleanup: delete campus-classroom test drafts from Google Classroom.

Deletes draft CourseWork posts created during smoke/E2E testing from the
dev test courses (CampusClass 888684407230, JC1 828477607889), matched by
title prefix ("E2E gate check" / "Issue 10"). Everything is listed first
and printed before deletion.

Sign-in: RFC 8628 device flow against campus.auth (same as
verify_test_account_roles.py) — the browser leg must be the STAFF test
account (coursework deletion needs a teacher; the device page shows the
authorizing account). Releases google.classroom via the broker with the
courses.readonly + coursework.students scopes.

Stdlib only. Usage: python scripts/cleanup_classroom_drafts.py
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
CLASSROOM_API = "https://classroom.googleapis.com"
# min_scopes must stay within the classroom client's registered
# upstream_scopes entry (asking for coursework.students 400s
# AUTH_INVALID_SCOPE — the app's send seam never asks for it either);
# the release still carries the user's full connected grant, which
# includes coursework.students for the deletion calls.
MIN_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
]
TITLE_PREFIXES = ("E2E gate check", "Issue 10")
COURSES = ["888684407230", "828477607889"]


def load_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    path = REPO_ROOT / ".env"
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), value.strip().strip("'\""))
    return values


_DOTENV = load_dotenv()


def cfg(key: str, required: bool = True) -> str:
    value = os.environ.get(key) or _DOTENV.get(key) or ""
    if required and not value:
        sys.exit(f"error: {key} is not set")
    return value


def http_json(method, url, *, headers=None, body=None):
    data = None
    all_headers = {"Accept": "application/json"}
    if headers:
        all_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"raw": raw[:300]}


def main() -> int:
    client_id = cfg("CLIENT_ID")
    client_secret = cfg("CLIENT_SECRET")
    auth_base = _DOTENV.get("CAMPUS_AUTH_BASE_URL", AUTH_BASE_DEFAULT).rstrip("/")

    status, device = http_json(
        "POST", f"{auth_base}/auth/v1/oauth/device_authorize",
        body={"client_id": client_id},
    )
    if status != 200:
        sys.exit(f"error: device_authorize failed (HTTP {status}): {json.dumps(device)[:200]}")
    print("[cleanup] open in a browser as the STAFF test account and confirm:")
    print(f"  {device.get('verification_uri_complete') or device['verification_uri']}")

    interval = int(device.get("interval", 5))
    deadline = time.monotonic() + int(device.get("expires_in", 600)) + 10
    auth_header = "Basic " + base64.b64encode(
        f"{client_id}:{client_secret}".encode()).decode()
    campus_token = None
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, body = http_json(
            "POST", f"{auth_base}/auth/v1/oauth/token",
            headers={"Authorization": auth_header},
            body={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            },
        )
        if status == 200:
            campus_token = body["access_token"]
            break
        envelope = body.get("error", {}) if isinstance(body, dict) else {}
        errors = {
            str(body.get("oauth_error", "")),
            str(envelope.get("code", "")).lower(),
            str(envelope.get("details", {}).get("oauth_error", "")),
        }
        if "authorization_pending" in errors or "slow_down" in errors:
            continue
        sys.exit(f"error: token poll failed (HTTP {status}): {json.dumps(body)[:200]}")
    if not campus_token:
        sys.exit("error: device code expired before authorization.")

    status, released = http_json(
        "POST", f"{auth_base}/auth/v1/broker/google/classroom/",
        headers={"Authorization": f"Bearer {campus_token}"},
        body={"min_scopes": MIN_SCOPES},
    )
    if status != 200:
        sys.exit(f"error: broker release failed (HTTP {status}): {json.dumps(released)[:300]}")
    google_token = released["access_token"]
    print("[cleanup] broker released google.classroom token (staff)")

    headers = {"Authorization": f"Bearer {google_token}"}
    targets: list[dict] = []
    for course_id in COURSES:
        status, body = http_json(
            "GET",
            f"{CLASSROOM_API}/v1/courses/{course_id}/courseWork?pageSize=50",
            headers=headers,
        )
        if status != 200:
            print(f"[cleanup] WARN: courseWork.list {course_id} -> HTTP {status}, skipping")
            continue
        for work in body.get("courseWork", []):
            title = work.get("title", "")
            print(f"[cleanup]   saw: {title!r} "
                  f"(course {course_id}, coursework {work.get('id')}, "
                  f"state={work.get('state')})")
            if title.startswith(TITLE_PREFIXES):
                targets.append({"courseId": course_id, "id": work["id"], "title": title})

    if not targets:
        print("[cleanup] nothing to delete — no matching drafts found.")
        http_json("POST", f"{auth_base}/auth/v1/oauth/revoke",
                  headers={"Authorization": auth_header},
                  body={"token": campus_token, "client_id": client_id})
        return 0

    print("[cleanup] deleting:")
    for t in targets:
        print(f"  - {t['title']}  (course {t['courseId']}, coursework {t['id']})")

    failures = 0
    for t in targets:
        status, body = http_json(
            "DELETE",
            f"{CLASSROOM_API}/v1/courses/{t['courseId']}/courseWork/{t['id']}",
            headers=headers,
        )
        label = "deleted" if status in (200, 204) else f"HTTP {status}: {json.dumps(body)[:150]}"
        print(f"[cleanup]   {t['title']} -> {label}")
        if status not in (200, 204):
            failures += 1

    http_json("POST", f"{auth_base}/auth/v1/oauth/revoke",
              headers={"Authorization": auth_header},
              body={"token": campus_token, "client_id": client_id})
    print("[cleanup] campus token revoked")
    if failures:
        print(f"CLEANUP INCOMPLETE: {failures} deletion(s) failed")
        return 1
    print("CLEANUP COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
