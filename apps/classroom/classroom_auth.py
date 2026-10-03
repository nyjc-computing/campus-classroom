"""apps.classroom.classroom_auth

Google Classroom auth bridge (issue #9; broker swap issue #30).

Campus.auth is the sole custodian of Google Classroom credentials (the
namespaced `google.classroom` integration; design campus#730 §2.4–2.5,
tracker campus#733). This module releases the signed-in user's Classroom
access token from campus.auth's token broker so other sessions can call
`with_classroom_session()` and get a ready Classroom API client.

Architecture (docs/auth-bridge.md has the full rationale):

- Token release: `with_classroom_session()` POSTs
  `{campus.auth}/auth/v1/broker/google/classroom/` with the user's campus
  bearer token (the one the app already holds from its campus login
  session). The broker answers with a live access token, its expiry and
  its scope — never a refresh token. When it nears expiry the app simply
  asks the broker again (campus refreshes its stored credential silently
  server-side). Tokens live in memory for at most one request's
  duration; nothing is persisted anywhere.
- Connecting: the user grants the `google.classroom` integration once via
  the campus-profile integrations page (campus.auth's connect flow). This
  app has no connect UX of its own; NotConnectedError points there.
- Identity mapping is enforced campus-side at connect time (consenting
  Google email must equal the campus session user), so the release path
  needs no local identity check.
- LEGACY (issue #30 retire lane, removed once the broker path is proven):
  the app's own Google OAuth flow (`/classroom/authorize|callback` +
  Flask-session token storage) is kept working through the transition,
  but `with_classroom_session()` no longer reads or writes it —
  session-stored Google credentials are dead weight until removal.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlencode

import flask
import requests
from campus_python.errors import APIError as CampusClientError

# --- Scope inventory (PRD §6.4 MVP set, issue #9) ---------------------------

# Identity scopes: enforced campus-side for the integration connect flow
# (part of the google.classroom vault SCOPES cap); the legacy in-app flow
# below still uses them for its own email match.
GOOGLE_IDENTITY_SCOPES = (
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
)

# MVP Classroom scopes. Scope names are taken from the GCP "Data access"
# list (the canonical set), NOT the PRD §6.4 table — the PRD has two wrong
# names, found by Google at first real consent (issue #9 smoke test):
#   - "classroom.coursework.readonly" does not exist; the real scope is
#     "classroom.course-work.readonly" (hyphenated).
#   - "classroom.coursework.students.readonly" is a noncanonical alias of
#     "classroom.student-submissions.students.readonly" (requesting both is
#     rejected as redundant).
# courses.readonly is required for courses.list() (course pickers, the
# /classroom live check) despite not being in PRD §6.4's table — that
# table's post-MVP "classroom.courses" is the read-write scope.
# Still excluded (do NOT request): drive.readonly (Drive shortcuts),
# classroom.push-notifications (feedback release, session #16 — request
# incrementally when that lands).
CLASSROOM_SCOPES_MVP = (
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.addons.teacher",
    "https://www.googleapis.com/auth/classroom.addons.student",
    "https://www.googleapis.com/auth/classroom.course-work.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.students.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
)

# classroom.coursework.students (write) is needed by issue #10's
# Send-to-Classroom flow: courses.courseWork.create / .patch (draft
# CourseWork, Link Material) require it per the Classroom discovery doc —
# the MVP classroom.course-work.readonly is listing-only. PRD §6.4 defers
# it to post-MVP (feedback release needs it too, session #16), but
# CourseWork creation comes first, so it is a FEATURE scope: never
# requested at connect, demanded by the send flow via
# with_classroom_session(required_scopes=...) — a missing grant raises
# MissingClassroomScopesError. (The PRD §6.4 table's "classroom.courses"
# is course-level management — not needed here.)
#
# NOTE (issue #30, corrected 2026-10-03 on live verification): the dev
# vault's SCOPES cap turned out WIDER than first recorded — the profile
# connect grants 11 scopes (7 MVP + coursework.students + userinfo pair +
# openid) — so this scope IS grantable and the send flow works through the
# broker. The seam still does not ASK the broker for it (see
# _BROKER_ASKABLE below); it is enforced locally against the returned grant.
CLASSROOM_SCOPES_SEND = (
    "https://www.googleapis.com/auth/classroom.coursework.students",
)

# Google endpoints (legacy in-app flow only). Module-level so the test
# harness can stub them.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CLASSROOM_API_BASE = "https://classroom.googleapis.com"

# Refresh the access token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 60

# Public preview version for userProfiles.checkUserCapability (PRD §6.7).
_CAPABILITY_PREVIEW_VERSION = "V1_20240930_PREVIEW"

_HTTP_TIMEOUT = 30

# Flask session key of the LEGACY stored Google credential (retire lane).
_CREDENTIALS_KEY = "classroom_credentials"

# --- campus.auth token broker (issue #30) -------------------------------------

# campus.auth route that releases the user's google.classroom access
# token (campus#733 Phase 1, live on dev via campus#741). Module-level so
# the verify harness can retarget it at a stub.
BROKER_PATH = "/auth/v1/broker/google/classroom/"

# Scopes the broker may be ASKED for: deliberately the conservative set
# (MVP + identity) even though the dev vault's observed cap is wider
# (coursework.students + openid included, verified 2026-10-03). Asking
# beyond a deployment's cap is a 400 AUTH_INVALID_SCOPE — a configuration
# bug, not a re-consent situation — so only this floor is ever asked;
# caller requirements beyond it (CLASSROOM_SCOPES_SEND) are enforced
# locally against the returned grant, which the local scope gate does
# just as correctly. Widen this set only if broker-authoritative
# missing-scope 403s are wanted for the feature scopes.
_BROKER_ASKABLE = frozenset(CLASSROOM_SCOPES_MVP) | frozenset(GOOGLE_IDENTITY_SCOPES)

# campus-profile origins (the integrations/connect host). development is
# the Railway dev deployment; staging/production follow the campus-suite
# service naming. CAMPUS_PROFILE_URL overrides explicitly.
_PROFILE_DEVELOPMENT_URL = "https://campus-profile-development.up.railway.app"
_PROFILE_URLS = {
    "development": _PROFILE_DEVELOPMENT_URL,
    "staging": "https://campus-profile.campus.nyjc.dev",
    "production": "https://campus-profile.campus.nyjc.app",
}


def profile_integrations_url() -> str:
    """Origin of the campus-profile app that hosts the connect UX."""
    explicit = os.environ.get("CAMPUS_PROFILE_URL", "")
    if explicit:
        return explicit.rstrip("/")
    env_name = os.environ.get("ENV", os.environ.get("CAMPUS_ENV", "development"))
    return _PROFILE_URLS.get(env_name, _PROFILE_DEVELOPMENT_URL)


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
    """GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing from the environment.

    Legacy in-app flow only; the broker path has no Google config of its
    own (campus.auth holds the integration's client).
    """

    code = "google_not_configured"

    def __init__(self):
        super().__init__(
            "Google Classroom sign-in is not configured. Set GOOGLE_CLIENT_ID "
            "and GOOGLE_CLIENT_SECRET in the environment (see .env.example)."
        )


class NotConnectedError(ClassroomAuthError):
    """campus.auth holds no google.classroom credential for the user."""

    code = "classroom_not_connected"

    def __init__(self):
        super().__init__(
            "This action needs a Google Classroom connection. Connect your "
            "Google account once on the Campus profile integrations page: "
            f"{profile_integrations_url()}/profile/integrations"
        )


class MissingClassroomScopesError(ClassroomAuthError):
    """The released credential lacks scopes the caller requires.

    `missing` scopes can be granted by reconnecting via the campus-profile
    integrations page (the dev vault's cap covers every scope this app
    asks for — verified 2026-10-03).
    """

    code = "classroom_missing_scopes"

    def __init__(self, missing: list[str], granted: list[str]):
        self.missing = list(missing)
        self.granted = list(granted)
        super().__init__(
            "Your Google account has not granted some Classroom permissions "
            "this action needs. Reconnect Google Classroom to grant them."
        )


class CampusSessionExpiredError(ClassroomAuthError):
    """The user's Campus login session cannot back a broker release.

    The broker requires a live campus bearer bound to the user; when none
    can be obtained (no login session, or the stored campus credential is
    gone/unrefreshable) the user must sign in to Campus again.
    """

    code = "campus_session_expired"

    def __init__(self):
        super().__init__(
            "Your Campus sign-in has expired. Sign in to Campus again."
        )


class BrokerConfigError(ClassroomAuthError):
    """campus.auth rejected the release as a caller/configuration error.

    Raised for 400 AUTH_INVALID_SCOPE (min_scopes vs allowlist/vault cap)
    and 403 bridge-guard denials (client flags): deployment configuration
    bugs, logged loudly at the raise site, never swallowed.
    """

    code = "classroom_broker_config"


class IdentityMismatchError(ClassroomAuthError):
    """Google account email does not match the Campus user's email.

    Legacy in-app flow only (the broker path enforces this campus-side at
    connect). Raised during the OAuth callback; the refusal is intentional
    and loud.
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
    """The legacy OAuth token/userinfo exchange failed, or the campus.auth
    broker could not be reached or returned a malformed response."""

    code = "classroom_oauth_flow_error"


class ClassroomAPIError(ClassroomAuthError):
    """A Classroom REST API call failed after any token-refresh retry."""

    code = "classroom_api_error"

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"Classroom API call failed ({status_code}): {detail}")


