"""apps.classroom.campus_session

Campus user-session seam (issue #98).

Every route that needs the signed-in user's Campus credentials opens
`campus.with_user_session()`, which mints a bearer through the
login-session token route on each use. When the user's stored campus
grant is gone or unrefreshable (or the login session itself has
expired), that mint fails with an OAuth `invalid_grant` 400 —
"No campus grant … re-authentication is required" — which used to
escape as a raw BadRequestError and 500 every affected route while the
Flask session stayed valid (a student working longer than the grant
lives hit it on every submit/save).

`user_session()` wraps `with_user_session()` and translates that one
error into `classroom_auth.CampusSessionExpiredError`, the app's
existing "sign in to Campus again" contract: the app-wide errorhandler
(turn routes/classroom_auth.py) already renders it as 401 JSON on
/api/ paths and a redirect into the campus OAuth flow on browser
paths, so callers need no per-site handling.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import campus_python
import flask
from campus_python.errors import APIError as CampusAPIError

from . import classroom_auth as cauth


def is_reauth_required(err: BaseException) -> bool:
    """True when an SDK error means the Campus grant needs a fresh login.

    The login-session token route reports a dead login session or a
    missing/expired campus grant as OAuth `invalid_grant` (HTTP 400).
    The SDK exposes the OAuth error via `APIError.oauth_error` — from
    the response envelope's details, or recovered from the AUTH_* code
    when production strips them (#87).
    """
    return isinstance(err, CampusAPIError) and err.oauth_error == "invalid_grant"


@contextmanager
def user_session() -> Iterator[campus_python.Campus]:
    """Open the signed-in user's Campus session, as `with_user_session()`.

    Raises the classroom_auth errors on top of the SDK's own: a
    re-authentication-required failure surfaces as
    `cauth.CampusSessionExpiredError` (never a raw 500).
    """
    campus = flask.current_app.campus
    try:
        with campus.with_user_session() as client:
            yield client
    except CampusAPIError as err:
        if is_reauth_required(err):
            raise cauth.CampusSessionExpiredError() from err
        raise


__all__ = ["is_reauth_required", "user_session"]
