"""apps.classroom.iframe_context

Real Classroom context for the /addon/* iframes (issue #12, PRD §4.4
GC-6/GC-7).

Two sources of truth exist for an iframe request:

- URL parameters Classroom appends when it loads the iframe (camelCase;
  PRD §4.4 table). Client-supplied — never trusted for authorization.
- ``courses.courseWork.getAddOnContext``, called server-side with the
  signed-in user's broker-released credential. Google's docs recommend
  calling it on every iframe open; its ``userRole`` is the only role
  authority (PRD §6.9: Campus has no roles — Classroom provides them).

Every context dict carries ``source``:

- ``"classroom"`` — getAddOnContext succeeded (the deployment path;
  required there, so forged params without a matching Classroom post
  are rejected).
- ``"mock"`` — local development only (``current_app.debug``); the
  historical get_mock_context defaults, parameterized by URL args.
"""

from __future__ import annotations

import re

from . import classroom_auth as cauth

# Per-view URL parameters (PRD §4.4 iframes table). Classroom appends
# camelCase params; login_hint is accepted and ignored (the Campus
# identity comes from the Campus session, not the URL).
_VIEW_PARAMS: dict[str, tuple[str, ...]] = {
    "teacher": ("courseId", "itemId", "attachmentId"),
    "student": ("courseId", "itemId", "attachmentId"),
    "review": ("courseId", "itemId", "attachmentId", "submissionId"),
}

# The Classroom role allowed to open each view. Review is a teacher
# surface (PRD §4.4); discovery has no login and no role gate.
VIEW_ROLES: dict[str, str] = {
    "teacher": "TEACHER",
    "student": "STUDENT",
    "review": "TEACHER",
}

# getAddOnContext needs the matching add-on scope per role (both are in
# the MVP connect set, so the broker ask-floor already covers them).
VIEW_SCOPES: dict[str, tuple[str, ...]] = {
    "teacher": ("https://www.googleapis.com/auth/classroom.addons.teacher",),
    "student": ("https://www.googleapis.com/auth/classroom.addons.student",),
    "review": ("https://www.googleapis.com/auth/classroom.addons.teacher",),
}

# Opaque Google identifiers are short alphanumeric tokens; addOnToken is
# a JWT-style dotted token. The cap keeps hostile params out of logs and
# upstream URLs.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
_PARAM_MAX_LEN = 2048


class ParamError(ValueError):
    """A required URL parameter is missing or malformed (HTTP 400)."""


class ContextError(Exception):
    """The Classroom context rejects this iframe request.

    status maps to the HTTP response; code is a stable machine string
    (JSON bodies on /addon/* follow the classroom_auth error shape).
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _clean_param(name: str, value: str | None) -> str | None:
    """Validate one opaque URL parameter; None means absent."""
    if value is None or value == "":
        return None
    value = value.strip()
    if len(value) > _PARAM_MAX_LEN:
        raise ParamError(f"{name} is too long")
    if "\n" in value or "\r" in value:
        raise ParamError(f"{name} contains control characters")
    return value


def _clean_id(name: str, value: str | None, required: bool) -> str | None:
    value = _clean_param(name, value)
    if value is None:
        if required:
            raise ParamError(f"Missing required URL parameter: {name}")
        return None
    if not _ID_RE.match(value):
        raise ParamError(f"{name} is not a valid identifier")
    return value


def parse_params(view: str, args) -> dict:
    """Parse and validate the URL parameters for one iframe view.

    Returns a dict keyed by the original camelCase names, absent params
    omitted. Raises ParamError (HTTP 400) on missing required params or
    values that fail the identifier charset.
    """
    required = _VIEW_PARAMS[view]
    parsed: dict = {}
    for name in ("courseId", "itemId", "attachmentId", "submissionId"):
        value = _clean_id(name, args.get(name), required=name in required)
        if value is not None:
            parsed[name] = value
    token = _clean_param("addOnToken", args.get("addOnToken"))
    if token is not None:
        if not _TOKEN_RE.match(token):
            raise ParamError("addOnToken is not a valid token")
        parsed["addOnToken"] = token
    return parsed


def resolve_real_context(
        client: "cauth.ClassroomClient", view: str, params: dict,
) -> dict:
    """Build the server-validated context for one iframe view.

    Calls courses.courseWork.getAddOnContext with the user's broker-
    released credential (never an app credential) and enforces the view's
    role. ClassroomAPIError 403/404 — the post doesn't exist, the
    attachment isn't under this item, or the user can't see it — maps to
    ContextError(403) so forged params reveal nothing (mirrors #28's
    answer-404-not-403 stance). Other API errors propagate to the
    app-wide handler (502 JSON on /addon/*).
    """
    expected_role = VIEW_ROLES[view]
    try:
        data = client.get_addon_context(
            params["courseId"],
            params["itemId"],
            attachment_id=params.get("attachmentId"),
            add_on_token=params.get("addOnToken"),
        )
    except cauth.ClassroomAPIError as err:
        if err.status_code in (401, 403, 404):
            # 401 included: a valid-looking token for a post the user
            # cannot access surfaces as 401/403 depending on grant state,
            # and neither may leak the post's existence.
            raise ContextError(
                403,
                "context_rejected",
                "Classroom did not confirm this iframe context; open the "
                "attachment from its Classroom post.",
            ) from err
        raise

    role = data.get("userRole")
    if role != expected_role:
        raise ContextError(
            403,
            "role_mismatch",
            f"This view is for {expected_role} accounts; Classroom "
            f"reports {role or 'no role'} for this user.",
        )

    return {
        "source": "classroom",
        "userRole": role,
        "courseId": params["courseId"],
        "itemId": data.get("itemId") or params["itemId"],
        "itemType": data.get("itemType") or params.get("itemType"),
        "attachmentId": params.get("attachmentId"),
        "submissionId": params.get("submissionId"),
        # The token itself is never reflected into the page (echo risk);
        # callers only learn whether one was presented.
        "addOnTokenPresent": "addOnToken" in params,
    }


def mock_context(view: str, args) -> dict:
    """Local-development context (the historical get_mock_context).

    Only reachable when ``current_app.debug`` — routes gate this — so the
    deployment can never serve fabricated context.
    """
    defaults = {
        "source": "mock",
        "userRole": VIEW_ROLES[view],
        "courseId": "test_course_123",
        "itemId": "test_item_456",
        "itemType": "courseWork",
        "attachmentId": "test_attachment_789",
        "submissionId": "test_submission_001",
        "addOnTokenPresent": False,
    }
    for name in ("courseId", "itemId", "attachmentId", "submissionId"):
        value = args.get(name)
        if value:
            defaults[name] = value
    return defaults