# --- LEGACY credential storage (Flask session; issue #30 retire lane) --------
#
# Read/written only by the legacy /classroom/authorize|callback flow below.
# with_classroom_session() no longer touches any of this.

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


# --- campus.auth broker access (issue #30) -------------------------------------

def _broker_url() -> str:
    """Full campus.auth broker URL, from the app's Campus client."""
    campus = flask.current_app.campus
    return campus.auth.client.base_url.rstrip("/") + BROKER_PATH


def _campus_bearer() -> str:
    """The signed-in user's campus access token, refreshed when expired.

    Public SDK surface only: `auth.get_token()` returns the stored campus
    credential as-is (the SDK does not auto-refresh yet), so an expired
    token is refreshed here the same way Campus._get_token_from_session
    does. Any SDK auth failure maps to CampusSessionExpiredError — the
    remedy is a fresh campus sign-in, not a retry.
    """
    auth = flask.current_app.campus.auth
    try:
        token = auth.get_token()
        if token.is_expired():
            login = auth.logins.from_session()
            token = auth.token(
                grant_type="refresh_token",
                refresh_token=token.refresh_token,
            )
            auth.credentials["campus"][login.user_id].update(token=token)
    except CampusClientError as err:
        raise CampusSessionExpiredError() from err
    return token.access_token


