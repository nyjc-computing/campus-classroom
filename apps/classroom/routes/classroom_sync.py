"""apps.classroom.routes.classroom_sync

Send-to-Classroom routes (issue #10, PRD §6.7 primary flow).

- GET  /api/v1/classroom/courses                teacher's courses for the picker
- POST /api/v1/assignments/<id>/classroom/send  post the assignment to N courses

Not-connected / missing-scope failures raise ClassroomAuthError and are
translated by the app-wide errorhandler (JSON for /api/ paths, including
missing_scopes + authorize_url for the incremental re-consent redirect).
"""

import flask

from .. import classroom_auth as cauth
from .. import classroom_sync as csync

# Send-to-Classroom needs everything the MVP connect grant covers plus the
# coursework write scope (draft CourseWork creation) — see
# classroom_auth.CLASSROOM_SCOPES_SEND for why it is not part of MVP.
_SEND_SCOPES = cauth.CLASSROOM_SCOPES_MVP + cauth.CLASSROOM_SCOPES_SEND


def register_routes(app: flask.Flask, login_manager):
    """Register the classroom-sync routes with the Flask app."""

    @app.get("/api/v1/classroom/courses")
    @login_manager.login_required
    def api_classroom_courses(**_):
        """Teacher's Classroom courses, for course pickers (courses.list)."""
        with cauth.with_classroom_session() as classroom:
            courses = classroom.courses_list(teacher_only=True)
        return flask.jsonify({"courses": [
            {
                "id": course.get("id", ""),
                "name": course.get("name", ""),
                "section": course.get("section", ""),
            }
            for course in courses
        ]})

    @app.post("/api/v1/assignments/<assignment_id>/classroom/send")
    @login_manager.login_required
    def api_send_to_classroom(assignment_id: str, **_):
        """Post this assignment to the selected Classroom courses.

        Request body: {"course_ids": ["<courseId>", ...]} (non-empty).

        Per PRD §6.7 each course gets a draft CourseWork plus an add-on
        attachment (eligible teacher) or a Link Material fallback; every
        link is persisted to Campus (GC-8). Returns one outcome per course;
        a failing course never aborts the rest. The assignment locks for
        editing once any link is stored (existing lock semantics).
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json(silent=True) or {}
        course_ids = data.get("course_ids")
        if (not isinstance(course_ids, list)
                or not course_ids
                or not all(isinstance(c, str) and c.strip() for c in course_ids)):
            return flask.jsonify({
                "error": "course_ids must be a non-empty list of Google "
                         "Classroom course IDs",
            }), 400

        with (cauth.with_classroom_session(required_scopes=_SEND_SCOPES) as classroom,
              campus.with_user_session() as campus_client):
            try:
                assignment = campus_client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise

            if str(assignment.created_by) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            outcomes = csync.send_assignment_to_classroom(
                campus_client, classroom, assignment,
                [c.strip() for c in course_ids],
            )

        return flask.jsonify({"results": outcomes})


__all__ = ["register_routes"]
