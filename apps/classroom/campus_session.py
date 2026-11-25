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

import logging
from contextlib import contextmanager
from typing import Iterator

import campus_python
import flask
from campus_python.errors import (
    AccessDeniedError,
    AuthenticationError,
    RateLimitExceededError,
    ServerError,
    UnavailableError,
)
from campus_python.errors import APIError as CampusAPIError

from . import classroom_auth as cauth

logger = logging.getLogger(__name__)


def is_reauth_required(err: BaseException) -> bool:
    """True when an SDK error means the Campus grant needs a fresh login.

    The login-session token route reports a dead login session or a
    missing/expired campus grant as OAuth `invalid_grant` (HTTP 400).
    The SDK exposes the OAuth error via `APIError.oauth_error` — from
    the response envelope's details, or recovered from the AUTH_* code
    when production strips them (#87).

    Two more shapes mean the same thing and surfaced as 500s on the
    submission review page (2026-11-13): the flask session can lose its
    login/session ids entirely (the SDK raises AuthenticationError
    before any HTTP call), and a login-session row that died server-side
    makes the auth store answer the mint's session lookup with an
    AccessDeniedError for sessions:read. Both are re-auth, not retry.
    """
    if isinstance(err, CampusAPIError) and err.oauth_error == "invalid_grant":
        return True
    text = _error_text(err).lower()
    if isinstance(err, AuthenticationError) and "login session id" in text:
        return True
    return isinstance(err, AccessDeniedError) and "sessions:read" in text


def _error_text(err: BaseException) -> str:
    """Message text that survives the SDK's arg conventions.

    str(err) is empty when the message was passed into a non-standard
    constructor slot; the traceback-visible args always carry it.
    """
    return str(err) or " ".join(str(a) for a in err.args)


@contextmanager
def app_session() -> Iterator[campus_python.Campus]:
    """Open the app-scoped Campus session, as `with_app_session()`.

    Same contract as the SDK context manager, but the client-credentials
    mint runs on a throwaway Campus — same structural reason as
    `_broker_release` (classroom_auth.py): a session block on the shared
    app-wide instance leaves that user's or the app's bearer on it for
    the block's lifetime, and any interleaving request thread's
    flask_campus resolution (`push_context`) then presents it. The auth
    service denies user principals on session-flow routes (#854/#1055),
    the push hook degrades that request to anonymous (#28), and the
    browser loops into /login (classroom#153; prod gthread workers make
    the interleaving routine). Keep every with_*_session off
    `flask.current_app.campus` unless the SDK gains per-request
    principal scoping (campus-api-python#189).
    """
    with campus_python.Campus(timeout=60).with_app_session() as client:
        yield client


@contextmanager
def user_session() -> Iterator[campus_python.Campus]:
    """Open the signed-in user's Campus session, as `with_user_session()`.

    Runs on a throwaway Campus, never `flask.current_app.campus` — same
    structural reason as app_session() above (classroom#153).

    Raises the classroom_auth errors on top of the SDK's own: a
    re-authentication-required failure surfaces as
    `cauth.CampusSessionExpiredError` (never a raw 500).

    A rejected mint is retried once before the session is declared
    expired (#98 investigation, 2026-11-10): a transient failure on the
    auth service's token store surfaces with the exact same
    invalid_grant error a dead grant produces, and one immediate retry
    papers over the transient case — a grant that is genuinely gone
    fails the retry too, and re-authentication remains the remedy.

    A reauth-shaped failure raised by the CALLER'S BODY (after the
    mint already succeeded — e.g. a sessions:read denial from an SDK
    call) must NOT take the retry path: contextlib forbids a second
    yield after throw(), so the retry would turn the error into a
    `RuntimeError: generator didn't stop after throw()` 500. Re-raising
    as CampusSessionExpiredError keeps the #98 contract instead (#139
    harness found this latent path).
    """
    campus = campus_python.Campus(timeout=60)
    body_entered = False
    try:
        with campus.with_user_session() as client:
            body_entered = True
            yield client
    except CampusAPIError as err:
        if not is_reauth_required(err):
            raise
        if body_entered:
            raise cauth.CampusSessionExpiredError() from err
        logger.warning(
            "Campus grant mint rejected (%s: %s); retrying once before "
            "treating the session as expired (#98)",
            err.oauth_error or "unknown_error", err,
        )
        try:
            with campus.with_user_session() as client:
                yield client
        except CampusAPIError as retry_err:
            if is_reauth_required(retry_err):
                raise cauth.CampusSessionExpiredError() from retry_err
            raise


