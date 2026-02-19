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

    @app.get("/addon/teacher")
    @login_manager.login_required
    def addon_teacher(user_id, campus, **_):
        """Teacher View iframe - create and edit assignments.

        URL parameters (from Google Classroom):
            - courseId: The Classroom course ID
            - itemId: The CourseWork/CourseWorkMaterial ID
            - attachmentId: The add-on attachment ID

        For local testing, these can be omitted to use mock values.
        """
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
    def addon_student(user_id, campus, **_):
        """Student View iframe - complete assignment.

        URL parameters (from Google Classroom):
            - courseId: The Classroom course ID
            - itemId: The CourseWork ID
            - attachmentId: The add-on attachment ID

        For local testing, these can be omitted to use mock values.
        """
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
    def addon_review(user_id, campus, **_):
        """Student Work Review iframe - teacher reviews submissions.

        URL parameters (from Google Classroom):
            - courseId: The Classroom course ID
            - itemId: The CourseWork ID
            - attachmentId: The add-on attachment ID
            - submissionId: The student submission ID

        For local testing, these can be omitted to use mock values.
        """
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
