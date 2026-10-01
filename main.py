"""Main entry point for Campus Classroom Flask application."""

from apps.classroom import create_app

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