def register_error_handlers(app: flask.Flask) -> None:
    """No Campus API failure may surface as a raw 500 (#139).

    The 2026-11-22 prod incident: a student's submission create died on
    the missing `submissions.admin` circle grant as an uncaught
    AccessDeniedError — Flask 500 HTML, so the answer sheet's
    `formatApiError({})` had nothing to show but its generic "Could not
    start your submission" fallback. The friendly message lives HERE,
    not per-route: every surface (student /a/ writes, staff pages, and
    flask_campus hook-phase failures like #126's context-push denials —
    those funnel through Flask's handle_exception too) gets the same
    contract, and `formatApiError` already surfaces server messages.

    Runs LAST in the error pipeline: reauth-shaped errors became
    CampusSessionExpiredError inside user_session() (#98) and routes
    translate their own not-founds, so what lands here is genuinely
    unexpected. Reauth-shaped errors are still honoured if one escapes
    the seam (direct with_user_session calls, hook phase).
    """

    @app.errorhandler(CampusAPIError)
    def handle_campus_api_error(err: CampusAPIError):
        wants_json = (
            flask.request.path.startswith("/api/")
            or flask.request.accept_mimetypes.best == "application/json"
        )
        if is_reauth_required(err):
            # Mirror the #98 contract: the remedy is a fresh Campus
            # sign-in, not a retry or a grant request.
            expired = cauth.CampusSessionExpiredError()
            if wants_json:
                return flask.jsonify(
                    {"error": {"code": expired.code, "message": expired.message}}
                ), 401
            flask.flash(expired.message, "warning")
            return flask.redirect(
                flask.url_for("auth.login",
                              next=flask.request.full_path.rstrip("?"))
            )

        if isinstance(err, AccessDeniedError):
            code = "campus_access_denied"
            status = 403
            message = (
                "Campus denied this action for your account (missing "
                "access grant). If you think you should have access, "
                "sign in again, or ask your teacher or a Campus admin "
                "to grant it."
            )
        elif isinstance(err, RateLimitExceededError):
            code = "campus_rate_limited"
            status = 429
            message = "Too many requests — wait a moment and try again."
        elif isinstance(err, (ServerError, UnavailableError)):
            code = "campus_api_unavailable"
            status = 502
            message = "Campus is having a problem right now — try again in a moment."
        else:
            code = f"campus_{(err.error or 'api_error').lower()}"
            status = err.status_code if 400 <= err.status_code < 600 else 502
            message = err.error_description or _error_text(err) or "Campus API error"

        app.logger.warning(
            "Campus API error on %s %s: %s (%s, status=%s, request_id=%s)",
            flask.request.method, flask.request.path,
            _error_text(err), err.error, err.status_code, err.request_id,
        )
        if wants_json:
            payload: dict = {"error": {"code": code, "message": message}}
            if err.request_id:
                payload["error"]["request_id"] = err.request_id
            field_errors = getattr(err, "errors", None)
            if field_errors:
                payload["error"]["errors"] = [
                    {"field": fe.field, "code": fe.code, "message": fe.message}
                    for fe in field_errors
                ]
            return flask.jsonify(payload), status

        flask.flash(message, "danger")
        return flask.redirect(
            flask.request.referrer or flask.url_for("index")
        )


__all__ = ["is_reauth_required", "register_error_handlers", "user_session"]
