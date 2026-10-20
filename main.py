"""Main entry point for Campus Classroom Flask application."""

import os

# Local dev server marker (issue #12): selects the Lax session-cookie
# policy in the app factory; the deployment (no FLASK_DEBUG) gets the
# SameSite=None + Secure + Partitioned policy the Classroom iframes need.
os.environ.setdefault("FLASK_DEBUG", "1")

from apps.classroom import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    # The OAuth callback origin comes from PUBLIC_URL (see .env.example);
    # locally that is http://localhost:5000, so no TLS is needed here.
    app.run(
        debug=True,
        host="0.0.0.0",
        port=5000,
        threaded=True,
        use_reloader=False,
    )
