"""Verification for issue #4: assignment question payloads must use
Question model field names (id/prompt/question); mismatches now 422.

Checks:
1. The payload shape produced by question-builder.js (collectQuestions +
   flattenQuestions) validates against campus.model.Question, exactly the
   way campus.api.payloads.models_from_payloads validates on POST/PATCH.
2. Legacy/malformed shapes fail the same validation (i.e. would be 422).
3. upstream_error_response() converts a campus ValidationError into a
   422 JSON response carrying the structured field errors.
4. new.html / view.html still render (template regression check).
"""

import flask

import campus.model
from campus_python.errors import APIError, FieldError, ValidationError

from apps.classroom.routes.assignments import upstream_error_response

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# 1. Builder output shape validates like upstream models_from_payloads
# ---------------------------------------------------------------------------
# Simulates question-builder.js collectQuestions() + flattenQuestions() for a
# representative nested assignment (q1 with subparts, plus a second question).
builder_payload = [
    {"id": "q1", "prompt": "Read the following passage...", "question": "What is the main theme?"},
    {"id": "q1.a", "prompt": "", "question": "Identify two supporting details."},
    {"id": "q1.a.i", "prompt": "", "question": "Explain your first choice."},
    {"id": "q1.a.ii", "prompt": "", "question": "Explain your second choice."},
    {"id": "q2", "prompt": "Consider the following scenario...", "question": "Analyze the cause and effect."},
]

validated = []
for i, payload in enumerate(builder_payload):
    try:
        validated.append(campus.model.Question(**payload))
    except (TypeError, ValueError) as e:  # what upstream turns into 422
        check(f"builder payload questions[{i}] validates", False, str(e))
check("all builder payloads match Question schema", len(validated) == len(builder_payload))

# Level-4 nesting (deepest the model docstring mentions) also allowed
try:
    campus.model.Question(id="q1.a.i.1", prompt="", question="x")
    check("level-4 dotted id q1.a.i.1 validates", True)
except (TypeError, ValueError) as e:
    check("level-4 dotted id q1.a.i.1 validates", False, str(e))

# ---------------------------------------------------------------------------
# 2. Malformed / legacy shapes are rejected (upstream would 422 these)
# ---------------------------------------------------------------------------
legacy_shapes = [
    ("legacy question_id/question_text", {"question_id": "q1", "question_text": "What is 2+2?"}),
    ("missing prompt", {"id": "q1", "question": "What is 2+2?"}),
    ("renamed fields", {"questionId": "q1", "prompt": "", "questionText": "What is 2+2?"}),
    ("invalid id format", {"id": "question 1", "prompt": "", "question": "What is 2+2?"}),
]
for name, payload in legacy_shapes:
    try:
        campus.model.Question(**payload)
        check(f"rejected: {name}", False, "constructor accepted legacy payload")
    except (TypeError, ValueError):
        check(f"rejected: {name}", True)

# ---------------------------------------------------------------------------
# 3. upstream_error_response pass-through
# ---------------------------------------------------------------------------
app = flask.Flask(__name__)
with app.test_request_context():
    err = ValidationError(
        error_description="questions[0] does not match the Question schema",
        errors=[FieldError(
            field="questions[0]",
            code="INVALID_FORMAT",
            message="Invalid question ID format: question 1. "
                    "Must use hierarchical dot notation (e.g., q1, q1.a, q1.a.i)",
        )],
    )
    resp, status = upstream_error_response(err)
    body = resp.get_json()
    check("status passed through as 422", status == 422, str(status))
    check("error.code == VALIDATION_FAILED", body["error"]["code"] == "VALIDATION_FAILED")
    check("field error relayed", body["error"]["errors"][0]["field"] == "questions[0]")
    check("constructor message relayed", "Invalid question ID format" in body["error"]["errors"][0]["message"])

    # Non-validation APIError (e.g. upstream 500/409) keeps its status
    other = APIError(status_code=409, error_description="Conflict")
    _, other_status = upstream_error_response(other)
    check("non-422 APIError keeps upstream status", other_status == 409, str(other_status))

# ---------------------------------------------------------------------------
# 4. Template render regression check
# ---------------------------------------------------------------------------
render_app = flask.Flask(__name__, template_folder="../apps/classroom/templates",
                         static_folder="../apps/classroom/static")

@render_app.get("/assignments")
def assignments_list():
    return ""

with render_app.test_request_context():
    flask.g.user = None
    new_html = flask.render_template("assignments/new.html")
    view_html = flask.render_template("assignments/view.html", assignment_id="assignment_test")
check("assignments/new.html renders", "Create New Assignment" in new_html)
check("assignments/view.html renders", "assignment-container" in view_html)
check(
    "new.html loads api-errors.js before question-builder.js",
    new_html.find("api-errors.js") < new_html.find("question-builder.js") and new_html.find("api-errors.js") != -1,
)
check(
    "view.html loads api-errors.js before question-builder.js",
    view_html.find("api-errors.js") < view_html.find("question-builder.js") and view_html.find("api-errors.js") != -1,
)
check("new.html uses formatApiError", "formatApiError(err" in new_html)
check("view.html uses formatApiError", "formatApiError(err" in view_html)

# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    raise SystemExit(1)
print("All checks passed.")

