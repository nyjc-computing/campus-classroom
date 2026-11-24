"""campus.apps.classroom.routes.documents

Document-question routes (#62/#78): export challenges for the answer
sheet, the apphost publisher surface, and the embed origin's document
serving.

/export-challenge lives on the main app: the student's /a/ page calls it
immediately before asking the embedded document to serialize itself, so
every accepted snapshot carries a fresh server-minted nonce.

The campus-apphost browser surface lives under /apps/: /apps/ is the
public landing page, /apps/add the upload form, /apps/myapps the
signed-in publisher's own apps, /apps/new a not-yet-implemented
in-browser authoring stub, and /apps/embed/<a>/<q> the document-
serving route — all gated to EMBED_MODE=1 so the main origin never
serves executable teacher HTML (#78). Embed requests carry no Campus
session cookie (different origin), so auth is the short-lived HMAC
token the share page minted into the iframe URL.
"""

import math

import flask
from campus import flask_campus

from .. import campus_session, documents, enrollment


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
        data = flask.request.get_json() or {}
        question_id = data.get("question_id")
        if not question_id:
            return flask.jsonify({"error": "question_id is required"}), 400

        with campus_session.user_session() as client:
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

    @app.get("/apps/")
    def apps_landing():
        """campus-apphost landing page (#81): what this origin hosts.

        Public orientation for publishers: the blurb explains that
        single-file HTML apps uploaded here become embeddable document
        questions in Campus Classroom; flask_campus login state picks
        the CTA (signed in → add/myapps, anonymous → sign-in with a
        next= onto the add form). 404 unless EMBED_MODE=1 — on the main
        deployment / is the Classroom app.
        """
        if not documents.embed_mode():
            flask.abort(404)
        return flask.render_template("apps_landing.html")

    @app.get("/apps/myapps")
    @login_manager.login_required
    def apps_myapps(**_):
        """The signed-in publisher's own apps (#81).

        Filtered app-side on uploaded_by_user_id (the registry has no
        server-side filter; the listing is small in dev). ?deployed=
        carries the redirect-after-add banner.
        """
        if not documents.embed_mode():
            flask.abort(404)
        user_id = flask.g.user.id
        with campus_session.user_session() as client:
            apps = [
                app for app in client.api.hostedapps.list()
                if str(getattr(app, "uploaded_by_user_id", "")) == str(user_id)
            ]
        return flask.render_template(
            "apps_myapps.html",
            apps=apps,
            deployed=flask.request.args.get("deployed"),
            updated=flask.request.args.get("updated"),
        )

    @app.get("/apps/add")
    @login_manager.login_required
    def apps_add(**_):
        """Upload page for a new app (#81): slug + single-file HTML.

        Exists only on the apphost deployment — uploading lives on the
        hosting origin by design. Any signed-in Campus user may publish
        in dev (immediate, attribution via uploaded_by_user_id, captured
        by campus audit's session journey); approval gating arrives
        before prod. The form POSTs the apps JSON API; on success it
        redirects to /apps/myapps?deployed=<slug>.
        """
        if not documents.embed_mode():
            flask.abort(404)
        return flask.render_template("apps_add.html")

    @app.get("/apps/edit/<slug>")
    @login_manager.login_required
    def apps_edit(slug: str, **_):
        """Edit page for one of the signed-in publisher's own apps (#142).

        Owns-only: a signed-in user who does not own the slug gets the
        same 404 as an unknown slug, so the surface does not reveal
        which slugs other publishers hold. The form PATCHes
        /api/v1/apps/<slug> — a display-name edit, a replacement HTML
        upload, or a slug move (re-publish under a new address, the
        registry's sanctioned rename, since slugs are immutable
        upstream by design).
        """
        if not documents.embed_mode():
            flask.abort(404)
        user_id = flask.g.user.id
        with campus_session.user_session() as client:
            try:
                app_record = client.api.hostedapps.get_meta(slug)
            except Exception as e:
                if "not found" in str(e).lower():
                    flask.abort(404)
                raise
            if (str(getattr(app_record, "uploaded_by_user_id", ""))
                    != str(user_id)):
                flask.abort(404)
        # Route-computed: a child-template set is invisible inside
        # blocks, and both the page copy and the drop-zone JS need it.
        size_kb = max(1, math.ceil((app_record.size or 0) / 1024))
        return flask.render_template(
            "apps_edit.html",
            app_record=app_record,
            size_kb=size_kb,
        )

    @app.get("/apps/new")
    @login_manager.login_required
    def apps_new(**_):
        """Placeholder for in-browser app creation (#81).

        The route exists so the IA is stable (nav/landing can link it);
        authoring in the browser is not implemented yet — the page says
        so and points at /apps/add for uploading existing HTML.
        """
        if not documents.embed_mode():
            flask.abort(404)
        return flask.render_template("apps_new.html")

    @app.get("/api/v1/apps")
    @login_manager.login_required
    def list_apps(**_):
        """List deployed apps for the document-question picker (#81).

        Serves both deployments: the main app's question builder calls
        this for the picker; the apphost publish page server-renders
        its own list.
        """
        with campus_session.user_session() as client:
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
        """Deploy an app by upserting into the hostedapps registry
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

        with campus_session.user_session() as client:
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

    @app.patch("/api/v1/apps/<slug>")
    @login_manager.login_required
    # The action culminates in campus.api's hostedapps yapper emissions
    # (campus.hostedapps.update for an in-place edit; a slug move is a
    # new + delete pair) — the journey names the whole episode.
    @flask_campus.journey("campus.hostedapps.update")
    def api_update_app(slug: str, **_):
        """Update one of the caller's own apps (#142).

        Request body: {name?: str, slug?: str, html?: str,
        remove_old_slug?: bool} — every field optional:

        - name: new display name (blank/absent keeps the stored one;
          the registry has no way to clear a name back to None).
        - html: replacement single-file document; absent keeps the
          current content (a name-only or move-only update re-PUTs the
          stored bytes — the registry only writes through the by-slug
          upsert).
        - slug: move the app to a new address. Slugs are immutable
          upstream, so a move is re-publish under the new slug (same
          content/name) and optionally remove the old record —
          assignments referencing the OLD slug break either way, which
          the edit page warns about before saving. Moves target an
          UNUSED slug (409 otherwise): a move must never silently
          replace someone else's app.
        - remove_old_slug: with slug, also delete the old record after
          the new one is in place (default false — the copy stays).

        Owner-gated: only the publishing user may edit (403), and a
        foreign slug 404s like the edit page. Only on the apphost
        deployment (EMBED_MODE=1).
        """
        if not documents.embed_mode():
            flask.abort(404)
        data = flask.request.get_json(silent=True) or {}

        name = str(data.get("name") or "").strip() or None
        new_slug_raw = str(data.get("slug") or "").strip()
        html = data.get("html")
        remove_old = bool(data.get("remove_old_slug"))

        with campus_session.user_session() as client:
            try:
                record = client.api.hostedapps.get_meta(slug)
            except Exception as e:
                if "not found" in str(e).lower():
                    return flask.jsonify({"error": "App not found."}), 404
                raise
            if (str(getattr(record, "uploaded_by_user_id", ""))
                    != str(flask.g.user.id)):
                return flask.jsonify({
                    "error": "Only the app's publisher can edit it.",
                }), 403

            current_slug = record.slug
            target_slug = (
                documents.sanitize_app_slug(new_slug_raw)
                if new_slug_raw else current_slug
            )
            if new_slug_raw and not target_slug:
                return flask.jsonify({
                    "error": "Provide a slug (lowercase letters, numbers, "
                             "dots, dashes)."
                }), 400
            moved = target_slug != current_slug
            content_replaced = html is not None
            name_changed = name is not None and name != record.name

            if not moved and not content_replaced and not name_changed:
                return flask.jsonify({"error": "Nothing to update."}), 400

            if moved:
                try:
                    client.api.hostedapps.get_meta(target_slug)
                except Exception as e:
                    if "not found" not in str(e).lower():
                        raise
                else:
                    return flask.jsonify({
                        "error": f"An app already exists at {target_slug} — "
                                 "a move needs an unused slug.",
                    }), 409

            if content_replaced:
                html = str(html)
                if not html.strip() or "<" not in html:
                    return flask.jsonify({
                        "error": "The app must be a non-empty HTML document."
                    }), 400
                if len(html) > documents.MAX_DOCUMENT_CHARS:
                    return flask.jsonify(
                        {"error": "The app exceeds the 2 MB limit."}), 400
                content: bytes | str = html
                content_type = "text/html"
            else:
                content = client.api.hostedapps.get_content(current_slug)
                content_type = record.content_type or "text/html"

            # The display name rides every write: in-place replaces keep
            # the stored name otherwise, and a move creates a fresh
            # record that would drop it.
            put_name = name if name is not None else (record.name or None)
            meta = client.api.hostedapps.put_by_slug(
                target_slug, content, content_type=content_type,
                name=put_name,
            )

            old_slug_removed = False
            warning = None
            if moved and remove_old:
                try:
                    client.api.hostedapps.delete_by_slug(current_slug)
                    old_slug_removed = True
                except Exception as e:
                    # The move itself succeeded; report the leftover
                    # rather than failing the whole update.
                    warning = (
                        f"The app now lives at {target_slug}, but removing "
                        f"the old {current_slug} failed ({e})."
                    )

        return flask.jsonify({
            "slug": meta.slug,
            "name": meta.name or put_name or meta.slug,
            "moved": moved,
            "old_slug": current_slug if moved else None,
            "old_slug_removed": old_slug_removed,
            "content_replaced": content_replaced,
            "updated_at": meta.updated_at,
            **({"warning": warning} if warning else {}),
        }), 200

    @app.get("/apps/embed/<assignment_id>/<question_id>")
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
