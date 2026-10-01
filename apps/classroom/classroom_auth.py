"""apps.classroom.classroom_auth

Google Classroom auth bridge (issue #9, epic #3 GC-6/7 foundation).

Campus authenticates identity only; this module bridges a Campus-authenticated
user to a Google credential carrying the Classroom scopes the app needs, so
other sessions can call `with_classroom_session()` and get a ready Classroom
API client.

Architecture (docs/auth-bridge.md has the full rationale):

- The PRD (§7.1 RQ2) assumed Campus would extend its tokens with Classroom
  scopes, but Campus cannot do that today: its Google proxy hardcodes
  email/profile scopes and its credentials API only issues campus-provider
  credentials. So the app runs its own Google OAuth flow with its own
  GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET (PRD §6.10.2 step 3) and Google's
  native incremental authorization (include_granted_scopes=true) stands in
  for the Campus-side incremental approval.
- Identity mapping: Campus user id/email is the Workspace email (Campus
  provisions users from Google userinfo), so the Google account's verified
  email must equal the Campus user's email. Mismatches are refused loudly
  and no credential is stored.
- Persistence: Google credentials live in the signed Flask session cookie
  (epic rule: no local DB; sessions in Flask session). Long-term storage
  should move to Campus once its credentials API accepts third-party
  provider rows; `with_classroom_session()` is the only seam other code
  should touch, so that swap is local to this module.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlencode

import flask
import requests

# --- Scope inventory (PRD §6.4 MVP set, issue #9) ---------------------------

# Identity scopes: needed to enforce the Campus email↔Google email match.
GOOGLE_IDENTITY_SCOPES = (
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

# MVP Classroom scopes. Deliberately excluded (post-MVP, do NOT request):
#   classroom.courses (grade passback), drive.readonly (Drive shortcuts),
#   classroom.push-notifications + classroom.coursework.students (feedback
#   release, session #16 — request incrementally when that session lands).
CLASSROOM_SCOPES_MVP = (
    "https://www.googleapis.com/auth/classroom.addons.teacher",
    "https://www.googleapis.com/auth/classroom.addons.student",
    "https://www.googleapis.com/auth/classroom.coursework.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.students.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
)

# Google endpoints. Module-level so the test harness can stub them.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CLASSROOM_API_BASE = "https://classroom.googleapis.com"

# Refresh the access token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 60

_HTTP_TIMEOUT = 30

# Flask session key holding the stored Google credential dict.
_CREDENTIALS_KEY = "classroom_credentials"


# --- Errors ------------------------------------------------------------------

class ClassroomAuthError(Exception):
    """Base class for auth-bridge failures.

    `message` is safe to show to users; `code` is a stable machine-readable
    identifier for JSON error responses. Nothing here should surface as a 500.
    """

    code = "classroom_auth_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class GoogleNotConfiguredError(ClassroomAuthError):
    """GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing from the environment."""

    code = "google_not_configured"

    def __init__(self):
        super().__init__(
            "Google Classroom sign-in is not configured. Set GOOGLE_CLIENT_ID "
            "and GOOGLE_CLIENT_SECRET in the environment (see .env.example)."
        )


class NotConnectedError(ClassroomAuthError):
    """The signed-in user has no stored Google credential."""

    code = "classroom_not_connected"

    def __init__(self):
        super().__init__(
            "This action needs a Google Classroom connection. "
            "Connect your Google account first."
        )


class MissingClassroomScopesError(ClassroomAuthError):
    """The stored credential lacks scopes the caller requires.

    `missing` scopes can be requested incrementally by redirecting the user
    to /classroom/authorize?scopes=<space-joined missing scopes>.
    """

    code = "classroom_missing_scopes"

    def __init__(self, missing: list[str], granted: list[str]):
        self.missing = list(missing)
        self.granted = list(granted)
        super().__init__(
            "Your Google account has not granted some Classroom permissions "
            "this action needs. Reconnect Google Classroom to grant them."
        )


class IdentityMismatchError(ClassroomAuthError):
    """Google account email does not match the Campus user's email.

    Raised during the OAuth callback; the refusal is intentional and loud.
    """

    code = "classroom_identity_mismatch"

    def __init__(self, campus_email: str, google_email: str):
        self.campus_email = campus_email
        self.google_email = google_email
        super().__init__(
            f"Refused: Google account '{google_email}' does not match your "
            f"Campus account '{campus_email}'. Sign in to Google with your "
            "school account."
        )


class OAuthFlowError(ClassroomAuthError):
    """The Google OAuth token/userinfo exchange failed."""

    code = "classroom_oauth_flow_error"


class ClassroomAPIError(ClassroomAuthError):
    """A Classroom REST API call failed after any token-refresh retry."""

    code = "classroom_api_error"

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"Classroom API call failed ({status_code}): {detail}")


# --- Credential storage (Flask session) --------------------------------------

def get_stored_credentials() -> dict | None:
    """Return the current user's stored Google credential dict, or None."""
    creds = flask.session.get(_CREDENTIALS_KEY)
    if creds and creds.get("access_token"):
        return dict(creds)
    return None


