"""apps.classroom.routes.classroom_auth

Routes for the Google Classroom auth bridge (issue #9; broker swap #30,
legacy in-app OAuth flow retired #30).

- GET /classroom  connection status (broker-derived) + acceptance demo
                  (courses.list)

An app-wide errorhandler converts ClassroomAuthError into a clean JSON
response (API/addon paths) or a flash + redirect (browser paths), so a
missing connection or missing scope never surfaces as a 500. Remediation
for both is the campus-profile integrations page — the only connect UX
since campus.auth became the sole custodian of Google Classroom tokens.
"""

import flask

from .. import classroom_auth as cauth


def register_routes(app: flask.Flask, login_manager):
    """Register Classroom auth bridge routes with the Flask app."""

    bp = flask.Blueprint("classroom_auth", __name__, url_prefix="/classroom")

    def _profile_integrations_url() -> str:
        return f"{cauth.profile_integrations_url()}/profile/integrations"

    @app.errorhandler(cauth.ClassroomAuthError)
    def handle_classroom_auth_error(err: cauth.ClassroomAuthError):
        """No auth-bridge failure may surface as a 500.

        Browser paths get a flash + redirect to the connection page (whose
        reconnect CTA points at the campus-profile integrations page);
        API and iframe paths get a machine-readable JSON error carrying
        the same reconnect URL.
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
                payload["error"]["reconnect_url"] = _profile_integrations_url()
            status = 403 if isinstance(
                err, (cauth.MissingClassroomScopesError, cauth.NotConnectedError)
            ) else 502
            return flask.jsonify(payload), status

        reconnectable = isinstance(
            err, (cauth.NotConnectedError, cauth.MissingClassroomScopesError)
        )
        if reconnectable:
            # The connection page renders the state truthfully (missing
            # scopes included) with the profile-page reconnect CTA.
            flask.flash(err.message, "warning")
            return flask.redirect(flask.url_for("classroom_auth.connection"))

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
            profile_integrations_url=_profile_integrations_url(),
            courses=courses,
            courses_error=courses_error,
        )

    app.register_blueprint(bp)
