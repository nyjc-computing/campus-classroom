"""apps.classroom.classroom_sync

Send-to-Classroom orchestration (issue #10, PRD §6.7 primary flow, GC-8).

For each Classroom course the teacher selected:

1. Check attachment eligibility via userProfiles.checkUserCapability
   (public preview; any API failure counts as ineligible — graceful
   degradation per PRD §6.7).
2. Eligible: create a draft CourseWork (no materials), then a
   CreateAddOnAttachment whose view URIs point at this deployment's
   /addon/teacher|student|review routes.
3. Ineligible: create the draft CourseWork with a Link Material pointing
   at the shareable /a/{assignment_id} page (students open content in a
   new tab).
4. Persist the ClassroomLink in Campus (GC-8) via
   client.api.assignments[id].links.add — course_id + coursework_id
   always, attachment_id only when the add-on attachment exists.

One class failing never aborts the batch: every course gets an outcome
dict the picker UI renders per class. Re-sending to courses that are not
linked yet is allowed (retry after a partial failure); already-linked
courses are reported as already_linked, and the assignment's edit-lock
then applies as usual (routes/assignments.py).
"""

from __future__ import annotations

import campus.common.utils.url as campus_url

from . import classroom_auth as cauth

# userProfiles.checkUserCapability capability gating attachment creation
# (PRD §6.7). Needs classroom.addons.teacher — already in the MVP connect
# set — and the V1_20240930_PREVIEW preview version (classroom_auth).
_CREATE_ADDON_CAPABILITY = "CREATE_ADD_ON_ATTACHMENT"

# Stable outcome status values for the picker UI.
STATUS_ATTACHED = "attached"  # CourseWork + add-on attachment created
STATUS_LINKED = "linked"  # CourseWork with Link Material, no attachment
STATUS_ALREADY_LINKED = "already_linked"
STATUS_ERROR = "error"


def _view_uris(origin: str) -> dict:
    """teacher/student/review view URIs for CreateAddOnAttachment.

    Bare route URLs: Google Classroom appends courseId, itemId,
    attachmentId (and submissionId for review) itself when it iframes
    them. Real in-iframe context handling is session #4's scope.
    """
    return {
        "teacherViewUri": {"uri": f"{origin}/addon/teacher"},
        "studentViewUri": {"uri": f"{origin}/addon/student"},
        "studentWorkReviewUri": {"uri": f"{origin}/addon/review"},
    }


def _link_material(share_url: str, title: str) -> list[dict]:
    """CourseWork materials payload for the Link Material fallback."""
    return [{"link": {"url": share_url, "title": title}}]


def send_assignment_to_classroom(
    campus_client,
    classroom: cauth.ClassroomClient,
    assignment,
    course_ids: list[str],
) -> list[dict]:
    """Post one Campus assignment to one or more Classroom courses.

    Args:
        campus_client: authenticated Campus user session (the open
            campus.with_user_session() context).
        classroom: ready ClassroomClient (the open with_classroom_session()
            context, granted the Send scopes).
        assignment: Campus Assignment resource
            (client.api.assignments[assignment_id].get()).
        course_ids: Classroom course IDs the teacher selected.

    Returns:
        One outcome dict per requested course (deduplicated, order kept):
        {course_id, status, coursework_id, attachment_id, message}.
    """
    origin = campus_url.canonical_origin()
    share_url = f"{origin}/a/{assignment.id}"
    linked_course_ids = {link.course_id for link in (assignment.classroom_links or [])}

    outcomes: list[dict] = []
    for course_id in dict.fromkeys(course_ids):  # dedupe, keep order
        if course_id in linked_course_ids:
            outcomes.append({
                "course_id": course_id,
                "status": STATUS_ALREADY_LINKED,
                "coursework_id": None,
                "attachment_id": None,
                "message": "This class already has this assignment posted.",
            })
            continue
        outcomes.append(_send_to_course(
            campus_client, classroom, assignment, course_id, origin, share_url,
        ))
    return outcomes


def _send_to_course(campus_client, classroom, assignment, course_id, origin, share_url) -> dict:
    """Send one assignment to one course; never raises for API failures."""
    base = {
        "course_id": course_id,
        "coursework_id": None,
        "attachment_id": None,
        "message": "",
    }

    eligible = classroom.check_user_capability(_CREATE_ADDON_CAPABILITY)
    coursework_body: dict = {
        "title": assignment.title,
        "state": "DRAFT",
        "workType": "ASSIGNMENT",
    }
    if assignment.description:
        coursework_body["description"] = assignment.description
    if not eligible:
        coursework_body["materials"] = _link_material(share_url, assignment.title)

    try:
        coursework = classroom.coursework_create(course_id, coursework_body)
    except cauth.ClassroomAPIError as err:
        return {**base, "status": STATUS_ERROR, "message": err.message}
    coursework_id = coursework.get("id", "")
    if not coursework_id:
        return {
            **base,
            "status": STATUS_ERROR,
            "message": "Classroom did not return an assignment ID for the "
                       "created draft; nothing was linked.",
        }

    attachment_id = None
    if eligible:
        try:
            attachment = classroom.addon_attachment_create(
                course_id, coursework_id,
                {"title": assignment.title, **_view_uris(origin)},
            )
            attachment_id = attachment.get("id")
        except cauth.ClassroomAPIError as err:
            # The capability check passed but creation was refused — e.g.
            # the developer project is not Marketplace-approved yet
            # (session #3). Degrade to the Link Material fallback: patch
            # the link onto the existing draft and store the link without
            # attachment_id (the ClassroomLink model allows that).
            fallback_ok = _try_patch_link_material(
                classroom, course_id, coursework_id, share_url, assignment.title,
            )
            message = (
                f"Add-on attachment was refused ({err.message}); "
                + ("posted with the assignment link instead."
                   if fallback_ok else
                   "the assignment link could not be added — add it in "
                   "Classroom manually.")
            )
            return _persist_link(
                campus_client, assignment, course_id, coursework_id,
                None, STATUS_LINKED, message,
            )

    status = STATUS_ATTACHED if attachment_id else STATUS_LINKED
    return _persist_link(
        campus_client, assignment, course_id, coursework_id,
        attachment_id, status, base["message"],
    )


def _try_patch_link_material(classroom, course_id, coursework_id, share_url, title) -> bool:
    """Best-effort: add a Link Material to an existing draft CourseWork."""
    try:
        classroom.coursework_patch(course_id, coursework_id, {
            "materials": _link_material(share_url, title),
        })
        return True
    except cauth.ClassroomAPIError:
        return False


def _persist_link(campus_client, assignment, course_id, coursework_id,
                  attachment_id, status, message) -> dict:
    """Store the ClassroomLink in Campus (GC-8) and shape the outcome."""
    try:
        campus_client.api.assignments[assignment.id].links.add(
            course_id=course_id,
            coursework_id=coursework_id,
            attachment_id=attachment_id,
        )
    except Exception as err:  # Campus failure must not lose the batch
        return {
            "course_id": course_id,
            "status": STATUS_ERROR,
            "coursework_id": coursework_id,
            "attachment_id": attachment_id,
            "message": f"Classroom accepted the draft but Campus could not "
                       f"record the link ({err}); retry sending to this "
                       "class may create a duplicate draft.",
        }
    return {
        "course_id": course_id,
        "status": status,
        "coursework_id": coursework_id,
        "attachment_id": attachment_id,
        "message": message,
    }