def store_credentials(creds: dict) -> None:
    """Persist a Google credential dict into the Flask session.

    Replaces the whole key (rather than mutating in place) so Flask marks
    the session modified and re-signs the cookie.
    """
    flask.session[_CREDENTIALS_KEY] = dict(creds)


def clear_credentials() -> None:
    """Forget the current user's Google credential, if any."""
    flask.session.pop(_CREDENTIALS_KEY, None)


def missing_scopes(required, granted) -> list[str]:
    """Scopes in `required` that are absent from `granted` (order-stable)."""
    granted_set = set(granted or ())
    return [scope for scope in required if scope not in granted_set]


# --- Google OAuth flow --------------------------------------------------------

def _google_client_config() -> tuple[str, str]:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise GoogleNotConfiguredError()
    return client_id, client_secret


def is_configured() -> bool:
    """True when GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are set."""
    try:
        _google_client_config()
        return True
    except ClassroomAuthError:
        return False


def campus_user_email() -> str:
    """Email of the Campus-authenticated user (identity anchor)."""
    user = getattr(flask.g, "user", None)
    email = getattr(user, "email", None) or getattr(user, "id", None)
    if not email:
        raise ClassroomAuthError("No Campus user is signed in.")
    return str(email)


def build_authorization_url(
        redirect_uri: str,
        scopes,
        state: str,
        login_hint: str | None = None,
) -> str:
    """Build the Google consent URL for an incremental authorization.

    access_type=offline gets a refresh token; include_granted_scopes=true
    merges previously granted scopes into the new grant (Google's
    incremental authorization); prompt=consent guarantees the response
    carries a fresh refresh token even when the user has an existing
    Google session.
    """
    client_id, _ = _google_client_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    workspace_domain = os.environ.get("WORKSPACE_DOMAIN", "")
    if workspace_domain:
        params["hd"] = workspace_domain
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange an authorization code for a Google token payload."""
    client_id, client_secret = _google_client_config()
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=_HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise OAuthFlowError(
            f"Google rejected the authorization code ({resp.status_code}). "
            "Try connecting again."
        )
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a fresh access token."""
    client_id, client_secret = _google_client_config()
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
        timeout=_HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise OAuthFlowError(
            "Google Classroom connection expired. Reconnect your "
            "Google account."
        )
    return resp.json()


def fetch_userinfo(access_token: str) -> dict:
    """Fetch the Google account profile for an access token."""
    resp = requests.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise OAuthFlowError("Could not read the Google account profile.")
    return resp.json()


def connect_from_callback(code: str, redirect_uri: str) -> dict:
    """Complete the OAuth callback: exchange, verify identity, store.

    Enforces the Google↔Campus identity mapping (verified Google email must
    equal the Campus user's email) before anything is persisted. Returns the
    stored credential dict.
    """
    campus_email = campus_user_email()
    token = exchange_code(code, redirect_uri)
    userinfo = fetch_userinfo(token["access_token"])

    google_email = (userinfo.get("email") or "").strip().lower()
    if not google_email or not userinfo.get("email_verified", False):
        raise OAuthFlowError(
            "Google did not return a verified email address for your "
            "account; connection refused."
        )
    if google_email != campus_email.strip().lower():
        raise IdentityMismatchError(campus_email, google_email)

    scopes = set((token.get("scope") or "").split())
    creds = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", ""),
        "expires_at": time.time() + int(token.get("expires_in", 3600)),
        "scopes": sorted(s for s in scopes if s),
        "email": google_email,
    }
    if not creds["refresh_token"]:
        # prompt=consent makes this unlikely; refuse rather than store a
        # credential we cannot refresh.
        raise OAuthFlowError(
            "Google did not return a refresh token; connection refused. "
            "Try connecting again."
        )
    store_credentials(creds)
    return creds


