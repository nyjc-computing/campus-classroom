"""apps.classroom.routes.classroom_auth

Routes for the Google Classroom auth bridge (issue #9; broker swap #30).

- GET  /classroom            connection status (broker-derived) + acceptance
                             demo (courses.list)
- GET  /classroom/authorize  LEGACY (retire lane): start (incremental) Google
                             consent — the connect UX is the campus-profile
                             integrations page now
- GET  /classroom/callback   LEGACY: OAuth callback; its Flask-session token
                             is no longer read by with_classroom_session()
- POST /classroom/disconnect forget the LEGACY stored Google credential

An app-wide errorhandler converts ClassroomAuthError into a clean JSON
response (API/addon paths) or a flash + redirect (browser paths), so a
missing connection or missing scope never surfaces as a 500.
"""

import secrets

import campus.common.utils.url as campus_url
import flask

from .. import classroom_auth as cauth


def _is_safe_redirect(target: str) -> bool:
    """Only allow relative URLs (same rule as flask_campus login)."""
    return target.startswith("/") and not target.startswith("//")


def _allowlisted_scopes() -> set[str]:
    """Scopes the `?scopes=` narrowing parameter may request: the MVP set
    plus feature scopes (issue #10 Send-to-Classroom grants the courses
    write scope incrementally; the default connect request stays MVP)."""
    return set(cauth.CLASSROOM_SCOPES_MVP) | set(cauth.CLASSROOM_SCOPES_SEND)


