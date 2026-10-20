"""campus.apps.classroom.routes.iframe

Google Classroom Add-On iframe routes.

These routes render within Google Classroom iframes. Since issue #12 the
context is no longer trusted from the URL alone: on the deployment every
view validates itself server-side via
courses.courseWork.getAddOnContext (role + post), and mock context is
reachable only under a debug server.
"""

import flask

from .. import classroom_auth as cauth
from .. import iframe_context

_TEMPLATES = {
    "teacher": "addon/teacher.html",
    "student": "addon/student.html",
    "review": "addon/review.html",
}


def register_routes(app: flask.Flask, login_manager):
    """Register iframe routes with the Flask app.

    Args:
        app: The Flask application
        login_manager: The OAuthLoginManager for authentication
    """

    def _context_error_response(err: iframe_context.ContextError):
        app.logger.warning(
            "Iframe context rejected (%s): %s", err.code, err.message
        )
        return (
            flask.jsonify({"error": {"code": err.code, "message": err.message}}),
            err.status,
        )

    def _render_view(view: str):
        """Shared context resolution for the three authed iframe views.

        Deployment path (never debug): parse the camelCase URL params,
        then validate them server-side with getAddOnContext under the
        view's add-on scope — role mismatches and forged params are
        rejected with 403/400. Debug path: the historical mock context,
        which the deployment can never reach (issue #12's mock gate).
        """
        try:
            params = iframe_context.parse_params(view, flask.request.args)
        except iframe_context.ParamError as err:
            return (
                flask.jsonify(
                    {"error": {"code": "invalid_params", "message": str(err)}}
                ),
                400,
            )

        if flask.current_app.debug:
            context = iframe_context.mock_context(view, flask.request.args)
        else:
            try:
                with cauth.with_classroom_session(
                    required_scopes=iframe_context.VIEW_SCOPES[view],
                ) as classroom:
                    context = iframe_context.resolve_real_context(
                        classroom, view, params
                    )
            except iframe_context.ContextError as err:
                return _context_error_response(err)

        return flask.render_template(
            _TEMPLATES[view],
            user_id=flask.g.user.id,
            context=context,
        )

    @app.get("/addon/discovery")
    def addon_discovery():
        """Attachment Discovery iframe - points teachers at the platform.

        Assignment creation is API-first (PRD §6.7, issue #13): teachers
        post from the platform via "Send to Google Classroom" (issue #10),
        so this view offers no in-iframe picker - only instructions for the
        platform flow. Deliberately public: the Campus session cookie is
        not guaranteed inside a partitioned Classroom iframe (issue #12)
        and this view reads no user data, so a login wall here would only
        reproduce the issue #24 sign-in-inside-iframe dead end.

        URL parameters (from Google Classroom) are accepted but unused:
            - courseId: The Classroom course ID
            - itemId: The CourseWork ID being created
            - addOnToken: Authorization token (never rendered or logged)
        """
        return flask.render_template("addon/discovery.html")

    @app.get("/addon/teacher")
    @login_manager.login_required
    def addon_teacher(**_):
        """Teacher View iframe - create and edit assignments.

        Context is resolved server-side (see _render_view): URL params
        courseId / itemId / attachmentId, validated against
        getAddOnContext on the deployment; TEACHER role enforced.
        """
        return _render_view("teacher")

    @app.get("/addon/student")
    @login_manager.login_required
    def addon_student(**_):
        """Student View iframe - complete assignment.

        Context is resolved server-side (see _render_view): URL params
        courseId / itemId / attachmentId, validated against
        getAddOnContext on the deployment; STUDENT role enforced.
        """
        return _render_view("student")

    @app.get("/addon/review")
    @login_manager.login_required
    def addon_review(**_):
        """Student Work Review iframe - teacher reviews submissions.

        Context is resolved server-side (see _render_view): URL params
        courseId / itemId / attachmentId / submissionId, validated against
        getAddOnContext on the deployment; TEACHER role enforced.
        """
        return _render_view("review")


__all__ = ["register_routes"]
