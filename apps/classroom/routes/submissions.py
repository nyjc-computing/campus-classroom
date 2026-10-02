"""campus.apps.classroom.routes.submissions

API routes for Submission resources.

All data is stored via Campus API - no local database.
"""

import flask

import campus.model


def register_routes(app: flask.Flask, login_manager):
    """Register submission routes with the Flask app.

    Args:
        app: The Flask application
        login_manager: The OAuthLoginManager for authentication
    """

    @app.get("/api/v1/submissions")
    @login_manager.login_required
    def list_submissions(**_):
        """List submissions visible to the current user.

        Access rules (PRD §6.9): a user sees their own submissions, or —
        when filtering by assignment_id — the submissions of an
        assignment they own (teacher review). Requesting another
        student's submissions answers 404 without revealing data.

        Query parameters:
            - assignment_id: Filter by assignment (must be owned by the caller)
            - student_id: Filter by student (must be the caller)
            - course_id: Filter by Google Classroom course (combined with
              the default own-submissions scope)

        Returns:
            JSON array of submissions
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        assignment_id = flask.request.args.get("assignment_id")
        student_id = flask.request.args.get("student_id")
        course_id = flask.request.args.get("course_id")

        if student_id and student_id != user_id:
            return flask.jsonify({"error": "Submission not found"}), 404
        if not student_id and not assignment_id:
            student_id = user_id

        with campus.with_user_session() as client:
            if assignment_id:
                try:
                    assignment = client.api.assignments[assignment_id].get()
                except Exception as e:
                    if "not found" in str(e).lower():
                        return flask.jsonify({"error": "Assignment not found"}), 404
                    raise
                if str(getattr(assignment, "created_by", "")) != str(user_id):
                    return flask.jsonify({"error": "Assignment not found"}), 404

            submissions = client.api.submissions.list(
                assignment_id=assignment_id,
                student_id=student_id,
                course_id=course_id,
            )

        return flask.jsonify([s.to_resource() for s in submissions])

    @app.post("/api/v1/submissions")
    @login_manager.login_required
    def api_create_submission(**_):
        """Create a new submission.

        Request body:
            - assignment_id: Assignment ID (required)
            - course_id: Google Classroom course ID (required)
            - responses: Array of response objects (optional)

        Returns:
            Created submission as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json()

        if not data:
            return flask.jsonify({"error": "Request body required"}), 400

        if "assignment_id" not in data:
            return flask.jsonify({"error": "assignment_id is required"}), 400

        if "course_id" not in data:
            return flask.jsonify({"error": "course_id is required"}), 400

        with campus.with_user_session() as client:
            submission = client.api.submissions.new(
                assignment_id=data["assignment_id"],
                student_id=user_id,
                course_id=data["course_id"],
                responses=data.get("responses"),
            )

        return flask.jsonify(submission.to_resource()), 201

    @app.get("/api/v1/submissions/<submission_id>")
    @login_manager.login_required
    def api_get_submission(submission_id: str, **_):
        """Get a submission by ID.

        Visible to the submission's student and to the owner of the
        assignment it belongs to (teacher review, PRD §6.9). Anyone else
        gets 404 so the submission's existence is not revealed.

        Returns:
            Submission as JSON, or 404 if not found or not visible
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            try:
                submission = client.api.submissions[submission_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Submission not found"}), 404
                raise

            if str(getattr(submission, "student_id", "")) != str(user_id):
                try:
                    assignment = client.api.assignments[submission.assignment_id].get()
                except Exception as e:
                    if "not found" in str(e).lower():
                        return flask.jsonify({"error": "Submission not found"}), 404
                    raise
                if str(getattr(assignment, "created_by", "")) != str(user_id):
                    return flask.jsonify({"error": "Submission not found"}), 404

        return flask.jsonify(submission.to_resource())

    @app.get("/api/v1/submissions/by-assignment/<assignment_id>")
    @login_manager.login_required
    def api_list_submissions_by_assignment(assignment_id: str, **_):
        """List all submissions for an assignment.

        Assignment-owner only (teacher review, PRD §6.9); anyone else
        gets 404 without revealing the submissions.

        Returns:
            JSON array of submissions
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

            submissions = client.api.submissions.by_assignment(assignment_id)

        return flask.jsonify([s.to_resource() for s in submissions])

    @app.get("/api/v1/submissions/by-student/<student_id>")
    @login_manager.login_required
    def api_list_submissions_by_student(student_id: str, **_):
        """List all submissions by a student.

        Students may list only their own submissions (PRD §6.9); any
        other student_id answers 404 without revealing data.

        Query parameters:
            - course_id: Optional filter by course

        Returns:
            JSON array of submissions
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        course_id = flask.request.args.get("course_id")

        if student_id != user_id:
            return flask.jsonify({"error": "Submission not found"}), 404

        with campus.with_user_session() as client:
            if course_id:
                # Get submissions for this student, filtered by course
                all_submissions = client.api.submissions.by_student(student_id)
                submissions = [s for s in all_submissions if s.course_id == course_id]
            else:
                submissions = client.api.submissions.by_student(student_id)

        return flask.jsonify([s.to_resource() for s in submissions])

    @app.get("/api/v1/submissions/for-student")
    @login_manager.login_required
    def api_get_student_submission(**_):
        """Get the current student's submission for an assignment.

        Query parameters:
            - assignment_id: Assignment ID (required)

        Returns:
            Submission as JSON, or 404 if not found
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        assignment_id = flask.request.args.get("assignment_id")
        if not assignment_id:
            return flask.jsonify({"error": "assignment_id is required"}), 400

        with campus.with_user_session() as client:
            submissions = client.api.submissions.list(
                assignment_id=assignment_id,
                student_id=user_id,
            )
            if not submissions:
                return flask.jsonify({"error": "Submission not found"}), 404

        return flask.jsonify(submissions[0].to_resource())

    @app.patch("/api/v1/submissions/<submission_id>")
    @login_manager.login_required
    def api_update_submission(submission_id: str, **_):
        """Update a submission.

        Request body: Fields to update (responses, feedback, etc.)

        Returns:
            Updated submission as JSON, or 404 if not found
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json()
        if not data:
            return flask.jsonify({"error": "No update data provided"}), 400

        with campus.with_user_session() as client:
            # Check ownership (students can update their own submissions)
            try:
                current = client.api.submissions[submission_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Submission not found"}), 404
                raise

            if str(current.student_id) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            # Build update payload
            updates = {}
            if "responses" in data:
                updates["responses"] = data["responses"]
            if "feedback" in data:
                updates["feedback"] = data["feedback"]
            if "submitted_at" in data:
                updates["submitted_at"] = data["submitted_at"]

            client.api.submissions[submission_id].update(**updates)
            updated = client.api.submissions[submission_id].get()

        return flask.jsonify(updated.to_resource())

    @app.delete("/api/v1/submissions/<submission_id>")
    @login_manager.login_required
    def api_delete_submission(submission_id: str, **_):
        """Delete a submission.

        Returns:
            204 on success, 404 if not found
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            # Check ownership
            try:
                current = client.api.submissions[submission_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Submission not found"}), 404
                raise

            if str(current.student_id) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            client.api.submissions[submission_id].delete()

        return "", 204

    @app.post("/api/v1/submissions/<submission_id>/responses")
    @login_manager.login_required
    def api_submit_response(submission_id: str, **_):
        """Add or update a response for a question.

        Request body:
            - question_id: Question ID (required)
            - response_text: Response text (required)

        Returns:
            Updated submission as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json()
        if not data:
            return flask.jsonify({"error": "Request body required"}), 400

        if "question_id" not in data:
            return flask.jsonify({"error": "question_id is required"}), 400

        if "response_text" not in data:
            return flask.jsonify({"error": "response_text is required"}), 400

        with campus.with_user_session() as client:
            # Check ownership
            try:
                current = client.api.submissions[submission_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Submission not found"}), 404
                raise

            if str(current.student_id) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            client.api.submissions[submission_id].responses.add(
                question_id=data["question_id"],
                response_text=data["response_text"],
            )
            updated = client.api.submissions[submission_id].get()

        return flask.jsonify(updated.to_resource())

    @app.post("/api/v1/submissions/<submission_id>/feedback")
    @login_manager.login_required
    def api_add_feedback(submission_id: str, **_):
        """Add teacher feedback for a question response.

        Replaces any existing feedback for this question.

        Request body:
            - question_id: Question ID (required)
            - feedback_text: Feedback text (required)

        Returns:
            Updated submission as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        data = flask.request.get_json()
        if not data:
            return flask.jsonify({"error": "Request body required"}), 400

        if "question_id" not in data:
            return flask.jsonify({"error": "question_id is required"}), 400

        if "feedback_text" not in data:
            return flask.jsonify({"error": "feedback_text is required"}), 400

        with campus.with_user_session() as client:
            # For MVP, any authenticated user can add feedback
            # In production, verify the user is a teacher for this course

            client.api.submissions[submission_id].feedback.add(
                question_id=data["question_id"],
                feedback_text=data["feedback_text"],
            )
            updated = client.api.submissions[submission_id].get()

        return flask.jsonify(updated.to_resource())

    @app.post("/api/v1/submissions/<submission_id>/submit")
    @login_manager.login_required
    def api_finalize_submission(submission_id: str, **_):
        """Mark submission as submitted (sets submitted_at).

        Returns:
            Updated submission as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            # Check ownership
            try:
                current = client.api.submissions[submission_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Submission not found"}), 404
                raise

            if str(current.student_id) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            client.api.submissions[submission_id].submit()
            updated = client.api.submissions[submission_id].get()

        return flask.jsonify(updated.to_resource())

    @app.post("/api/v1/submissions/<submission_id>/unsubmit")
    @login_manager.login_required
    def api_unsubmit_submission(submission_id: str, **_):
        """Unsubmit a submission (allows student to edit again).

        Returns:
            Updated submission as JSON
        """
        user_id = flask.g.user.id
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            # Check ownership
            try:
                current = client.api.submissions[submission_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Submission not found"}), 404
                raise

            if str(current.student_id) != user_id:
                return flask.jsonify({"error": "Forbidden"}), 403

            # To unsubmit, we clear the submitted_at timestamp
            client.api.submissions[submission_id].update(submitted_at=None)
            updated = client.api.submissions[submission_id].get()

        return flask.jsonify(updated.to_resource())


__all__ = ["register_routes"]