def register_routes(app: flask.Flask, login_manager):
    """Register Classroom auth bridge routes with the Flask app."""

    bp = flask.Blueprint("classroom_auth", __name__, url_prefix="/classroom")

    @app.errorhandler(cauth.ClassroomAuthError)
    def handle_classroom_auth_error(err: cauth.ClassroomAuthError):
        """No auth-bridge failure may surface as a 500.

        Browser paths get a redirect to the connection page (or straight to
        the incremental consent screen when scopes are missing); API and
        iframe paths get a machine-readable JSON error.
        """
        wants_json = (
            flask.request.path.startswith(("/api/", "/addon/"))
            or flask.request.accept_mimetypes.best == "application/json"
        )
        if isinstance(err, cauth.CampusSessionExpiredError):
            # The broker needs a live campus bearer; none can be obtained.
            # Browser paths re-enter the campus OAuth flow, API paths get
            # the machine-readable 401.
            if wants_json:
                return flask.jsonify(
                    {"error": {"code": err.code, "message": err.message}}
                ), 401
            flask.flash(err.message, "warning")
            return flask.redirect(
                flask.url_for("auth.login", next=flask.request.path)
            )
        if wants_json:
            payload: dict = {"error": {"code": err.code, "message": err.message}}
            if isinstance(err, cauth.MissingClassroomScopesError):
                payload["error"]["missing_scopes"] = err.missing
                payload["error"]["authorize_url"] = flask.url_for(
                    "classroom_auth.authorize",
                    next=flask.request.url,
                    scopes=" ".join(err.missing),
                )
            status = 403 if isinstance(
                err, (cauth.MissingClassroomScopesError, cauth.NotConnectedError)
            ) else 502
            return flask.jsonify(payload), status

        if isinstance(err, (cauth.NotConnectedError, cauth.MissingClassroomScopesError)):
            flask.flash(err.message, "warning")
            kwargs: dict = {"next": flask.request.full_path}
            if isinstance(err, cauth.MissingClassroomScopesError):
                kwargs["scopes"] = " ".join(err.missing)
            return flask.redirect(flask.url_for("classroom_auth.authorize", **kwargs))

        app.logger.error("Classroom auth error (%s): %s", err.code, err.message)
        flask.flash(err.message, "danger")
        return flask.redirect(flask.url_for("classroom_auth.connection"))

    @bp.get("/")
    @login_manager.login_required
    def connection(**_):
        """Connection status page + courses.list() acceptance demo.

        Status is broker-derived (issue #30): campus.auth is the source of
        truth for the user's google.classroom grant, and nothing is read
        from or written to the Flask session. The probe asks for the
        connected grant without a minimum so the page can show exactly
        what was granted; errors render as page states, never 500s.
        """
        connected: bool | None = None
        connected_email = None
        granted: list[str] = []
        missing = list(cauth.CLASSROOM_SCOPES_MVP)
        status_error = None
        try:
            creds = cauth.fetch_broker_credential()
            connected = True
            connected_email = creds["email"]
            granted = creds["scopes"]
            missing = cauth.missing_scopes(cauth.CLASSROOM_SCOPES_MVP, granted)
        except cauth.NotConnectedError:
            connected = False
        except cauth.CampusSessionExpiredError:
            # Re-login is the only remedy; let the errorhandler redirect
            # browser paths straight into the campus OAuth flow.
            raise
        except cauth.ClassroomAuthError as err:
            status_error = err.message

        courses = None
        courses_error = None
        if connected:
            try:
                with cauth.with_classroom_session() as classroom:
                    courses = classroom.courses_list()
            except cauth.ClassroomAuthError as err:
                courses_error = err.message

        return flask.render_template(
            "classroom/connection.html",
            connected=connected,
            connected_email=connected_email,
            granted_scopes=granted,
            missing_scopes=missing,
            status_error=status_error,
            profile_integrations_url=(
                f"{cauth.profile_integrations_url()}/profile/integrations"
            ),
            courses=courses,
            courses_error=courses_error,
        )

    @bp.get("/authorize")
    @login_manager.login_required
    def authorize(**_):
        """Start Google consent; `scopes` narrows the request to a
        previously-missing subset for incremental approval."""
        scopes_param = flask.request.args.get("scopes", "")
        if scopes_param:
            allowed = _allowlisted_scopes()
            scopes = tuple(
                s for s in scopes_param.replace(",", " ").split() if s in allowed
            ) or cauth.CLASSROOM_SCOPES_MVP
        else:
            scopes = cauth.CLASSROOM_SCOPES_MVP

        next_url = flask.request.args.get("next") or flask.url_for(
            "classroom_auth.connection"
        )
        if not _is_safe_redirect(next_url):
            next_url = flask.url_for("classroom_auth.connection")

        state = secrets.token_urlsafe(32)
        try:
            auth_url = cauth.build_authorization_url(
                redirect_uri=campus_url.full_url_for("classroom_auth.callback"),
                scopes=cauth.GOOGLE_IDENTITY_SCOPES + tuple(scopes),
                state=state,
                login_hint=cauth.campus_user_email(),
            )
        except cauth.ClassroomAuthError as err:
            flask.flash(err.message, "danger")
            return flask.redirect(flask.url_for("classroom_auth.connection"))

        flask.session["classroom_oauth_state"] = state
        flask.session["classroom_auth_next"] = next_url
        return flask.redirect(auth_url)

    @bp.get("/callback")
    def callback():
        """OAuth callback: verify state, exchange code, enforce identity."""
        if flask.request.args.get("error"):
            flask.flash(
                "Google Classroom connection was cancelled — the app will "
                "keep working without Classroom features.",
                "warning",
            )
            return flask.redirect(flask.url_for("classroom_auth.connection"))

        code = flask.request.args.get("code", "")
        state = flask.request.args.get("state", "")
        expected_state = flask.session.pop("classroom_oauth_state", None)
        next_url = flask.session.pop("classroom_auth_next", None) or flask.url_for(
            "classroom_auth.connection"
        )
        if not _is_safe_redirect(next_url):
            next_url = flask.url_for("classroom_auth.connection")

        if not code or not state or state != expected_state:
            # Loud refusal: state mismatch is CSRF or a stale/out-of-band
            # visit — do not proceed regardless of the code's validity.
            app.logger.warning(
                "Classroom OAuth callback rejected (state mismatch=%s)",
                state != expected_state,
            )
            return (
                flask.render_template(
                    "classroom/callback_rejected.html",
                    reason="This sign-in attempt could not be verified "
                           "(invalid or expired state). Close this page and "
                           "try connecting again.",
                ),
                400,
            )

        try:
            creds = cauth.connect_from_callback(
                code,
                redirect_uri=campus_url.full_url_for("classroom_auth.callback"),
            )
        except cauth.IdentityMismatchError as err:
            # Identity mapping enforcement: refuse loudly, store nothing.
            app.logger.error(
                "Classroom identity mismatch refused: campus=%s google=%s",
                err.campus_email,
                err.google_email,
            )
            return (
                flask.render_template(
                    "classroom/callback_rejected.html",
                    reason=err.message,
                ),
                403,
            )
        except cauth.ClassroomAuthError as err:
            app.logger.error("Classroom OAuth callback failed: %s", err.message)
            flask.flash(err.message, "danger")
            return flask.redirect(flask.url_for("classroom_auth.connection"))

        flask.flash(
            f"Google account {creds['email']} connected to Campus Classroom.",
            "success",
        )
        return flask.redirect(next_url)

    @bp.post("/disconnect")
    @login_manager.login_required
    def disconnect(**_):
        cauth.clear_credentials()
        flask.flash("Google Classroom disconnected.", "info")
        return flask.redirect(flask.url_for("classroom_auth.connection"))

    app.register_blueprint(bp)