# --- Classroom API client ------------------------------------------------------

class ClassroomClient:
    """Minimal Google Classroom REST client bound to one user's credential.

    Refreshes the access token when expired and once more on a 401, then
    persists any refreshed credential back to the Flask session on close().
    Other sessions add typed methods as needed; `request()` covers
    everything else.
    """

    def __init__(self, creds: dict):
        self._creds = dict(creds)
        self._dirty = False

    # -- token handling --

    def _token_expired(self) -> bool:
        return time.time() >= float(self._creds.get("expires_at", 0)) - _EXPIRY_SKEW_SECONDS

    def _refresh(self) -> None:
        refresh_token = self._creds.get("refresh_token")
        if not refresh_token:
            raise NotConnectedError()
        token = refresh_access_token(refresh_token)
        self._creds["access_token"] = token["access_token"]
        self._creds["expires_at"] = time.time() + int(token.get("expires_in", 3600))
        granted = set((token.get("scope") or "").split())
        if granted:
            self._creds["scopes"] = sorted(set(self._creds.get("scopes", ())) | granted)
        self._dirty = True

    def _access_token(self) -> str:
        if self._token_expired():
            self._refresh()
        return self._creds["access_token"]

    def close(self) -> None:
        """Persist refreshed credentials back to the Flask session."""
        if self._dirty:
            store_credentials(self._creds)
            self._dirty = False

    # -- API calls --

    def request(self, method: str, path: str, *, params=None, json=None):
        """Call a Classroom REST endpoint; returns the decoded JSON body.

        `path` is relative to the API base, e.g. "v1/courses".
        """
        url = f"{CLASSROOM_API_BASE}/{path.lstrip('/')}"
        for attempt in (1, 2):
            resp = requests.request(
                method,
                url,
                params=params,
                json=json,
                headers={
                    "Authorization": f"Bearer {self._access_token()}",
                    "Content-Type": "application/json",
                },
                timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code == 401 and attempt == 1:
                # Access token revoked/expired server-side: refresh once.
                self._refresh()
                continue
            break
        if not resp.ok:
            raise ClassroomAPIError(resp.status_code, resp.text[:500])
        return resp.json() if resp.content else {}

    def courses_list(self, *, page_size: int = 30, teacher_only: bool = False) -> list[dict]:
        """List the user's Classroom courses (courses.list)."""
        params: dict = {"pageSize": min(page_size, 100)}
        if teacher_only:
            params["teacherMe"] = "true"
        data = self.request("GET", "v1/courses", params=params)
        return data.get("courses", [])


@contextmanager
def with_classroom_session(
        required_scopes: "list[str] | tuple[str, ...] | None" = None,
) -> Iterator[ClassroomClient]:
    """Run a block with a ready Classroom client for the signed-in user.

    Usage:
        with with_classroom_session() as classroom:
            courses = classroom.courses_list()

    Raises (never a bare 500 — routes/handlers translate these):
        NotConnectedError: user has no stored Google credential.
        MissingClassroomScopesError: credential lacks `required_scopes`;
            `err.missing` feeds the incremental re-consent redirect.

    Pass `required_scopes=()` to skip the scope gate for calls that only
    need whatever was granted.
    """
    required = tuple(required_scopes) if required_scopes is not None else CLASSROOM_SCOPES_MVP
    creds = get_stored_credentials()
    if creds is None:
        raise NotConnectedError()
    if required:
        missing = missing_scopes(required, creds.get("scopes", []))
        if missing:
            raise MissingClassroomScopesError(missing, creds.get("scopes", []))
    client = ClassroomClient(creds)
    try:
        yield client
    finally:
        client.close()
