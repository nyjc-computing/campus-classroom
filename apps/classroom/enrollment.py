"""apps.classroom.enrollment

Classroom-enrollment checks for the PRD v1.3 authorization model (#28).

PRD v1.3 rules 2 and 3 both turn on one question: is the signed-in user
a member of a Google Classroom course this assignment is linked to?

- Rule 2 (`/a/{id}` never public): a signed-in non-owner may view the
  assignment content only when they are in a course in the assignment's
  `classroom_links`.
- Rule 3 (classroom-scoped attempt/submit): a student may create or
  mutate a submission only under the same condition.

The answer comes from the user's own Classroom courses
(`courses.list` through the auth bridge) intersected with the
assignment's `classroom_links`. Any membership counts: `courses.list`
returns the courses the user is in as student or teacher, and a course
teacher can already see the CourseWork in Classroom, so course-scoped
visibility is the intent — assignment-owner-only editing is unchanged
(rule 1, enforced in the assignment routes).

Failure semantics are fail-closed: errors from the auth bridge and
Classroom API propagate as `classroom_auth` errors (never swallowed
into a "not enrolled" answer):

- `NotConnectedError`: the user has not connected google.classroom —
  callers offer the campus-profile connect prompt.
- `MissingClassroomScopesError`: the grant lacks `courses.readonly` —
  reconnect guidance.
- `ClassroomAPIError` / `OAuthFlowError`: Classroom unreachable —
  surfaced (browser paths redirect to the connection page, API paths
  get a 502 JSON).
"""

from . import classroom_auth as cauth

# The only Classroom scope the enrollment check itself needs.
COURSES_READONLY_SCOPE = "https://www.googleapis.com/auth/classroom.courses.readonly"

# courses.list page size: one page, no pagination (ClassroomClient does
# not follow nextPageToken). A user with more than 100 active courses
# would see enrollment checks miss beyond the first page — unrealistic
# for a school account; revisit with the Student View listing (#14),
# which needs real pagination anyway.
_COURSES_PAGE_SIZE = 100


def linked_course_ids(assignment) -> set[str]:
    """Course ids referenced by the assignment's classroom_links."""
    return {
        link.course_id
        for link in (getattr(assignment, "classroom_links", None) or [])
        if getattr(link, "course_id", None)
    }


def user_course_ids() -> set[str]:
    """Ids of the signed-in user's Google Classroom courses.

    Raises the classroom_auth errors listed in the module docstring.
    """
    with cauth.with_classroom_session(
            required_scopes=(COURSES_READONLY_SCOPE,)) as classroom:
        return {
            course["id"]
            for course in classroom.courses_list(page_size=_COURSES_PAGE_SIZE)
            if course.get("id")
        }


def is_enrolled(assignment) -> bool:
    """True when the signed-in user is in a course the assignment links to.

    An assignment with no classroom_links is enrolled for nobody (only
    its owner can see or attempt it).
    """
    linked = linked_course_ids(assignment)
    if not linked:
        return False
    return bool(linked & user_course_ids())


__all__ = [
    "COURSES_READONLY_SCOPE",
    "is_enrolled",
    "linked_course_ids",
    "user_course_ids",
]
