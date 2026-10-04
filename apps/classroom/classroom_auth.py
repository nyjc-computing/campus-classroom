"""apps.classroom.classroom_auth

Google Classroom auth bridge (issue #9; broker swap issue #30).

Campus.auth is the sole custodian of Google Classroom credentials (the
namespaced `google.classroom` integration; design campus#730 §2.4–2.5,
tracker campus#733). This module releases the signed-in user's Classroom
access token from campus.auth's token broker so other sessions can call
`with_classroom_session()` and get a ready Classroom API client.

Architecture (docs/auth-bridge.md has the full rationale):

- Token release: `with_classroom_session()` asks the campus.auth token
  broker through the client library (`auth.broker.token("google",
  "classroom", ...)`, api#82) inside the user's campus session
  (`campus.with_user_session()`, which resolves the campus bearer the
  broker requires and refreshes it when expired). The broker answers
  with a live access token, its expiry and its scope — never a refresh
  token. When it nears expiry the app simply asks the broker again
  (campus refreshes its stored credential silently server-side).
  Tokens live in memory for at most one request's duration; nothing is
  persisted anywhere.
- Connecting: the user grants the `google.classroom` integration once via
  the campus-profile integrations page (campus.auth's connect flow). This
  app has no connect UX of its own; NotConnectedError points there.
- Identity mapping is enforced campus-side at connect time (consenting
  Google email must equal the campus session user), so the release path
  needs no local identity check.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

import flask
import requests
from campus_python.errors import (
    APIError as CampusClientError,
    AuthenticationError as CampusAuthenticationError,
    MalformedResponseError as CampusMalformedResponseError,
    NotFoundError as CampusNotFoundError,
)

# --- Scope inventory (PRD §6.4 MVP set, issue #9) ---------------------------

# Identity scopes: not requested by any classroom call, but part of the
# google.classroom vault grant (enforced campus-side at connect), so they
# stay in the broker ask-floor below for documentation.
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


# Refresh the access token this many seconds before it actually expires.
_EXPIRY_SKEW_SECONDS = 60

# Public preview version for userProfiles.checkUserCapability (PRD §6.7).
_CAPABILITY_PREVIEW_VERSION = "V1_20240930_PREVIEW"

_HTTP_TIMEOUT = 30

# Google Classroom REST API root (the only Google endpoint this app calls
# directly; tokens for it come from the campus.auth broker).
CLASSROOM_API_BASE = "https://classroom.googleapis.com"

# --- campus.auth token broker (issue #30, library resource api#82) -----------

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


class OAuthFlowError(ClassroomAuthError):
    """The campus.auth broker could not be reached or returned a response
    that does not carry a usable access token."""

    code = "classroom_oauth_flow_error"


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


class ClassroomAPIError(ClassroomAuthError):
    """A Classroom REST API call failed after any token-refresh retry."""

    code = "classroom_api_error"

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"Classroom API call failed ({status_code}): {detail}")


def missing_scopes(required, granted) -> list[str]:
    """Scopes in `required` that are absent from `granted` (order-stable)."""
    granted_set = set(granted or ())
    return [scope for scope in required if scope not in granted_set]


# --- campus.auth broker access (issue #30, library resource api#82) -----------

def _broker_error(err: CampusClientError) -> ClassroomAuthError:
    """Translate a library APIError from the broker release into the app
    error the routes translate for users (issue #30 semantics preserved):

        404 no connected credential   -> NotConnectedError (profile pointer)
        401 bad/expired campus bearer -> CampusSessionExpiredError (re-login)
        403 + details.missing_scopes  -> MissingClassroomScopesError
        400 AUTH_INVALID_SCOPE / other 403 -> BrokerConfigError, logged
            loudly: an allowlist/vault-cap/bridge-flag mismatch is a
            deployment configuration bug and must not be swallowed.
    """
    if isinstance(err, CampusNotFoundError):
        return NotConnectedError()
    if isinstance(err, CampusAuthenticationError):
        return CampusSessionExpiredError()
    missing = (err.details or {}).get("missing_scopes")
    if missing:
        return MissingClassroomScopesError(list(missing), [])
    flask.current_app.logger.error(
        "campus.auth broker rejected the google.classroom release "
        "(HTTP %s, %s): %s",
        err.status_code, err.error or "no error code", err,
    )
    return BrokerConfigError(
        "Campus refused the Classroom token request "
        f"(HTTP {err.status_code}, {err.error or 'no error code'}); this is a "
        "deployment configuration problem."
    )


def _broker_release(min_scopes: list[str]) -> dict:
    """Release the user's google.classroom token via the client library.

    `campus.with_user_session()` supplies the campus bearer the broker
    requires, refreshing an expired one itself — the dance the retired
    `_campus_bearer()` used to hand-roll. Transport failures and
    unusable releases map to OAuthFlowError; APIErrors go through
    `_broker_error` so callers keep their issue #30 error contract.
    """
    campus = flask.current_app.campus
    try:
        with campus.with_user_session() as client:
            data = client.auth.broker.token(
                "google", "classroom", min_scopes=min_scopes or None,
            )
    except CampusMalformedResponseError as err:
        raise OAuthFlowError(
            "Campus token broker returned a malformed response."
        ) from err
    except CampusClientError as err:
        raise _broker_error(err) from err
    except requests.RequestException as err:
        raise OAuthFlowError(
            "Could not reach the Campus token broker; try again shortly."
        ) from err

    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthFlowError(
            "Campus token broker returned a malformed response."
        )
    return data


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
