"""campus.apps.classroom.routes.documents

Document-question routes (#62/#78): export challenges for the answer
sheet, and the embed origin's document serving.

/export-challenge lives on the main app: the student's /a/ page calls it
immediately before asking the embedded document to serialize itself, so
every accepted snapshot carries a fresh server-minted nonce.

/embed lives only on the docs deployment: EMBED_MODE=1 gates it, so the
main origin never serves executable teacher HTML (#78). Embed requests
carry no Campus session cookie (different origin), so auth is the
short-lived HMAC token the share page minted into the iframe URL.
"""

import flask

from .. import documents, enrollment


def register_routes(app: flask.Flask, login_manager):
    """Register document routes with the Flask app.

    Args:
        app: The Flask application
        login_manager: The OAuthLoginManager for authentication
    """

    @app.post("/api/v1/assignments/<assignment_id>/export-challenge")
    @login_manager.login_required
    def api_export_challenge(assignment_id: str, **_):
        """Mint the nonce an exported snapshot must carry (#78).

        Enrollment-gated like every other attempt surface (PRD v1.3
        rule 3): only students who can attempt the assignment can ask
        for export authorization.

        Request body:
            - question_id: Question ID (required; must be a document
              question)

        Returns:
            {nonce, expires_at} — expires_at is a unix timestamp
        """
        campus = flask.current_app.campus
        data = flask.request.get_json() or {}
        question_id = data.get("question_id")
        if not question_id:
            return flask.jsonify({"error": "question_id is required"}), 400

        with campus.with_user_session() as client:
            try:
                assignment = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "Assignment not found"}), 404
                raise
            if not enrollment.is_enrolled(assignment):
                return flask.jsonify({
                    "error": "This assignment was not sent to any of your "
                             "Google Classroom courses, so it cannot be "
                             "attempted or submitted.",
                    "code": "not_enrolled",
                }), 403

        question = _find_question(assignment, question_id)
        if question is None or not documents.is_document(question):
            return flask.jsonify(
                {"error": "Question is not a document question"}), 400

        nonce, expires_at = documents.mint_export_nonce(assignment_id, question_id)
        return flask.jsonify({"nonce": nonce, "expires_at": expires_at})

    @app.get("/publish")
    @login_manager.login_required
    def publish_apps(**_):
        """campus-apphost publisher UI (#81): upload and list apps.

        Exists only on the apphost deployment — uploading lives on the
        hosting origin by design. Any signed-in Campus user may publish
        in dev (immediate, attribution via uploaded_by_user_id, captured
        by campus audit's session journey); approval gating arrives
        before prod.
        """
        if not documents.embed_mode():
            flask.abort(404)
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            apps = client.api.hostedapps.list()
        # Redirect-after-deploy (#81): publishApp() lands back here with
        # ?deployed=<slug>, so the table renders fresh WITH the banner.
        return flask.render_template(
            "publish.html",
            apps=apps,
            deployed=flask.request.args.get("deployed"),
        )

    @app.get("/api/v1/apps")
    @login_manager.login_required
    def list_apps(**_):
        """List deployed apps for the document-question picker (#81).

        Serves both deployments: the main app's question builder calls
        this for the picker; the apphost publish page server-renders
        its own list.
        """
        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            apps = client.api.hostedapps.list()
        return flask.jsonify([
            {
                "slug": app.slug,
                "name": app.name,
                "size": app.size,
                "updated_at": app.updated_at,
            }
            for app in apps
        ])

    @app.post("/api/v1/apps")
    @login_manager.login_required
    def api_publish_app(**_):
        """Publish an app by upserting into the hostedapps registry
        (campus.api.hostedapps, campus#1066).

        Request body: {slug: str, name?: str, html: str} — the slug is
        the app's address (sanitised); the optional name is the display
        name shown in pickers; html is the complete single-file
        document. Upsert semantics: an existing slug is replaced in
        place (content re-pointed at the new hash), which live-updates
        every referencing assignment. Only on the apphost deployment
        (EMBED_MODE=1).
        """
        if not documents.embed_mode():
            flask.abort(404)
        data = flask.request.get_json() or {}
        slug = documents.sanitize_app_slug(data.get("slug") or "")
        name = (data.get("name") or "").strip() or None
        html = data.get("html") or ""
        if not slug:
            return flask.jsonify({
                "error": "Provide a slug (lowercase letters, numbers, "
                         "dots, dashes)."
            }), 400
        if not html.strip() or "<" not in html:
            return flask.jsonify({
                "error": "The app must be a non-empty HTML document."
            }), 400
        if len(html) > documents.MAX_DOCUMENT_CHARS:
            return flask.jsonify(
                {"error": "The app exceeds the 2 MB limit."}), 400

        campus = flask.current_app.campus
        with campus.with_user_session() as client:
            replaced = True
            try:
                client.api.hostedapps.get_meta(slug)
            except Exception as e:
                if "not found" not in str(e).lower():
                    raise
                replaced = False
            meta = client.api.hostedapps.put_by_slug(
                slug, html, content_type="text/html", name=name
            )
        return flask.jsonify({
            "slug": meta.slug,
            "name": meta.name or name or slug,
            "replaced": replaced,
            "updated_at": meta.updated_at,
        }), (200 if replaced else 201)

    @app.get("/embed/<assignment_id>/<question_id>")
    def embed_document(assignment_id: str, question_id: str):
        """Serve a document question's HTML from the embed origin (#78).

        404s unless EMBED_MODE=1 — the main origin must never serve
        executable teacher HTML. Reads the assignment through the
        app-scoped session (embed visitors have no Campus session),
        injects the export runtime, and sends a CSP that only the main
        origin may frame.
        """
        if not documents.embed_mode():
            flask.abort(404)
        token = flask.request.args.get("t", "")
        if not documents.verify_embed_token(token, assignment_id, question_id):
            flask.abort(403)

        campus = flask.current_app.campus
        try:
            with campus.with_app_session() as client:
                assignment = client.api.assignments[assignment_id].get()
        except Exception as e:
            if "not found" in str(e).lower():
                flask.abort(404)
            raise

        question = _find_question(assignment, question_id)
        if question is None or not documents.is_document(question):
            flask.abort(404)

        # Two source shapes (#62/#81): legacy inline html, or a
        # registry reference (data.slug) — the file's CURRENT bytes,
        # so publisher updates propagate to sent assignments.
        question_data = getattr(question, "data", None) or {}
        html = question_data.get("html")
        slug = documents.question_app_slug(question)
        if not html and slug:
            try:
                with campus.with_app_session() as client:
                    html = client.api.hostedapps.get_content(slug).decode(
                        "utf-8", errors="replace"
                    )
            except Exception as e:
                if "not found" in str(e).lower():
                    flask.abort(404)
                raise
        if not html:
            flask.abort(404)

        parent = documents.parent_origin()
        html = documents.inject_export_runtime(html, question_id, parent)

        response = flask.make_response(html)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-store"
        frame_ancestors = (
            f"frame-ancestors {parent}" if parent
            else "frame-ancestors 'none'"
        )
        response.headers["Content-Security-Policy"] = (
            f"{frame_ancestors}; base-uri 'none'; form-action 'none'"
        )
        return response


def _find_question(assignment, question_id: str):
    for question in getattr(assignment, "questions", None) or []:
        if documents.question_id(question) == question_id:
            return question
    return None


__all__ = ["register_routes"]
