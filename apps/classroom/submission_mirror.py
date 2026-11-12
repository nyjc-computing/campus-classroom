"""apps.classroom.submission_mirror

Mirror Campus submission actions into Google Classroom (issue #104).

Campus is the source of truth for attempts (snapshot-on-Campus model),
so without a mirror Google Classroom's own turn-in state would lag
silently and staff reading the CourseWork would miss submissions. After
a campus submit has committed, this module walks the owning student's
released Classroom credential (the ``with_classroom_session()`` seam,
demanded with ``CLASSROOM_SCOPES_MIRROR``) through:

1. ``studentSubmissions.list`` (userId=me) — resolve the student's
   Classroom submission for the CourseWork the campus submission's
   course maps to, and read its state.
2. ``modifyAttachments`` — attach the per-submission review URL
   (``origin + /submissions/<id>``, via ``campus.common.utils.url`` —
   never hand-concatenated hosts) as a url-only link, unless one with
   the same URL is already attached (match on url, so retries are
   idempotent).
3. ``turnIn`` — unless already ``TURNED_IN`` (a no-op; Classroom's own
   resubmit semantics cover turning in again after a teacher RETURN).

After a campus unsubmit: ``reclaim`` — its FAILED_PRECONDITION (409)
means there is nothing to reclaim (already NEW/reclaimed) and counts as
success.

Contract (never block the campus action): the mirror runs in-request
(two or three quick Google calls; a queue is a later refinement), every
failure is retried once when transient (5xx/429/transport), and a final
failure is logged as a WARNING carrying the submission id and error
code, then reported to the route as a status string. Routes embed the
status in their JSON response (``"mirror": ...``) without changing
their own success/failure in any way.

Scope degradation (the designed pre-campus#1095 state): the released
credential may not carry ``classroom.coursework.me`` — a
MissingClassroomScopesError (grant misses the scope) or, until
campus#1095 widens the vault cap, the broker's AUTH_INVALID_SCOPE
rejection (BrokerConfigError) of the ask itself. Both are logged as a
WARNING naming campus#1095 and reported as ``skipped_missing_scope``:
the mirror stays skipped until the vault/GCP work lands and users
reconnect. Never raised into the route.
"""

from __future__ import annotations

import logging

import campus.common.utils.url as campus_url
import requests

from . import classroom_auth as cauth
from .classroom_auth import CLASSROOM_SCOPES_MIRROR

logger = logging.getLogger(__name__)

# Stable mirror statuses the routes embed in their JSON responses.
STATUS_MIRRORED = "mirrored"
STATUS_SKIPPED_MISSING_SCOPE = "skipped_missing_scope"
STATUS_FAILED = "failed"

# The Classroom submission state that makes turnIn a no-op.
_STATE_TURNED_IN = "TURNED_IN"

# The review-link attachment payload is url-only (Classroom fetches
# title + thumbnail itself, validated live for #104).
_LINK_ATTACHMENT_KEY = "link"


class MirrorError(Exception):
    """A mirror step could not complete; reported as STATUS_FAILED."""


def mirror_submit(assignment, submission) -> str:
    """Best-effort mirror of a committed campus submit: attach the
    review link and turn in (unless Classroom is already TURNED_IN).

    Returns STATUS_MIRRORED, STATUS_SKIPPED_MISSING_SCOPE or
    STATUS_FAILED; never raises.
    """
    return _best_effort(_submit_steps, assignment, submission)


def mirror_unsubmit(assignment, submission) -> str:
    """Best-effort mirror of a committed campus unsubmit: reclaim the
    Classroom submission (already-reclaimed precondition -> success).

    Returns STATUS_MIRRORED, STATUS_SKIPPED_MISSING_SCOPE or
    STATUS_FAILED; never raises.
    """
    return _best_effort(_unsubmit_steps, assignment, submission)


# --- entry plumbing -----------------------------------------------------------

def _best_effort(steps, assignment, submission) -> str:
    """Run a mirror; every failure path ends in a logged WARNING."""
    try:
        return steps(assignment, submission)
    except cauth.MissingClassroomScopesError as err:
        logger.warning(
            "submission %s: Classroom grant lacks %s; mirror skipped "
            "until the user reconnects (campus#1095)",
            submission.id, ", ".join(err.missing),
        )
        return STATUS_SKIPPED_MISSING_SCOPE
    except cauth.BrokerConfigError as err:
        # The broker refused the ask itself: until campus#1095 widens
        # the vault cap, coursework.me is not grantable yet. Same
        # designed skip as a missing grant scope — not an action failure.
        logger.warning(
            "submission %s: classroom.coursework.me is not grantable yet "
            "(vault cap; campus#1095); mirror skipped — %s",
            submission.id, err.message,
        )
        return STATUS_SKIPPED_MISSING_SCOPE
    except cauth.ClassroomAuthError as err:
        logger.warning(
            "submission %s: Classroom mirror failed (%s: %s)",
            submission.id, err.code, err.message,
        )
        return STATUS_FAILED
    except Exception as err:  # the contract is never-raise, ever
        logger.warning(
            "submission %s: Classroom mirror failed (%s: %s)",
            submission.id, type(err).__name__, err,
        )
        return STATUS_FAILED


