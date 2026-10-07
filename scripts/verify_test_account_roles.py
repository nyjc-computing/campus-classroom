"""Verify the Classroom role split of the dev test accounts (#767 §1).

Google's consent screens cannot tell you a user's Classroom role (see
campus#845: teacher and student accounts get identical canonical scope
descriptions). The role gates — add-ons checks, `teacherId=me` queries
— only surface at Classroom API-call time. This script calls the
Classroom API as each test account and reports whether it acts as a
teacher and/or a student, so the account setup (staff=teacher,
student=student) is verified where it actually matters.

MANUAL PREREQUISITES (dev only — this script is never run in CI):
  1. A campus.auth session cookie for the account, harvested from a
     browser that is signed in to any Campus app on dev: DevTools →
     Network → any campusauth-development.up.railway.app request →
     copy the `Cookie:` request header. Put it in .env as
     CAMPUS_AUTH_COOKIE_STAFF / CAMPUS_AUTH_COOKIE_STUDENT.
     Note (campus#844): logging out of a Campus app does NOT end the
     campus.auth session, so a harvested cookie keeps working until
     the session is swept or expires — refresh it only when this
     script reports a Google redirect.
  2. The account's username in .env (CAMPUS_TEST_STAFF_USERNAME /
     CAMPUS_TEST_STUDENT_USERNAME). The test-account PASSWORDS are
     not used here: the script never does the Google leg — it rides
     the campus.auth session cookie instead.

Flow per role:
  POST /auth/v1/sessions/campus/            (Basic = the app's own
        confidential client credentials; returns the auth session,
        including its pre-generated authorization_code)
  GET  /auth/v1/authorize?...               (Cookie header = the
        harvested campus.auth session; with a live session campus.auth
        mints the code without visiting accounts.google.com — the
        #844 behavior. If the chain reaches accounts.google.com the
        cookie is dead: re-harvest.)
  POST /auth/v1/token                       (server-to-server code
        exchange; access token comes back in `id`)
  POST /auth/v1/broker/google/classroom/    (releases the user's
        stored upstream Google token; min_scopes=classroom.courses.readonly)
  GET  classroom.googleapis.com/v1/courses?teacherId=me / studentId=me

The released Google token and the Campus token are held in memory
only (invariant D1) and never printed. Best-effort cleanup revokes
the Campus access token at the end; the harvested cookie itself is
left untouched.

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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTH_BASE_DEFAULT = "https://campusauth-development.up.railway.app"
COURSES_URL = "https://classroom.googleapis.com/v1/courses"
MIN_SCOPES = ["https://www.googleapis.com/auth/classroom.courses.readonly"]
MAX_AUTH_HOPS = 8


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
        with urllib.request.urlopen(request, timeout=30) as response:
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


def exchange_code_for_campus_token(
    *, auth_base: str, cookie: str, client_id: str, client_secret: str,
    redirect_uri: str, session: dict,
) -> dict:
    """Run /authorize with the harvested cookie and exchange the code.

    Returns the Campus token resource (access token string in `id`).
    """
    authorize_url = (
        f"{auth_base}/auth/v1/authorize?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": session["id"],
        })
    )
    code: str | None = None
    url = authorize_url
    for _ in range(MAX_AUTH_HOPS):
        request = urllib.request.Request(url, headers={"Cookie": cookie})
        # No-follow: chase Location headers manually so the final
        # redirect_uri hop (which carries ?code=...) is not requested.
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=30) as response:
                location = response.headers.get("Location")
                if response.status < 300 or not location:
                    sys.exit(
                        f"error: /authorize returned {response.status} "
                        "without a redirect — unexpected; is the cookie "
                        "from the right origin?"
                    )
        except urllib.error.HTTPError as err:
            location = err.headers.get("Location")
            if not location:
                sys.exit(
                    f"error: /authorize failed with HTTP {err.code}: "
                    f"{err.read().decode(errors='replace')[:300]}"
                )
        if location.startswith(("http://accounts.google.com", "https://accounts.google.com")):
            sys.exit(
                "error: campus.auth bounced to Google sign-in — the "
                "harvested CAMPUS_AUTH_COOKIE_… for this role is missing, "
                "expired, or swept. Re-harvest it from a signed-in browser "
                "(see module docstring)."
            )
        if location.startswith(redirect_uri):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
            code = (query.get("code") or [None])[0]
            break
        url = location if location.startswith("http") else auth_base + location
    if not code:
        sys.exit(
            "error: redirect chain never reached the redirect_uri with a "
            "code — inspect the chain manually."
        )

    candidates = [code]
    pregenerated = session.get("authorization_code")
    if pregenerated and pregenerated != code:
        candidates.append(pregenerated)
    last = None
    for candidate in candidates:
        status, token = http_json(
            "POST",
            f"{auth_base}/auth/v1/token",
            headers={"Authorization": basic_auth(client_id, client_secret)},
            body={
                "grant_type": "authorization_code",
                "code": candidate,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        if status == 200:
            return token
        last = (status, token)
    status, body = last  # type: ignore[assignment]
    sys.exit(f"error: code exchange failed (HTTP {status}): {json.dumps(body)[:300]}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classroom_courses(google_token: str, *, role_param: str) -> tuple[int, list[dict]]:
    status, body = http_json(
        "GET",
        f"{COURSES_URL}?{role_param}=me&pageSize=20",
        headers={"Authorization": f"Bearer {google_token}"},
    )
    if status != 200:
        detail = body.get("error", {}).get("message", json.dumps(body)[:200]) \
            if isinstance(body, dict) else str(body)
        sys.exit(f"error: courses.list ({role_param}=me) returned HTTP {status}: {detail}")
    return status, body.get("courses", [])


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
    public_url = cfg("PUBLIC_URL", required=True).rstrip("/")
    redirect_uri = cfg("CAMPUS_REDIRECT_URI", default=f"{public_url}/finalize_login")
    auth_base = cfg("CAMPUS_AUTH_BASE_URL", default=AUTH_BASE_DEFAULT).rstrip("/")
    expected_user = cfg(f"CAMPUS_TEST_{upper}_USERNAME", required=True)
    cookie = cfg(f"CAMPUS_AUTH_COOKIE_{upper}", required=True)

    print(f"[{role}] expected account: {expected_user}")

    # 1. Auth session (server-to-server, app's confidential client).
    status, session = http_json(
        "POST",
        f"{auth_base}/auth/v1/sessions/campus/",
        headers={"Authorization": basic_auth(client_id, client_secret)},
        body={"client_id": client_id, "redirect_uri": redirect_uri},
    )
    if status != 200:
        sys.exit(f"error: session creation failed (HTTP {status}): {json.dumps(session)[:300]}")
    print(f"[{role}] auth session created: {session.get('id', '?')}")

    # 2-3. /authorize with the harvested cookie, then code exchange.
    token = exchange_code_for_campus_token(
        auth_base=auth_base,
        cookie=cookie,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        session=session,
    )
    campus_access_token = token["id"]
    actual_user = token.get("user_id", "?")
    if actual_user != expected_user:
        print(
            f"[{role}] WARNING: token user_id is {actual_user}, expected "
            f"{expected_user} — the cookie belongs to the wrong account. "
            "Results below reflect the WRONG account."
        )

    # 4. Release the stored google.classroom credential via the broker.
    status, released = http_json(
        "POST",
        f"{auth_base}/auth/v1/broker/google/classroom/",
        headers={
            "Authorization": f"Bearer {campus_access_token}",
        },
        body={"min_scopes": MIN_SCOPES},
    )
    if status != 200:
        hint = ""
        if status == 404:
            hint = " (no google.classroom connection for this user — run the connect flow first)"
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

    # 5. Role evidence via the Classroom API.
    _, as_teacher = classroom_courses(google_token, role_param="teacherId")
    _, as_student = classroom_courses(google_token, role_param="studentId")
    print(f"[{role}] courses as TEACHER : {describe_courses(as_teacher)}")
    print(f"[{role}] courses as STUDENT : {describe_courses(as_student)}")

    # 6. Cleanup: revoke the Campus access token (best-effort).
    revoke_status, _ = http_json(
        "POST",
        f"{auth_base}/auth/v1/oauth/revoke",
        headers={"Authorization": basic_auth(client_id, client_secret)},
        body={"token": campus_access_token, "client_id": client_id},
    )
    print(f"[{role}] cleanup: campus token revoked (HTTP {revoke_status})")

    expected = as_teacher if role == "staff" else as_student
    if expected:
        print(f"[{role}] PASS: account acts as Classroom {role} "
              f"({len(expected)} course(s))")
        return 0
    print(
        f"[{role}] FAIL: no courses as {role}. Fix the account's role on "
        "the test class (People → role) and/or its membership, then rerun."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