def _broker_release(min_scopes: list[str]) -> dict:
    """POST the campus.auth broker for the user's google.classroom token.

    Error mapping (issue #30, semantics preserved for callers):
        404 no connected credential   -> NotConnectedError (profile pointer)
        403 + details.missing_scopes  -> MissingClassroomScopesError
        401 bad/expired campus bearer -> CampusSessionExpiredError (re-login)
        400 AUTH_INVALID_SCOPE / other 403 -> BrokerConfigError, logged
            loudly: an allowlist/vault-cap/bridge-flag mismatch is a
            deployment configuration bug and must not be swallowed.
    """
    try:
        resp = requests.post(
            _broker_url(),
            json={"min_scopes": list(min_scopes)} if min_scopes else {},
            headers={"Authorization": f"Bearer {_campus_bearer()}"},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as err:
        raise OAuthFlowError(
            "Could not reach the Campus token broker; try again shortly."
        ) from err

    if resp.ok:
        try:
            return resp.json()
        except ValueError as err:
            raise OAuthFlowError(
                "Campus token broker returned a malformed response."
            ) from err
    if resp.status_code == 404:
        raise NotConnectedError()
    if resp.status_code == 401:
        raise CampusSessionExpiredError()

    body: dict = {}
    if resp.content:
        try:
            body = resp.json()
        except ValueError:
            body = {}
    error = body.get("error") if isinstance(body, dict) else None
    details = error.get("details", {}) if isinstance(error, dict) else {}
    code = error.get("code", "") if isinstance(error, dict) else ""
    missing = details.get("missing_scopes") if isinstance(details, dict) else None
    if resp.status_code == 403 and missing:
        raise MissingClassroomScopesError(list(missing), [])

    flask.current_app.logger.error(
        "campus.auth broker rejected the google.classroom release "
        "(HTTP %s, %s): %s", resp.status_code, code or "no error code", body,
    )
    raise BrokerConfigError(
        "Campus refused the Classroom token request "
        f"(HTTP {resp.status_code}, {code or 'no error code'}); this is a "
        "deployment configuration problem."
    )


def fetch_broker_credential(min_scopes: list[str] | None = None) -> dict:
    """One broker release as a credential dict, for in-memory use only.

    `min_scopes=None` returns the user's connected grant as-is (allowlist-
    gated campus-side) — what the /classroom status page shows. A list
    enforces the minimum on the broker. Never persisted: the caller holds
    the token until `expires_in` at most, then asks the broker again.
    """
    data = _broker_release(min_scopes or [])
    access_token = data.get("access_token") if isinstance(data, dict) else None
    if not access_token:
        raise OAuthFlowError("Campus token broker returned a malformed response.")
    return {
        "access_token": str(access_token),
        "expires_at": time.time() + max(int(data.get("expires_in") or 0), 0),
        "scopes": sorted(str(data.get("scope") or "").split()),
        "email": str(data.get("user_id") or ""),
    }


# --- LEGACY Google OAuth flow (issue #30 retire lane) --------------------------
#
# Kept working through the broker transition; removed once the broker path
# is proven (see issue #30 "Retire"). Not used by with_classroom_session().

def _google_client_config() -> tuple[str, str]:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise GoogleNotConfiguredError()
    return client_id, client_secret


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


def campus_user_email() -> str:
    """Email of the Campus-authenticated user (identity anchor)."""
    user = getattr(flask.g, "user", None)
    email = getattr(user, "email", None) or getattr(user, "id", None)
    if not email:
        raise ClassroomAuthError("No Campus user is signed in.")
    return str(email)


# --- Classroom API client ------------------------------------------------------

class ClassroomClient:
    """Minimal Google Classroom REST client backed by the campus.auth broker.

    Holds one released access token in memory; asks the broker again when
    it nears expiry or after a 401 (campus refreshes its stored credential
    server-side — no refresh token ever reaches this app, and nothing is
    persisted). Other sessions add typed methods as needed; `request()`
    covers everything else.
    """

    def __init__(self, required: tuple[str, ...] = ()):
        self._required = tuple(required)
        self._creds: dict | None = None

    # -- token handling --

    def _ensure_token(self) -> None:
        """Release (or re-release) a broker token, then gate required scopes.

        The broker is asked only for the conservative floor
        (_BROKER_ASKABLE, see the comment there); any further requirement
        (feature scopes) is checked locally against the returned grant.
        """
        if (self._creds is not None
                and time.time()
                < float(self._creds["expires_at"]) - _EXPIRY_SKEW_SECONDS):
            return
        ask = [scope for scope in self._required if scope in _BROKER_ASKABLE]
        creds = fetch_broker_credential(ask)
        if self._required:
            missing = missing_scopes(self._required, creds["scopes"])
            if missing:
                raise MissingClassroomScopesError(missing, creds["scopes"])
        self._creds = creds

    def _access_token(self) -> str:
        self._ensure_token()
        return self._creds["access_token"]

    def close(self) -> None:
        """Nothing to persist: broker tokens are never stored."""

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
                # Access token revoked/expired server-side ahead of
                # schedule: release a fresh one from the broker, retry once.
                self._creds = None
                continue
            break
        if not resp.ok:
            raise ClassroomAPIError(resp.status_code, resp.text[:500])
        return resp.json() if resp.content else {}

    def courses_list(self, *, page_size: int = 30, teacher_only: bool = False) -> list[dict]:
        """List the user's Classroom courses (courses.list).

        `teacher_only` uses teacherId=me — courses.list has no `teacherMe`
        parameter (Google rejects it with 400 INVALID_ARGUMENT; found by the
        issue #10 smoke test against the real API).
        """
        params: dict = {"pageSize": min(page_size, 100)}
        if teacher_only:
            params["teacherId"] = "me"
        data = self.request("GET", "v1/courses", params=params)
        return data.get("courses", [])

    # -- Send-to-Classroom (issue #10, PRD §6.7) --

    def check_user_capability(self, capability: str) -> bool:
        """userProfiles.checkUserCapability (public preview).

        Returns the `allowed` flag. An API failure means "cannot answer"
        rather than "allowed", and per PRD §6.7 unavailability degrades to
        the Link Material fallback — so errors return False, never raise.
        """
        try:
            data = self.request(
                "POST",
                "v1/userProfiles/me:checkUserCapability",
                params={"previewVersion": _CAPABILITY_PREVIEW_VERSION},
                json={"capability": capability},
            )
        except ClassroomAPIError:
            return False
        return bool(data.get("allowed", False))

    def coursework_create(self, course_id: str, body: dict) -> dict:
        """Create a CourseWork post (courses.courseWork.create)."""
        return self.request("POST", f"v1/courses/{course_id}/courseWork", json=body)

    def coursework_patch(self, course_id: str, coursework_id: str, body: dict) -> dict:
        """Update mutable CourseWork fields (courses.courseWork.patch)."""
        return self.request(
            "PATCH", f"v1/courses/{course_id}/courseWork/{coursework_id}", json=body
        )

    def addon_attachment_create(
            self, course_id: str, coursework_id: str, body: dict,
    ) -> dict:
        """Create an add-on attachment under a CourseWork post
        (courses.courseWork.addOnAttachments.create; addOnToken is optional
        for partner-first creation)."""
        return self.request(
            "POST",
            f"v1/courses/{course_id}/courseWork/{coursework_id}/addOnAttachments",
            json=body,
        )


@contextmanager
def with_classroom_session(
        required_scopes: "list[str] | tuple[str, ...] | None" = None,
) -> Iterator[ClassroomClient]:
    """Run a block with a ready Classroom client for the signed-in user.

    The user's google.classroom access token is released from campus.auth's
    token broker — eagerly, so NotConnectedError / MissingClassroomScopesError
    raise before the block runs, as they always have — and held in memory
    only. Callers are unchanged by the broker swap (issue #30).

    Usage:
        with with_classroom_session() as classroom:
            courses = classroom.courses_list()

    Raises (never a bare 500 — routes/handlers translate these):
        CampusSessionExpiredError: the campus sign-in is too stale to
            release a token; the user must sign in again.
        NotConnectedError: user has not connected google.classroom via the
            campus-profile integrations page yet.
        MissingClassroomScopesError: the grant lacks `required_scopes`;
            `err.missing` feeds the reconnect guidance (reconnect via the
            campus-profile integrations page).

    Pass `required_scopes=()` to skip the scope gate for calls that only
    need whatever was granted.
    """
    required = tuple(required_scopes) if required_scopes is not None else CLASSROOM_SCOPES_MVP
    client = ClassroomClient(required)
    client._ensure_token()
    try:
        yield client
    finally:
        client.close()
