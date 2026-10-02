"""campus.apps.classroom

Campus Classroom Application: assignment platform with Google Classroom integration.
"""

import os

from dotenv import load_dotenv

import campus_python
import flask
from campus import flask_campus

from . import routes

# Load environment variables from .env file
load_dotenv()


def create_app():
    """Application factory for Campus Classroom."""
    app = flask.Flask(
        __name__,
        static_folder="static",
        static_url_path="/static"
    )
    campus = campus_python.Campus(timeout=60)

    # Configure Flask secret key from environment
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise ValueError("SECRET_KEY environment variable is required")

    # Add jinja timestamp filter
    def timestamp(dt, format="%%Y-%%m-%%d %%H:%%M"):
        if dt is None:
            return "N/A"
        if isinstance(dt, str):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except:
                return dt
        if hasattr(dt, "strftime"):
            return dt.strftime(format)
        return str(dt)

    app.jinja_env.filters["timestamp"] = timestamp

    # Setup OAuth login manager
    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus,
        default_endpoint="index"
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
        """Create new assignment page."""
        return flask.render_template("assignments/new.html")

    @app.get("/assignments/<assignment_id>")
    @login_manager.login_required
    def assignment_view(assignment_id: str, **_):
        """View/edit assignment page."""
        return flask.render_template("assignments/view.html", assignment_id=assignment_id)

    # Submission UI routes
    @app.get("/submissions/<submission_id>")
    @login_manager.login_required
    def submission_view(submission_id: str, **_):
        """View submission page."""
        return flask.render_template("submissions/view.html", submission_id=submission_id)

    # Shareable assignment page (PRD §6.9): the Link Material fallback posts
    # this URL into Classroom. Assignment content is never public (PRD v1.3
    # rule 2): anonymous visitors are sent to sign-in, and signed-in users
    # see content only if they own the assignment. Rendering for assigned
    # students (classroom-membership check) lands with the enrollment work
    # tracked in #28 — until then this page is owner-only.
    @app.get("/a/<assignment_id>")
    def assignment_share(assignment_id: str):
        """Gated render-only assignment page for the Link Material fallback.

        Primary read path is the app-scoped Campus session, falling back to
        the visitor's own Campus session if app scope is unavailable. The
        visitor must be signed in and own the assignment; anyone else gets
        a 403 gate page with no assignment content.
        """
        if getattr(flask.g, "user", None) is None:
            return flask.redirect(flask.url_for("sign_in"))

        campus = flask.current_app.campus
        assignment = None
        try:
            with campus.with_app_session() as client:
                assignment = client.api.assignments[assignment_id].get()
        except Exception as e:
            if "not found" in str(e).lower():
                flask.abort(404)
            # App scope unavailable on this deployment; try user scope.

        if assignment is None:
            try:
                with campus.with_user_session() as client:
                    assignment = client.api.assignments[assignment_id].get()
            except Exception as e:
                if "not found" in str(e).lower():
                    flask.abort(404)
                raise

        if str(getattr(assignment, "created_by", "")) != str(flask.g.user.id):
            return flask.render_template("assignments/share_gated.html"), 403

        return flask.render_template(
            "assignments/share.html",
            assignment=assignment,
        )

    # Test routes (for local development)
    @app.get("/test/iframe")
    def test_iframe():
        """Test page for iframe views."""
        return flask.render_template("test-iframe.html")

    return app
