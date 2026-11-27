"""campus.apps.classroom.routes.assignments

API routes for Assignment resources.

All data is stored via Campus API - no local database.
"""

from dataclasses import asdict

import flask
from campus_python.errors import APIError

from .. import campus_session, enrollment
from .. import classroom_auth as cauth


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


def _link_state(err: cauth.ClassroomAuthError) -> str:
    """Map a bridge failure to the listing's X-Classroom-Link-State value
    (#180): the dashboard must tell "not connected" (and "needs a
    reconnect") apart from a genuinely empty class instead of showing a
    bare "No assignments yet" for both.
    """
    if isinstance(err, cauth.NotConnectedError):
        return "not-connected"
    if isinstance(err, cauth.MissingClassroomScopesError):
        return "reconnect-needed"
    if isinstance(err, cauth.CampusSessionExpiredError):
        return "reauth-needed"
    return "unavailable"


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
        degradation cannot leak anything — it only ever hides. The WHY
        is not swallowed silently, though (#180): the response carries
        the bridge state in X-Classroom-Link-State so the dashboard can
        tell "not connected" (actionable) apart from a genuinely empty
        class — see _link_state().
        """
        user_id = flask.g.user.id

        try:
            my_courses = enrollment.user_courses()
            link_state = "ok"
        except cauth.ClassroomAuthError as err:
            link_state = _link_state(err)
            flask.current_app.logger.warning(
                "Assignment listing degraded to own-only for %s: %s (%s)",
                user_id, err.code, err.message,
            )
            my_courses = {}

        with campus_session.user_session() as client:
            assignments = client.api.assignments.list()

        visible = [
            a for a in assignments
            if str(getattr(a, "created_by", "")) == str(user_id)
            or enrollment.linked_course_ids(a) & my_courses.keys()
        ]
        visible.sort(key=lambda a: str(getattr(a, "created_at", "")), reverse=True)

        response = flask.jsonify([a.to_resource() for a in visible])
        response.headers["X-Classroom-Link-State"] = link_state
        return response

    @app.get("/api/v1/assignments/<assignment_id>/classroom-states")
    @login_manager.login_required
    def api_assignment_classroom_states(assignment_id: str, **_):
        """Per-student Classroom submission states for the drill-down (#105).

        Reviewers only (creator or co-tutor, #130) — the same gate as
        the submissions drill-down this enriches; anyone else 404s
        without revealing the assignment. For each Classroom course the
        assignment links to, one studentSubmissions.list (all students,
        on the VIEWER's own MVP credential — students.readonly +
        rosters.readonly, no incremental consent) plus a roster map
        joins Google's obfuscated userId to the school email the campus
        rows use:

            {"states": [{email, course_id, coursework_id,
                         submission_id, state}]}

        Degrades explicitly ("states": [] + "degraded": <code>) when the
        auth bridge is unusable (not connected / missing scope /
        Classroom down) — the drill-down must keep rendering its campus
        rows; only the #104 mirror flag goes away.
        CampusSessionExpired still propagates (#98 re-auth contract).
        """
        user_id = flask.g.user.id
        with campus_session.user_session() as client:
            try:
                assignment = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise
            if not enrollment.can_review(assignment, user_id):
                return flask.jsonify({"error": "Assignment not found"}), 404
            links = [
                (link.course_id, link.coursework_id)
                for link in (assignment.classroom_links or [])
                if getattr(link, "course_id", None)
                and getattr(link, "coursework_id", None)
            ]
        if not links:
            return flask.jsonify({"states": [], "degraded": "not_linked"})

        try:
            with cauth.with_classroom_session() as classroom:
                states = []
                for course_id, coursework_id in links:
                    roster = classroom.course_students_email_map(course_id)
                    for gc in classroom.student_submissions_list_all(
                            course_id, coursework_id):
                        email = roster.get(gc.get("userId") or "")
                        if email:
                            states.append({
                                "email": email,
                                "course_id": course_id,
                                "coursework_id": coursework_id,
                                "submission_id": gc.get("id"),
                                "state": gc.get("state"),
                            })
        except cauth.CampusSessionExpiredError:
            raise
        except cauth.ClassroomAuthError as err:
            # Read-only enrichment: degrade explicitly instead of
            # failing the page's data load (same pattern as the
            # rule-4 listing's bridge degradation).
            flask.current_app.logger.warning(
                "Classroom states degraded for assignment %s: %s (%s)",
                assignment_id, err.code, err.message,
            )
            return flask.jsonify({"states": [], "degraded": err.code})

        return flask.jsonify({"states": states})

    @app.post("/api/v1/assignments")
    @login_manager.login_required
    def api_create_assignment(**_):
        """Create a new assignment (teachers only — classroom#147).

        The 2026-11-24 journey investigation saw six students receive
        the creation page; gate the write too, so the page-level check
        is never the only line of defense.

        Request body:
            - title: Assignment title (required)
            - description: Assignment description (optional)
            - questions: Array of question objects (optional)
            - classroom_links: Array of ClassroomLink objects (optional)

        Returns:
            Created assignment as JSON; 403 for a connected non-teacher,
            502 envelope for auth-bridge failures (fail-closed — a
            create must not proceed on an unknown role)
        """
        data = flask.request.get_json()

        if not data or "title" not in data:
            return flask.jsonify({"error": "title is required"}), 400

        try:
            teaches = bool(enrollment.taught_courses())
        except cauth.ClassroomAuthError as e:
            return flask.jsonify({
                "error": {"code": e.code, "message": e.message}
            }), 502
        if not teaches:
            return flask.jsonify(
                {"error": "Only teachers can create assignments"}
            ), 403

        with campus_session.user_session() as client:
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
        with campus_session.user_session() as client:
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
        data = flask.request.get_json()
        if not data:
            return flask.jsonify({"error": "No update data provided"}), 400

        # Check ownership first
        with campus_session.user_session() as client:
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
        with campus_session.user_session() as client:
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
        data = flask.request.get_json()
        if not data or "course_id" not in data or "coursework_id" not in data:
            return flask.jsonify(
                {"error": "course_id and coursework_id are required"}
            ), 400

        with campus_session.user_session() as client:
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
        with campus_session.user_session() as client:
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
