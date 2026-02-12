"""campus.apps.classroom

Campus Classroom Application: assignment platform with Google Classroom integration.
"""

import os

import campus_python
import flask
from campus import flask_campus


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
    def timestamp(dt, format="%Y-%m-%d %H:%M"):
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

    # Register routes
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

    return app
