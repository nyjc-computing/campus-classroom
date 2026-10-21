"""One-off probe: can this user actually CREATE a Classroom add-on attachment?

The ground-truth test for the missing attach-picker chip (#11): creates a
DRAFT CourseWork in the dev TEST course, calls
courses.courseWork.addOnAttachments.create against it, reports Google's
verbatim response, then DELETES the draft (cascading the attachment).
checkUserCapability was tried first but sits behind the Workspace
Developer Preview Program (404 without membership — the PRD §6.7
"degrades gracefully" case), so the create call itself is the oracle:

- 200/201  → the user CAN create add-on attachments; licences and install
             are fine and the missing chip is a picker-side issue.
- 403      → Google's permission error names the blocker verbatim (the
             seat-licence gate made real) — escalation text for the full
             Super Admin.
- 404      → attachment creation context not found / add-on not visible.

Sign-in: RFC 8628 device flow against campus.auth (cleanup_classroom_
drafts.py pattern) — the browser leg must be the TEST STAFF account
(courseWork.create needs the teacher; the device page names the
authorizing account, campus#852).

POLICY (live-classroom-policy): CampusClass 888684407230 ONLY — never
the live JC1/JC2 classes; the probe draft is deleted in the same run.

Stdlib only. Usage: python scripts/check_addon_capability.py
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_BASE_DEFAULT = "https://campusauth-development.up.railway.app"
CLASSROOM_API = "https://classroom.googleapis.com"
ORIGIN = "https://campus-classroom-development.up.railway.app"
# min_scopes must stay within the classroom client's registered
# upstream_scopes entry (cleanup_classroom_drafts.py header has the
# story); the release still carries the full connected grant incl.
# coursework.students, which courseWork.create needs.
MIN_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
]
COURSE_ID = "888684407230"  # TEST course only — never the live JC1/JC2
DRAFT_TITLE = "GC-1 capability probe (auto-deleted)"


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
        sys.exit(
            f"error: device_authorize failed (HTTP {status}): "
            f"{json.dumps(device)[:200]}"
        )
    print("[probe] open in a browser as CAMPUS-TEST-STAFF and confirm:")
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

    def revoke():
        http_json("POST", f"{auth_base}/auth/v1/oauth/revoke",
                  headers={"Authorization": auth_header},
                  body={"token": campus_token, "client_id": client_id})

    status, released = http_json(
        "POST", f"{auth_base}/auth/v1/broker/google/classroom/",
        headers={"Authorization": f"Bearer {campus_token}"},
        body={"min_scopes": MIN_SCOPES},
    )
    if status != 200:
        revoke()
        sys.exit(
            f"error: broker release failed (HTTP {status}): "
            f"{json.dumps(released)[:300]}"
        )
    google_token = released["access_token"]
    scopes = released.get("scope", "?")
    print("[probe] broker released google.classroom token")
    if "classroom.coursework.students" not in scopes:
        revoke()
        sys.exit("error: grant lacks coursework.students — is this the "
                 "STAFF account? courseWork.create needs it.")
    headers = {"Authorization": f"Bearer {google_token}"}

    # 1. Draft CourseWork in the TEST course (invisible to students).
    status, coursework = http_json(
        "POST", f"{CLASSROOM_API}/v1/courses/{COURSE_ID}/courseWork",
        headers=headers,
        body={
            "title": DRAFT_TITLE,
            "description": "Created + deleted by scripts/check_addon_capability.py",
            "workType": "ASSIGNMENT",
            "state": "DRAFT",
        },
    )
    if status != 200:
        revoke()
        sys.exit(f"error: courseWork.create failed (HTTP {status}): "
                 f"{json.dumps(coursework)[:300]}")
    coursework_id = coursework["id"]
    print(f"[probe] draft CourseWork created: {coursework_id}")

    # 2. THE ORACLE: create an add-on attachment on it.
    status, attachment = http_json(
        "POST",
        f"{CLASSROOM_API}/v1/courses/{COURSE_ID}/courseWork/{coursework_id}"
        f"/addOnAttachments",
        headers=headers,
        body={
            "title": "Campus Classroom capability probe",
            "teacherViewUri": {"uri": f"{ORIGIN}/addon/teacher"},
            "studentViewUri": {"uri": f"{ORIGIN}/addon/student"},
            "studentWorkReviewUri": {"uri": f"{ORIGIN}/addon/review"},
        },
    )

    print()
    print(f"addOnAttachments.create -> HTTP {status}")
    print(f"response: {json.dumps(attachment)[:500]}")
    print()
    if status in (200, 201):
        print("VERDICT: attachment creation SUCCEEDED — install, scopes and "
              "licence/capability are all fine for this user. The missing "
              "chip is a picker-side issue, not access.")
    elif status == 403:
        print("VERDICT: PERMISSION DENIED — Google's message above names "
              "the blocker verbatim (seat-licence gate made real). "
              "Escalate to the full Super Admin with this text.")
    elif status == 404:
        print("VERDICT: NOT FOUND — the add-on/context is not visible to "
              "this user; consistent with install/licence scope problems.")
    else:
        print("VERDICT: inconclusive — inspect the response above.")

    # 3. Cleanup: delete the draft (cascades the attachment).
    status, body = http_json(
        "DELETE",
        f"{CLASSROOM_API}/v1/courses/{COURSE_ID}/courseWork/{coursework_id}",
        headers=headers,
    )
    label = ("deleted" if status in (200, 204)
             else f"HTTP {status}: {json.dumps(body)[:150]} — remove manually")
    print(f"[probe] draft CourseWork cleanup: {label}")

    revoke()
    print("[probe] campus token revoked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
