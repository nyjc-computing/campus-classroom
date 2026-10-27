"""campus.apps.classroom.routes.assignments

API routes for Assignment resources.

All data is stored via Campus API - no local database.
"""

from dataclasses import asdict

import flask
from campus_python.errors import APIError

from .. import classroom_auth as cauth
from .. import enrollment


def upstream_error_response(e: APIError):
    """Pass a campus APIError through to the client, preserving the upstream
    status code and error envelope (e.g. 422 VALIDATION_FAILED naming the
    offending questions[i] entry) instead of surfacing an opaque 500.
    """
    error = {
        "code": getattr(e, "error", None) or "UPSTREAM_ERROR",
        "message": e.error_description or "Campus API request failed",
    }
    if getattr(e, "errors", None):
        error["errors"] = [
            {"field": fe.field, "code": fe.code, "message": fe.message}
            for fe in e.errors
        ]
    return flask.jsonify({"error": error}), getattr(e, "status_code", 502)


def register_routes(app: flask.Flask, login_manager):
    """Register assignment routes with the Flask app.

    Args:
        app: The Flask application
        login_manager: The OAuthLoginManager for authentication
    """

    @app.get("/api/v1/assignments")
    @login_manager.login_required
    def list_assignments(**_):
        """List the assignments visible to the current user (PRD §6.9).

        Rule 4 (#14): own assignments (created_by = me) plus assignments
        assigned to the caller via Google Classroom — those whose
        classroom_links include one of their courses. A client-supplied
        created_by filter is ignored, so no caller can enumerate another
        user's assignments; results are the intersection computed here,
        newest first.

        The Classroom course lookup degrades to own-only on any
        auth-bridge failure (no connection, missing scopes, Classroom
        down): this is a discovery listing, and every content read or
        attempt still passes the rule-2/3 enrollment gates, so the
        degradation cannot leak anything — it only ever hides.
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus

        try:
            my_courses = enrollment.user_courses()
        except cauth.ClassroomAuthError as err:
            flask.current_app.logger.warning(
                "Assignment listing degraded to own-only for %s: %s (%s)",
                user_id, err.code, err.message,
            )
            my_courses = {}

        with campus.with_user_session() as client:
            assignments = client.api.assignments.list()

        visible = [
            a for a in assignments
            if str(getattr(a, "created_by", "")) == str(user_id)
            or enrollment.linked_course_ids(a) & my_courses.keys()
        ]
        visible.sort(key=lambda a: str(getattr(a, "created_at", "")), reverse=True)

        return flask.jsonify([a.to_resource() for a in visible])

    @app.post("/api/v1/assignments")
    @login_manager.login_required
    def api_create_assignment(**_):
        """Create a new assignment.

        Request body:
            - title: Assignment title (required)
            - description: Assignment description (optional)
            - questions: Array of question objects (optional)
            - classroom_links: Array of ClassroomLink objects (optional)

        Returns:
            Created assignment as JSON
        """
        campus = flask.current_app.campus
        data = flask.request.get_json()

        if not data or "title" not in data:
            return flask.jsonify({"error": "title is required"}), 400

        with campus.with_user_session() as client:
            try:
                assignment = client.api.assignments.new(
                    title=data["title"],
                    description=data.get("description"),
                    questions=data.get("questions"),
                    classroom_links=data.get("classroom_links"),
                )
            except APIError as e:
                return upstream_error_response(e)

        return flask.jsonify(assignment.to_resource()), 201

    @app.get("/api/v1/assignments/<assignment_id>")
    @login_manager.login_required
    def api_get_assignment(assignment_id: str, **_):
        """Get an assignment by ID.

        Owner-only (PRD §6.9 rule 1). A non-owner gets 404 so the
        assignment's existence is not revealed.

        Returns:
            Assignment as JSON, or 404 if not found or not owned
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            try:
                assignment = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

        if str(getattr(assignment, "created_by", "")) != str(user_id):
            return flask.jsonify({"error": "Assignment not found"}), 404

        return flask.jsonify(assignment.to_resource())

    @app.patch("/api/v1/assignments/<assignment_id>")
    @login_manager.login_required
    def api_update_assignment(assignment_id: str, **_):
        """Update an assignment.

        Request body: Fields to update (title, description, questions, etc.)

        Returns:
            Updated assignment as JSON, or 404 if not found
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json()
        if not data:
            return flask.jsonify({"error": "No update data provided"}), 400

        # Check ownership first
        with campus.with_user_session() as client:
            try:
                current = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(current.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            # Check if assignment is locked (has Classroom links)
            if current.classroom_links:
                return flask.jsonify({
                    "error": "Assignment is locked. Unlink from Google Classroom first."
                }), 409

            # Build update payload with only mutable fields
            updates = {}
            if "title" in data:
                updates["title"] = data["title"]
            if "description" in data:
                updates["description"] = data["description"]
            if "questions" in data:
                updates["questions"] = data["questions"]
            if "classroom_links" in data:
                updates["classroom_links"] = data["classroom_links"]

            try:
                client.api.assignments[assignment_id].update(**updates)
            except APIError as e:
                return upstream_error_response(e)
            updated = client.api.assignments[assignment_id].get()

        return flask.jsonify(updated.to_resource())

    @app.delete("/api/v1/assignments/<assignment_id>")
    @login_manager.login_required
    def api_delete_assignment(assignment_id: str, **_):
        """Delete an assignment.

        This will cascade delete all associated submissions.

        Query parameters:
            - force: Set to "true" to bypass warnings

        Returns:
            204 on success, 404 if not found, 409 if locked
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            # Check ownership first
            try:
                current = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(current.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            # Check if assignment is locked (has Classroom links)
            if current.classroom_links:
                force = flask.request.args.get("force", "false").lower() == "true"
                if not force:
                    return flask.jsonify({
                        "error": (
                            f"Assignment is linked to {len(current.classroom_links)} "
                            "Google Classroom class(es). Use ?force=true to confirm "
                            "deletion."
                        ),
                        "classroom_links": [
                            asdict(link) for link in current.classroom_links
                        ],
                    }), 409

            client.api.assignments[assignment_id].delete()

        return "", 204

    @app.post("/api/v1/assignments/<assignment_id>/links")
    @login_manager.login_required
    def api_add_classroom_link(assignment_id: str, **_):
        """Add a Google Classroom link to an assignment.

        Request body:
            - course_id: Google Classroom course ID (required)
            - coursework_id: Google Classroom coursework ID (required)
            - attachment_id: Google Classroom attachment ID (optional)

        Returns:
            Updated assignment as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json()
        if not data or "course_id" not in data or "coursework_id" not in data:
            return flask.jsonify(
                {"error": "course_id and coursework_id are required"}
            ), 400

        with campus.with_user_session() as client:
            # Check ownership
            try:
                current = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(current.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            client.api.assignments[assignment_id].links.add(
                course_id=data["course_id"],
                coursework_id=data["coursework_id"],
                attachment_id=data.get("attachment_id"),
            )
            updated = client.api.assignments[assignment_id].get()

        return flask.jsonify(updated.to_resource())

    @app.delete("/api/v1/assignments/<assignment_id>/links/<course_id>")
    @login_manager.login_required
    def api_remove_classroom_link(assignment_id: str, course_id: str, **_):
        """Remove a Google Classroom link from an assignment.

        Note: This endpoint removes the link from the assignment's classroom_links
        array. The actual Google Classroom assignment is not affected.

        Returns:
            Updated assignment as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            # Check ownership
            try:
                current = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(current.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            # Filter out the link to remove
            updated_links = [
                link
                for link in current.classroom_links
                if link.course_id != course_id
            ]
            links_data = [asdict(link) for link in updated_links]

            client.api.assignments[assignment_id].update(classroom_links=links_data)
            updated = client.api.assignments[assignment_id].get()

        return flask.jsonify(updated.to_resource())


__all__ = ["register_routes"]
