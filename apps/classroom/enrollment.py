"""apps.classroom.enrollment

Classroom-enrollment checks for the PRD v1.3 authorization model (#28).

PRD v1.3 rules 2 and 3 both turn on one question: is the signed-in user
a member of a Google Classroom course this assignment is linked to?

- Rule 2 (`/a/{id}` never public): a signed-in non-owner may view the
  assignment content only when they are in a course in the assignment's
  `classroom_links`.
- Rule 3 (classroom-scoped attempt/submit): a student may create or
  mutate a submission only under the same condition.
- Rule 4 (assigned-only listing, #14): a user's assignment list contains
  their own assignments plus those linked to a course they are in —
  never an enumerable index of all assignments.

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
  get a 502 JSON). The rule-4 listing is the one deliberate exception:
  a discovery page degrades to the caller's own assignments (every
  content read still passes rules 2/3, so the degradation cannot leak).
"""

from . import classroom_auth as cauth

# The only Classroom scope the enrollment check itself needs.
COURSES_READONLY_SCOPE = "https://www.googleapis.com/auth/classroom.courses.readonly"


def linked_course_ids(assignment) -> set[str]:
    """Course ids referenced by the assignment's classroom_links."""
    return {
        link.course_id
        for link in (getattr(assignment, "classroom_links", None) or [])
        if getattr(link, "course_id", None)
    }


def user_courses() -> dict[str, str]:
    """The signed-in user's Google Classroom courses (id → name).

    Raises the classroom_auth errors listed in the module docstring.
    """
    with cauth.with_classroom_session(
            required_scopes=(COURSES_READONLY_SCOPE,)) as classroom:
        return {
            course["id"]: course.get("name") or course["id"]
            for course in classroom.courses_list(page_size=100)
            if course.get("id")
        }


def enrolled_courses(assignment) -> list[dict]:
    """Courses the signed-in user shares with this assignment.

    Ordered by the assignment's classroom_links (the teacher's send
    order), each as {"course_id", "name"} — the share page keys a
    submission to one of these. Empty when the user is in none of them
    or the assignment has no classroom_links at all.
    """
    mine = user_courses()
    seen: dict[str, None] = {}
    for link in (getattr(assignment, "classroom_links", None) or []):
        course_id = getattr(link, "course_id", None)
        if course_id and course_id in mine:
            seen.setdefault(course_id, None)
    return [
        {"course_id": course_id, "name": mine[course_id]}
        for course_id in seen
    ]


def is_enrolled(assignment) -> bool:
    """True when the signed-in user is in a course the assignment links to.

    An assignment with no classroom_links is enrolled for nobody (only
    its owner can see or attempt it).
    """
    if not linked_course_ids(assignment):
        return False
    return bool(enrolled_courses(assignment))


__all__ = [
    "COURSES_READONLY_SCOPE",
    "enrolled_courses",
    "is_enrolled",
    "linked_course_ids",
    "user_courses",
]