def _submit_steps(assignment, submission) -> str:
    coursework = _coursework_for_submission(assignment, submission)
    review_url = _review_url(submission.id)
    with cauth.with_classroom_session(
            required_scopes=CLASSROOM_SCOPES_MIRROR) as classroom:
        _with_one_retry(
            lambda: _turn_in_steps(classroom, coursework, review_url))
    return STATUS_MIRRORED


def _unsubmit_steps(assignment, submission) -> str:
    coursework = _coursework_for_submission(assignment, submission)
    with cauth.with_classroom_session(
            required_scopes=CLASSROOM_SCOPES_MIRROR) as classroom:
        _with_one_retry(lambda: _reclaim_step(classroom, coursework))
    return STATUS_MIRRORED


def _with_one_retry(action) -> None:
    """Run a step sequence, retrying once on transient failures.

    The steps are idempotent (attachment match is by url, and the state
    is re-read before turnIn), so a full retry is safe even after a
    partial first pass — an earlier lost-response turnIn shows up as
    TURNED_IN and no-ops.
    """
    for attempt in (1, 2):
        try:
            action()
            return
        except (cauth.ClassroomAPIError, requests.RequestException) as err:
            if attempt == 2 or not _is_transient(err):
                raise
            logger.info(
                "Classroom mirror step failed transiently (%s); retrying once",
                err,
            )


def _is_transient(err) -> bool:
    """True for failures a retry can plausibly fix (429/5xx/transport)."""
    if isinstance(err, requests.RequestException):
        return True
    return isinstance(err, cauth.ClassroomAPIError) and (
        err.status_code >= 500 or err.status_code == 429
    )


# --- step sequences -----------------------------------------------------------

def _turn_in_steps(classroom, coursework, review_url) -> None:
    course_id, coursework_id = coursework
    student_submission = _resolve_student_submission(classroom, coursework)
    if student_submission.get("state") == _STATE_TURNED_IN:
        # Already turned in (a previous mirror pass, or the student
        # turned in natively in Classroom): never double-turn-in.
        return

    submission_id = student_submission["id"]
    attachments = (student_submission.get("assignmentSubmission") or {}).get(
        "attachments") or []
    already_attached = any(
        (a.get(_LINK_ATTACHMENT_KEY) or {}).get("url") == review_url
        for a in attachments
    )
    if not already_attached:
        classroom.student_submission_modify_attachments(
            course_id, coursework_id, submission_id,
            [{_LINK_ATTACHMENT_KEY: {"url": review_url}}],
        )
    classroom.student_submission_turn_in(course_id, coursework_id, submission_id)


def _reclaim_step(classroom, coursework) -> None:
    course_id, coursework_id = coursework
    student_submission = _resolve_student_submission(classroom, coursework)
    submission_id = student_submission["id"]
    try:
        classroom.student_submission_reclaim(
            course_id, coursework_id, submission_id)
    except cauth.ClassroomAPIError as err:
        if err.status_code == 409:
            # FAILED_PRECONDITION: nothing to reclaim (state NEW, or
            # already reclaimed) — the unsubmit's mirror goal holds.
            return
        raise


# --- resolution helpers ---------------------------------------------------------

def _resolve_student_submission(classroom, coursework) -> dict:
    """The caller's studentSubmission for this CourseWork (userId=me)."""
    student_submissions = classroom.student_submissions_list(
        coursework[0], coursework[1])
    if not student_submissions:
        raise MirrorError(
            "Classroom returned no studentSubmission for the caller")
    student_submission = student_submissions[0]
    if not student_submission.get("id"):
        raise MirrorError("Classroom studentSubmission carries no id")
    return student_submission


def _coursework_for_submission(assignment, submission) -> tuple[str, str]:
    """The (course_id, coursework_id) pair the campus submission maps to.

    The submission records the Classroom course it was opened from; the
    assignment's classroom_links pair that course with its CourseWork.
    """
    for link in (getattr(assignment, "classroom_links", None) or []):
        if (getattr(link, "course_id", None) == submission.course_id
                and getattr(link, "coursework_id", None)):
            return link.course_id, link.coursework_id
    raise MirrorError(
        f"assignment {assignment.id} has no Classroom link for course "
        f"{submission.course_id!r}"
    )


def _review_url(submission_id: str) -> str:
    """Absolute per-submission review URL (the chip's target).

    The origin comes from the canonical-origin builder (PUBLIC_URL), the
    repo guardrail for absolute URL generation.
    """
    origin = campus_url.canonical_origin()
    return f"{origin}/submissions/{submission_id}"


__all__ = [
    "STATUS_FAILED",
    "STATUS_MIRRORED",
    "STATUS_SKIPPED_MISSING_SCOPE",
    "mirror_submit",
    "mirror_unsubmit",
]
