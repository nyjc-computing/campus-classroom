"""campus.apps.classroom

Campus Classroom Application: assignment platform with Google Classroom integration.
"""

import os

import campus_python
import flask
from campus import flask_campus
from campus.common import env
from dotenv import load_dotenv

from . import campus_session, documents, enrollment, routes
from . import classroom_auth as cauth

# Load environment variables from .env file
load_dotenv()


def create_app():
    """Application factory for Campus Classroom."""
    # Static assets mount under the deployment's namespace (#156 gateway
    # cutover): url_for('static') then emits /classroom/static/... or
    # /apps/static/..., which campus-gateway routes (the host-root
    # /static it would otherwise emit is unrouted behind the gateway).
    static_url_path = "/apps/static" if documents.embed_mode() else "/classroom/static"
    app = flask.Flask(
        __name__,
        static_folder="static",
        static_url_path=static_url_path
    )

    # Trace producer (campus#816 item 2, issue #37): record classroom
    # requests as audit spans, and let SDK calls made while handling a
    # request carry trace context so they land as child spans (the
    # campus_python session instrumentation reads the flask.g state this
    # middleware stashes). Opt-in: needs AUDIT_API_KEY; see campus
    # docs/audit-tracing.md. Read once at app creation — flipping the
    # flag is a redeploy, not a runtime change. Ingestion is async and
    # fail-safe: a missing or broken audit key only loses spans.
    if env.get_flag("AUDIT_TRACING_ENABLED", False):
        from campus.flask_campus.audit import init_tracing as init_audit_tracing

        init_audit_tracing(app)

    # Action journeys (campus#828): one user-initiated episode — the
    # page load that began it, its XHRs and form posts, and the
    # server-to-server calls they spawn — groups as one journey in the
    # audit UI. Full lifecycle here: navigations mint (classroom serves
    # pages); journeys ride the sliding campus_action_journey cookie,
    # and SDK calls forward them. The @flask_campus.journey decorator
    # names action boundaries as routes warrant.

    from campus.flask_campus.audit import init_journeys

    init_journeys(app)

    campus = campus_python.Campus(timeout=60)

    # Configure Flask secret key from environment
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise ValueError("SECRET_KEY environment variable is required")

    # Fleet session cookie scheme (campus docs "Session Cookie Naming"):
    # distinct name per deployment so the two apps this repo builds
    # don't clobber each other's — or the other campus apps' — sessions
    # on the shared gateway hostname (#156).
    if documents.embed_mode():
        app.config["SESSION_COOKIE_NAME"] = "campus_apphost_session"
    else:
        app.config["SESSION_COOKIE_NAME"] = "campus_classroom_session"

    # In-iframe session policy (issue #12): Classroom embeds /addon/* as
    # third-party iframes, where SameSite=Lax cookies are never sent —
    # every /addon/* route and its same-origin /api/v1/* fetch would be
    # unauthenticated. The deployment therefore serves the iframe-capable
    # policy: SameSite=None + Secure + Partitioned (CHIPS), which carries
    # the session the in-iframe sign-in (via the framable campusauth
    # pages) sets inside the iframe's (classroom, app) partition. Local
    # debug keeps Flask's Lax default: Secure/Partitioned add nothing on
    # the plain-HTTP dev server, and real-iframe testing runs against the
    # deployment anyway. Requires Flask >= 3.1 for PARTITIONED.
    if os.environ.get("FLASK_DEBUG") != "1":
        app.config.update(
            SESSION_COOKIE_SAMESITE="None",
            SESSION_COOKIE_SECURE=True,
            SESSION_COOKIE_PARTITIONED=True,
        )

    # CSRF guard for the SameSite=None session (issue #12): a cookie that
    # crosses sites makes cross-site form posts carry it, so /api/v1/*
    # mutations must present the custom header the app's own fetch()es
    # set — forged cross-site requests cannot add headers. GETs stay
    # open: they mutate nothing and their JSON is unreadable cross-site.
    @app.before_request
    def _require_ajax_header_for_api_mutations():
        if (flask.request.method in {"POST", "PATCH", "PUT", "DELETE"}
                and flask.request.path.startswith("/api/v1/")
                and flask.request.headers.get("X-Requested-With") != "XMLHttpRequest"):
            return (
                flask.jsonify(
                    {
                        "error": {
                            "code": "csrf_header_missing",
                            "message": "Mutations must be sent with the "
                            "X-Requested-With header (issue #12).",
                        }
                    }
                ),
                403,
            )
        return None

    # Add jinja timestamp filter
    def timestamp(dt, format="%Y-%m-%d %H:%M"):
        if dt is None:
            return "N/A"
        if isinstance(dt, str):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except ValueError:
                return dt
        if hasattr(dt, "strftime"):
            return dt.strftime(format)
        return str(dt)

    app.jinja_env.filters["timestamp"] = timestamp

    # Staff-authored markdown (prompts, passages, question text,
    # descriptions) renders on the display surfaces; escape-first, see
    # text_render.
    from .text_render import render_markdown

    app.jinja_env.filters["md"] = render_markdown

    # Embed deployments render the same templates but only serve the
    # /apps/ surface, so base.html swaps the Classroom navbar (whose
    # routes are gated 404 there) for the apphost one.
    app.context_processor(
        lambda: {"embed_mode": documents.embed_mode()}
    )

    # Setup OAuth login manager. login_prefix mounts the auth blueprint
    # under the deployment's namespace (flask-campus#47): /classroom/login
    # vs /apps/login are dispatchable behind campus-gateway, where the
    # root-level /login is shared by four consumer apps and unroutable
    # (gateway umbrella campus#1295; opt-in tracked as classroom#156).
    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus,
        default_endpoint="index",
        login_prefix="/apps" if documents.embed_mode() else "/classroom",
    )
    login_manager.init_app(app)

    # Make campus available globally
    app.campus = campus
    app.login_manager = login_manager

    # Register API routes
    routes.assignments.register_routes(app, login_manager)
    routes.submissions.register_routes(app, login_manager)
    routes.iframe.register_routes(app, login_manager)
    routes.classroom_auth.register_routes(app, login_manager)
    routes.classroom_sync.register_routes(app, login_manager)
    routes.documents.register_routes(app, login_manager)

    # Last-resort translation of Campus API failures into JSON/flash
    # responses (#139): a denied gated write (e.g. a student without the
    # submissions.admin circle grant) must surface as a friendly 403,
    # never a raw 500 the answer sheet can only render as its bare
    # fallback banner.
    campus_session.register_error_handlers(app)

    # campus-apphost containment (#81): a deployment running EMBED_MODE=1
    # is the dedicated app-hosting origin (campus-apphost) — it serves
    # the /apps/ surface (landing page, /apps/publish, /apps/embed) plus
    # the apps JSON API, and nothing else: no /a/, no submissions, no
    # general /api/v1/*. A signed-in publisher does get a session there
    # (they authenticated deliberately); embed-origin scripts still have
    # no campus session to reach because /apps/embed never sets one.
    # / and /apps redirect into the landing page. Checked per request so
    # the verify harness can flip the flag; in real deployments it's set
    # once and never changes.
    @app.before_request
    def _apphost_embed_only():
        if not documents.embed_mode():
            return None
        path = flask.request.path
        if path in {"/", "/apps"}:
            return flask.redirect(flask.url_for("apps_landing"))
        allowed = (
            path.startswith(("/apps/", "/api/v1/apps", "/static/"))
            or path in {"/apps/login", "/apps/finalize_login", "/apps/logout"}
        )
        if not allowed:
            flask.abort(404)

    # Register UI routes
    @app.get("/")
    def index():
        return flask.render_template("index.html")

    @app.get("/sign-in")
    def sign_in():
        return flask.render_template("sign_in.html")

    @app.get("/dashboard")
    @login_manager.login_required
    def dashboard(**_):
        return flask.render_template("dashboard.html")

    # Assignment UI routes
    @app.get("/assignments")
    @login_manager.login_required
    def assignments_list(**_):
        """List assignments page."""
        return flask.render_template("assignments/list.html")

    @app.get("/assignments/new")
    @login_manager.login_required
    def assignment_new(**_):
        """Create new assignment page (teachers only — classroom#147).

        The 2026-11-24 journey investigation saw six students receive
        this page with HTTP 200. Gated on taught courses — the same
        signal the staff surfaces use, cached 60s per user: no
        Classroom connection or a pre-#1095-scope connection gets the
        standard reconnect page, a connected non-teacher a plain 403.
        """
        try:
            teaches = bool(enrollment.taught_courses())
        except cauth.NotConnectedError:
            return flask.render_template(
                "assignments/share_connect.html",
                profile_integrations_url=(
                    f"{cauth.profile_integrations_url()}/profile/integrations"
                ),
            ), 403
        except cauth.MissingClassroomScopesError:
            return flask.render_template(
                "assignments/share_connect.html",
                profile_integrations_url=(
                    f"{cauth.profile_integrations_url()}/profile/integrations"
                ),
                reconnect_reason="scope",
            ), 403
        if not teaches:
            flask.abort(403)
        return flask.render_template(
            "assignments/new.html",
            # The apphost (embed) deployment hosts the upload flow and
            # the publisher's app list; the builder links both when the
            # origin is configured (null degrades to embed-only).
            embed_origin=documents.embed_origin(),
        )

    @app.get("/assignments/<assignment_id>")
    @login_manager.login_required
    def assignment_view(assignment_id: str, **_):
        """View/edit assignment page."""
        # Document questions render live previews of their embedded app
        # (#78): mint per-question embed URLs exactly like the student
        # answer sheet does. The read uses the viewer's own Campus
        # session — the same ACL the page's client fetch applies
        # (assignment_submissions gates the same way). A dead Campus
        # grant propagates to the app-wide re-auth redirect (#98).
        embed_urls = {}
        embed_origin = documents.embed_origin()
        if embed_origin:
            with campus_session.user_session() as client:
                try:
                    assignment = client.api.assignments[assignment_id].get()
                except Exception as e:
                    if "not found" in str(e).lower():
                        flask.abort(404)
                    raise
            for question in getattr(assignment, "questions", None) or []:
                qid = documents.question_id(question)
                if qid and documents.is_document(question):
                    embed_urls[qid] = documents.embed_url_for(
                        assignment_id, qid
                    )

        return flask.render_template(
            "assignments/view.html",
            assignment_id=assignment_id,
            embed_urls=embed_urls,
            embed_origin=embed_origin,
            profile_integrations_url=(
                f"{cauth.profile_integrations_url()}/profile/integrations"
            ),
        )

    # Submissions drill-down (#105): one assignment's submissions, linked
    # from the dashboard. Server-side gate mirrors the by-assignment
    # API's ACL — reviewers (the creator or a co-tutor, #130) see the
    # page, everyone else (including enrolled students) gets 404 so the
    # assignment's review surface is not revealed; the rows themselves
    # are then client-fetched from the same reviewer-only API, so the
    # gate is enforced twice.
    @app.get("/assignments/<assignment_id>/submissions")
    @login_manager.login_required
    def assignment_submissions(assignment_id: str, **_):
        """List one assignment's submissions (staff review, #105)."""
        user_id = flask.g.user.id
        with campus_session.user_session() as client:
            try:
                assignment = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    flask.abort(404)
                raise
            if not enrollment.can_review(assignment, user_id):
                flask.abort(404)

        # CampusSessionExpiredError propagates: the app-wide handler
        # redirects browser paths into the campus OAuth flow (#98).
        return flask.render_template(
            "assignments/submissions.html",
            assignment_id=assignment_id,
            assignment_title=getattr(assignment, "title", assignment_id),
        )

    # Submissions inbox (navbar resource page): every submission to a
    # reviewable assignment — the caller's own, or any linked to a course
    # they teach (#130, mirroring Classroom's submission contract) —
    # newest first. The route resolves the taught-course set for the
    # client-side filter; like the rule-4 listing, a Classroom-bridge
    # failure degrades to own assignments only (the API gates stay
    # exact, so the degradation only ever hides). The rows are
    # client-fetched from the reviewer-only by-assignment API, so a
    # student just sees the empty state — nothing can leak that the API
    # would not already return them.
    @app.get("/submissions")
    @login_manager.login_required
    def submissions_list(**_):
        """List submissions across the caller's reviewable assignments (staff)."""
        user_id = flask.g.user.id
        try:
            taught = enrollment.taught_courses()
        except cauth.ClassroomAuthError as err:
            flask.current_app.logger.warning(
                "Submissions inbox degraded to own assignments for %s: %s (%s)",
                user_id, err.code, err.message,
            )
            taught = {}
        return flask.render_template(
            "submissions/list.html",
            taught_course_ids=sorted(taught),
        )

    # Submission UI routes
    @app.get("/submissions/<submission_id>")
    @login_manager.login_required
    def submission_view(submission_id: str, **_):
        """Review page for one submission (PRD §6.9 ACL, #104; #130).

        Allowed: the submission's own student — who opens this page from
        the review-link chip the mirror attaches in Classroom — the
        assignment's owner (staff review, the pre-#104 behavior), and
        any teacher of a linked course (co-tutor, #130: mirroring
        Classroom's own submission contract). Everyone else gets 404 so
        the submission's existence is not revealed, matching the
        submission API's ACL. Teacher-only fields (Classroom-native
        draftGrade) render nowhere on this page — grades stay
        Classroom-native (parked phase-2), so there is nothing to leak.

        The page is read-only for EVERY viewer: the response/submit
        verbs behind its forms are student-only and 403 a reviewer
        mid-click (the owner's editable affordances here were already
        dead that way) — owners edit on /a/<id>, co-tutors review in the
        drill-down. The assignment payload (title + questions) renders
        server-side instead of being client-fetched:
        /api/v1/assignments/<id> stays creator-only (a co-tutor must not
        land in the builder), and this route is already reviewer-gated —
        so it passes the questions down itself. One fewer client round
        trip, too.
        """
        user_id = flask.g.user.id
        with campus_session.user_session() as client:
            try:
                submission = client.api.submissions[submission_id].get()
            except Exception as e:
                # "not found" covers the API's 404; 409/conflict covers a
                # submission record deleted after its review chip was
                # already attached in Classroom (#104 chips are add-only).
                gone = ("not found" in str(e).lower()
                        or getattr(e, "status_code", None) in (404, 409))
                if gone:
                    flask.abort(404)
                raise

            try:
                assignment = client.api.assignments[
                    submission.assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    flask.abort(404)
                raise

            # Gate: the owning student, the creator, or a co-tutor.
            if (str(getattr(submission, "student_id", "")) != str(user_id)
                    and not enrollment.can_review(assignment, user_id)):
                flask.abort(404)

        # CampusSessionExpiredError propagates: the app-wide handler
        # redirects browser paths into the campus OAuth flow (#98).
        return flask.render_template(
            "submissions/view.html",
            submission_id=submission_id,
            read_only=True,
            assignment_title=getattr(
                assignment, "title", submission.assignment_id),
            assignment_questions=(
                assignment.to_resource().get("questions") or []),
        )

    # Shareable assignment page (PRD §6.9): the Link Material fallback posts
    # this URL into Classroom. Assignment content is never public (PRD v1.3
    # rule 2, #28): anonymous visitors are sent to sign-in; a signed-in
    # visitor sees content if they own the assignment or are in a Google
    # Classroom course the assignment is linked to (enrollment via the auth
    # bridge); anyone else gets a 403 gate page with no assignment content.
    # A signed-in visitor without a google.classroom connection gets a
    # connect prompt (deny + CTA) instead of the bare gate, and a student
    # whose connection predates the #104 mirror scope gets the reconnect
    # variant of the same prompt (the submit-time turn-in needs it).
    #
    # #14: for an enrolled student the page is also the answer sheet —
    # questions render with an answer form backed by the submissions API
    # (create/resume/save/submit, teacher feedback once released). The
    # submission is keyed to one of the shared courses: a per-course Link
    # Material URL pins ?course= (classroom_sync), otherwise a single
    # shared course is used directly and several shared courses get a
    # class picker. The owner gets a render-only preview with a manage
    # link instead of a form.
    @app.get("/a/<assignment_id>")
    def assignment_share(assignment_id: str):
        """Gated assignment page for the Link Material fallback (#28/#14).

        Primary read path is the app-scoped Campus session, falling back to
        the visitor's own Campus session if app scope is unavailable. The
        visitor must be signed in and either own the assignment or be
        enrolled in one of its linked Classroom courses.
        """
        if getattr(flask.g, "user", None) is None:
            return flask.redirect(flask.url_for("sign_in"))

        assignment = None
        try:
            with campus_session.app_session() as client:
                assignment = client.api.assignments[assignment_id].get()
        except Exception as e:
            if "not found" in str(e).lower():
                flask.abort(404)
            # App scope unavailable on this deployment; try user scope.

        if assignment is None:
            try:
                with campus_session.user_session() as client:
                    assignment = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    flask.abort(404)
                raise

        is_owner = (
            str(getattr(assignment, "created_by", "")) == str(flask.g.user.id)
        )
        courses: list[dict] = []
        if not is_owner:
            # Non-owner: rule 2's assigned-student render. The enrollment
            # check is fail-closed — bridge errors propagate to the
            # app-wide handler (browser paths land on the connection
            # page), except the reconnectable pair, which gets the
            # inline connect prompt on this page.
            try:
                if enrollment.can_review(assignment, flask.g.user.id):
                    # Staff previewing a course they teach: the mirror
                    # scope drives student-only verbs, so the staff
                    # demand stays MVP-only.
                    courses = enrollment.enrolled_courses(assignment)
                else:
                    # The visitor is the mirroring student (#104): the
                    # submit-time turn-in runs on THEIR credential, so
                    # demand the coursework.me grant here — a credential
                    # minted before the campus#1095 cap widening routes
                    # through the reconnect CTA instead of reaching a
                    # submit whose Classroom mirror would silently skip.
                    courses = enrollment.enrolled_courses(
                        assignment,
                        extra_required_scopes=cauth.CLASSROOM_SCOPES_MIRROR,
                    )
            except cauth.NotConnectedError:
                return flask.render_template(
                    "assignments/share_connect.html",
                    profile_integrations_url=(
                        f"{cauth.profile_integrations_url()}/profile/integrations"
                    ),
                ), 403
            except cauth.MissingClassroomScopesError:
                # The connection predates the scope: the same deny+CTA
                # page, reconnect-flavored (the connect flow asks for
                # exactly the vault cap, which now carries the scope).
                return flask.render_template(
                    "assignments/share_connect.html",
                    profile_integrations_url=(
                        f"{cauth.profile_integrations_url()}/profile/integrations"
                    ),
                    reconnect_reason="scope",
                ), 403
            if not courses:
                return flask.render_template("assignments/share_gated.html"), 403

        # A per-course Link Material pins its course; an unpinned open
        # uses the single shared course directly, and several shared
        # courses leave the choice to the class picker in the form.
        requested = flask.request.args.get("course")
        pinned_course = (
            requested if requested in {c["course_id"] for c in courses} else None
        )

        # Document questions (#62/#78) render as sandboxed iframes on the
        # dedicated embed origin — the doc HTML is never inlined into the
        # page. Without EMBED_ORIGIN the answer sheet degrades to a
        # notice (the frame is the only way to work the document).
        questions = list(getattr(assignment, "questions", None) or [])
        question_renderers = {
            documents.question_id(q): documents.renderer_for(q)
            for q in questions if documents.question_id(q)
        }
        # Group nodes: ids that parent dotted children (the builder emits
        # them question-less and type-less) render as passage sections.
        # Detected by children — not by empty text — so an unsupported
        # type still shows its notice rather than a bare passage (#62).
        qids = {documents.question_id(q) for q in questions}
        group_ids = {
            qid.rsplit(".", 1)[0] for qid in qids if qid and "." in qid
        }
        embed_urls = {}
        if documents.embed_origin():
            for question in questions:
                qid = documents.question_id(question)
                if qid and documents.is_document(question):
                    embed_urls[qid] = documents.embed_url_for(
                        assignment.id, qid
                    )

        return flask.render_template(
            "assignments/share.html",
            assignment=assignment,
            is_owner=is_owner,
            courses=courses,
            pinned_course=pinned_course,
            question_renderers=question_renderers,
            group_ids=group_ids,
            embed_urls=embed_urls,
            embed_origin=documents.embed_origin(),
            profile_integrations_url=(
                f"{cauth.profile_integrations_url()}/profile/integrations"
            ),
        )

    # Test routes (for local development)
    @app.get("/test/iframe")
    def test_iframe():
        """Test page for iframe views."""
        return flask.render_template("test-iframe.html")

    return app
