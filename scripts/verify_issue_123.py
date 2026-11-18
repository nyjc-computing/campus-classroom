"""Verify the issue #123 fix: group cards must not block Create Assignment.

Root cause: every builder card ships a `required` question textarea; when a
card becomes a group parent its leaf fields hide (CSS) and its text clears
(demoteCardContent), leaving a hidden empty required control. With native
HTML5 validation active (no `novalidate` on the form), clicking the submit
button failed constraint validation on that non-focusable control and the
submit event never fired — no handler, no POST, no message.

Fix: `novalidate` on #assignment-form; JS validation
(collectValidatedQuestions) owns the form, per-question alerts included.

Usage:
  python scripts/verify_issue_123.py            # static checks (CI-safe)
  python scripts/verify_issue_123.py --serve [--port 8792]
      Serve the real rendered builder + a stub API for an interactive
      browser run. Stub endpoints: GET /api/v1/apps (one fake app),
      POST /api/v1/assignments (captures payload, returns uid-assignment-
      verify123), GET /verify-state (captured POSTs as JSON).
"""

import argparse
import json
import sys
from pathlib import Path

import flask

REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSROOM_DIR = REPO_ROOT / "apps" / "classroom"

checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    marker = "PASS  " if ok else "FAIL  "
    suffix = f"  [{detail}]" if (detail and not ok) else ""
    print(marker + name + suffix)


def make_app():
    app = flask.Flask(
        "verify_issue_123",
        template_folder=str(CLASSROOM_DIR / "templates"),
        static_folder=str(CLASSROOM_DIR / "static"),
    )

    for endpoint, path in [
        ("assignments_list", "/assignments"),
        ("submissions_list", "/submissions"),
        ("apps_add", "/apps/add"),
        ("apps_myapps", "/apps/myapps"),
    ]:
        app.add_url_rule(path, endpoint, lambda: "")

    return app


def static_checks():
    app = make_app()
    with app.test_request_context():
        flask.g.user = None
        html = flask.render_template("assignments/new.html", embed_origin=None)

    check("assignments/new.html renders", "Create New Assignment" in html)
    check(
        "form carries novalidate (#123)",
        'id="assignment-form" class="needs-validation" novalidate' in html,
    )
    check(
        "JS validation still guards missing question text",
        "missing its question text" in
        (CLASSROOM_DIR / "static" / "js" / "question-builder.js").read_text(
            encoding="utf-8"),
    )


def serve(port):
    app = make_app()
    captured = []

    @app.get("/")
    def _new_assignment():
        flask.g.user = None
        return flask.render_template("assignments/new.html", embed_origin=None)

    @app.get("/api/v1/apps")
    def _apps():
        return [
            {"slug": "html-inspector", "name": "HTML Inspector",
             "updated_at": "2026-11-18T00:00:00Z"},
        ]

    @app.post("/api/v1/assignments")
    def _create():
        if flask.request.headers.get("X-Requested-With") != "XMLHttpRequest":
            flask.abort(400)
        captured.append(flask.request.get_json(force=True))
        return {"id": "uid-assignment-verify123"}

    @app.get("/assignments/<assignment_id>")
    def _view(assignment_id):
        return f"<html><body><h1>STUB VIEW {assignment_id}</h1></body></html>"

    @app.get("/verify-state")
    def _state():
        return {"posts": captured}

    print(f"Serving builder stub on http://127.0.0.1:{port}/ "
          "(POSTs land on /verify-state)")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False,
            threaded=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true",
                        help="serve the rendered builder + stub API")
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()

    if args.serve:
        serve(args.port)
        return 0

    static_checks()
    failed = sum(1 for ok in checks if not ok)
    if failed:
        print(f"\n{failed} check(s) FAILED")
        return 1
    print(f"\nAll {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
