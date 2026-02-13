"""campus.apps.classroom.routes.assignments

API routes for Assignment resources.

All data is stored via Campus API - no local database.
"""

import flask
from dataclasses import asdict

import campus.model


def register_routes(app: flask.Flask, login_manager):
    """Register assignment routes with the Flask app.

    Args:
        app: The Flask application
        login_manager: The OAuthLoginManager for authentication
    """

    @app.get("/api/v1/assignments")
    @login_manager.login_required
    def list_assignments(user_id, campus, **_):
        """List assignments, optionally filtered by teacher.

        Query parameters:
            - created_by: Filter by teacher (defaults to current user)

        Returns:
            JSON array of assignments
        """
        created_by = flask.request.args.get("created_by", user_id)

        with campus.with_user_session(user_id) as client:
            assignments = client.api.v1.assignments.list(created_by=created_by)

        return flask.jsonify([a.to_resource() for a in assignments])

    @app.post("/api/v1/assignments")
    @login_manager.login_required
    def api_create_assignment(user_id, campus, **_):
        """Create a new assignment.

        Request body:
            - title: Assignment title (required)
            - description: Assignment description (optional)
            - questions: Array of question objects (optional)
            - classroom_links: Array of ClassroomLink objects (optional)

        Returns:
            Created assignment as JSON
        """
        data = flask.request.get_json()

        if not data or "title" not in data:
            return flask.jsonify({"error": "title is required"}), 400

        with campus.with_user_session(user_id) as client:
            assignment = client.api.v1.assignments.new(
                title=data["title"],
                description=data.get("description"),
                questions=data.get("questions"),
                classroom_links=data.get("classroom_links"),
            )

        return flask.jsonify(assignment.to_resource()), 201

    @app.get("/api/v1/assignments/<assignment_id>")
    @login_manager.login_required
    def api_get_assignment(assignment_id: str, user_id, campus, **_):
        """Get an assignment by ID.

        Returns:
            Assignment as JSON, or 404 if not found
        """
        with campus.with_user_session(user_id) as client:
            try:
                assignment = client.api.v1.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

        return flask.jsonify(assignment.to_resource())

    @app.patch("/api/v1/assignments/<assignment_id>")
    @login_manager.login_required
    def api_update_assignment(assignment_id: str, user_id: str, campus, **_):
        """Update an assignment.

        Request body: Fields to update (title, description, questions, etc.)

        Returns:
            Updated assignment as JSON, or 404 if not found
        """
        data = flask.request.get_json()
        if not data:
            return flask.jsonify({"error": "No update data provided"}), 400

        # Check ownership first
        with campus.with_user_session(user_id) as client:
            try:
                current = client.api.v1.assignments[assignment_id].get()
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

            client.api.v1.assignments[assignment_id].update(**updates)
            updated = client.api.v1.assignments[assignment_id].get()

        return flask.jsonify(updated.to_resource())

    @app.delete("/api/v1/assignments/<assignment_id>")
    @login_manager.login_required
    def api_delete_assignment(assignment_id: str, user_id: str, campus, **_):
        """Delete an assignment.

        This will cascade delete all associated submissions.

        Query parameters:
            - force: Set to "true" to bypass warnings

        Returns:
            204 on success, 404 if not found, 409 if locked
        """
        with campus.with_user_session(user_id) as client:
            # Check ownership first
            try:
                current = client.api.v1.assignments[assignment_id].get()
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
                        "error": f"Assignment is linked to {len(current.classroom_links)} Google Classroom class(es). "
                                "Use ?force=true to confirm deletion.",
                        "classroom_links": [asdict(link) for link in current.classroom_links],
                    }), 409

            client.api.v1.assignments[assignment_id].delete()

        return "", 204

    @app.post("/api/v1/assignments/<assignment_id>/links")
    @login_manager.login_required
    def api_add_classroom_link(assignment_id: str, user_id: str, campus, **_):
        """Add a Google Classroom link to an assignment.

        Request body:
            - course_id: Google Classroom course ID (required)
            - coursework_id: Google Classroom coursework ID (required)
            - attachment_id: Google Classroom attachment ID (optional)

        Returns:
            Updated assignment as JSON
        """
        data = flask.request.get_json()
        if not data or "course_id" not in data or "coursework_id" not in data:
            return flask.jsonify({"error": "course_id and coursework_id are required"}), 400

        with campus.with_user_session(user_id) as client:
            # Check ownership
            try:
                current = client.api.v1.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(current.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            client.api.v1.assignments[assignment_id].links.add(
                course_id=data["course_id"],
                coursework_id=data["coursework_id"],
                attachment_id=data.get("attachment_id"),
            )
            updated = client.api.v1.assignments[assignment_id].get()

        return flask.jsonify(updated.to_resource())

    @app.delete("/api/v1/assignments/<assignment_id>/links/<course_id>")
    @login_manager.login_required
    def api_remove_classroom_link(assignment_id: str, course_id: str, user_id: str, campus, **_):
        """Remove a Google Classroom link from an assignment.

        Note: This endpoint removes the link from the assignment's classroom_links
        array. The actual Google Classroom assignment is not affected.

        Returns:
            Updated assignment as JSON
        """
        with campus.with_user_session(user_id) as client:
            # Check ownership
            try:
                current = client.api.v1.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(current.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            # Filter out the link to remove
            updated_links = [l for l in current.classroom_links if l.course_id != course_id]
            links_data = [asdict(l) for l in updated_links]

            client.api.v1.assignments[assignment_id].update(classroom_links=links_data)
            updated = client.api.v1.assignments[assignment_id].get()

        return flask.jsonify(updated.to_resource())


__all__ = ["register_routes"]
