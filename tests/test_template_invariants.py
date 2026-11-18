"""Template invariants that guard the #123 failure class.

#123: a group card's hidden empty `required` textarea silently blocked
Create Assignment — native HTML5 validation failed on a non-focusable
control, so the submit event never fired and the page's own JS
validation never ran. #125 fixed it with `novalidate` on the form;
this test keeps that fix (and the general rule) enforced:

  Rule 1 — every JS-handled form must carry `novalidate`
           (JS validation owns submission; native validation on hidden
           controls fails silently).
  Rule 2 — any template that loads question-builder.js must render
           every <form> as `novalidate`, because the builder emits
           `required` controls (question-builder.js card template)
           that can be hidden while grouped.

Run:  python tests/test_template_invariants.py   (exit 1 on violation)
"""

import re
import sys
from pathlib import Path

import flask

REPO_ROOT = Path(__file__).resolve().parents[1]
CLASSROOM_DIR = REPO_ROOT / "apps" / "classroom"

# Forms whose submit is handled by page JS (not native action), per template.
# Adding a JS-handled form here without novalidate fails Rule 1.
JS_HANDLED_FORMS = {
    "assignments/new.html": ["assignment-form"],
}

# Template context vars, mirroring the real routes (verify_issue_4.py pattern).
TEMPLATE_CONTEXT = {
    "assignments/new.html": {"embed_origin": None},
}

checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    marker = "PASS  " if ok else "FAIL  "
    suffix = f"\n      {detail}" if (detail and not ok) else ""
    print(marker + name + suffix)


def make_app():
    app = flask.Flask(
        "template_invariants",
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


def render(app, template):
    with app.test_request_context():
        flask.g.user = None
        return flask.render_template(template, **TEMPLATE_CONTEXT.get(template, {}))


def form_tags(html):
    return re.findall(r"<form\b[^>]*>", html)


def form_ids(tag):
    return re.findall(r'id="([^"]+)"', tag)


def main():
    app = make_app()
    rendered = {
        template: render(app, template) for template in JS_HANDLED_FORMS
    }

    # Rule 1: mapped JS-handled forms must be novalidate.
    for template, form_id_list in JS_HANDLED_FORMS.items():
        html = rendered[template]
        tags = form_tags(html)
        for form_id in form_id_list:
            tag = next((t for t in tags if f'id="{form_id}"' in t), None)
            if tag is None:
                check(f"{template}: form #{form_id} renders", False,
                      "form tag not found in rendered template")
                continue
            check(
                f"{template}: form #{form_id} is novalidate",
                "novalidate" in tag,
                "novalidate missing — native validation would silently "
                "block submit on hidden required controls (see #123/#125)",
            )

    # Rule 2: any template loading question-builder.js renders every
    # <form> as novalidate — the builder emits `required` controls
    # (hidden while grouped) that land inside these forms.
    builder_js = (CLASSROOM_DIR / "static" / "js" / "question-builder.js")
    builder_emits_required = "required" in builder_js.read_text(encoding="utf-8")
    for template, html in rendered.items():
        loads_builder = "question-builder.js" in html
        if not (builder_emits_required and loads_builder):
            continue
        tags = form_tags(html)
        bad = [t for t in tags if "novalidate" not in t]
        check(
            f"{template}: all forms novalidate (loads question-builder.js "
            f"which emits required fields: {builder_emits_required})",
            not bad,
            "forms without novalidate:\n      " + "\n      ".join(bad),
        )

    failed = sum(1 for ok in checks if not ok)
    if failed:
        print(f"\n{failed} check(s) FAILED — see #123 for the failure mode "
              f"this guards")
        return 1
    print(f"\nAll {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
