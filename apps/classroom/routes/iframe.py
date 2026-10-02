"""campus.apps.classroom.routes.iframe

Google Classroom Add-On iframe routes.

These routes render within Google Classroom iframes.
"""

import flask

import campus.model


def register_routes(app: flask.Flask, login_manager):
    """Register iframe routes with the Flask app.

    Args:
        app: The Flask application
        login_manager: The OAuthLoginManager for authentication
    """

    def get_mock_context(**overrides):
        """Get mock Classroom context for local testing.

        In production, these come from URL params provided by Google Classroom.
        For local testing, we use mock values unless overridden.
        """
        defaults = {
            "courseId": "test_course_123",
            "itemId": "test_item_456",
            "itemType": "courseWork",
            "attachmentId": "test_attachment_789",
            "addOnToken": None,
        }
        defaults.update(overrides)
        return defaults

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

        URL parameters (from Google Classroom):
            - courseId: The Classroom course ID
            - itemId: The CourseWork/CourseWorkMaterial ID
            - attachmentId: The add-on attachment ID

        For local testing, these can be omitted to use mock values.
        """
        user_id = flask.g.user.id
        context = get_mock_context(
            courseId=flask.request.args.get("courseId"),
            itemId=flask.request.args.get("itemId"),
            attachmentId=flask.request.args.get("attachmentId"),
        )

        return flask.render_template(
            "addon/teacher.html",
            user_id=user_id,
            context=context,
        )

    @app.get("/addon/student")
    @login_manager.login_required
    def addon_student(**_):
        """Student View iframe - complete assignment.

        URL parameters (from Google Classroom):
            - courseId: The Classroom course ID
            - itemId: The CourseWork ID
            - attachmentId: The add-on attachment ID

        For local testing, these can be omitted to use mock values.
        """
        user_id = flask.g.user.id
        context = get_mock_context(
            courseId=flask.request.args.get("courseId"),
            itemId=flask.request.args.get("itemId"),
            attachmentId=flask.request.args.get("attachmentId"),
        )

        return flask.render_template(
            "addon/student.html",
            user_id=user_id,
            context=context,
        )

    @app.get("/addon/review")
    @login_manager.login_required
    def addon_review(**_):
        """Student Work Review iframe - teacher reviews submissions.

        URL parameters (from Google Classroom):
            - courseId: The Classroom course ID
            - itemId: The CourseWork ID
            - attachmentId: The add-on attachment ID
            - submissionId: The student submission ID

        For local testing, these can be omitted to use mock values.
        """
        user_id = flask.g.user.id
        context = get_mock_context(
            courseId=flask.request.args.get("courseId"),
            itemId=flask.request.args.get("itemId"),
            attachmentId=flask.request.args.get("attachmentId"),
            submissionId=flask.request.args.get("submissionId"),
        )

        return flask.render_template(
            "addon/review.html",
            user_id=user_id,
            context=context,
        )


__all__ = ["register_routes"]
