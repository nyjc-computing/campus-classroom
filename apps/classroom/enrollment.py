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
- Review access (#130, mirroring Classroom's own submission contract):
  the assignment creator OR any teacher of a linked course may view a
  submission — students of the course still see only their own work.

The answer comes from the user's own Classroom courses
(`courses.list` through the auth bridge) intersected with the
assignment's `classroom_links`. Any membership counts: `courses.list`
returns the courses the user is in as student or teacher, and a course
teacher can already see the CourseWork in Classroom, so course-scoped
visibility is the intent — assignment-owner-only editing is unchanged
(rule 1, enforced in the assignment routes). Teacher-of-course answers
use the same endpoint filtered to `teacherId=me` (`courses.list` accepts
the filter under the same `courses.readonly` scope — live-proven in the
issue #10 smoke test, so no rosters scope is needed).

Failure semantics are fail-closed: errors from the auth bridge and
Classroom API propagate as `classroom_auth` errors (never swallowed
into a "not enrolled" answer):

- `NotConnectedError`: the user has not connected google.classroom —
  callers offer the campus-profile connect prompt.
- `MissingClassroomScopesError`: the grant lacks a demanded scope
  (`courses.readonly` — or, on the /a/ path, the #104 mirror scope) —
  reconnect guidance.
- `ClassroomAPIError` / `OAuthFlowError`: Classroom unreachable —
  surfaced (browser paths redirect to the connection page, API paths
  get a 502 JSON). The rule-4 listing is the one deliberate exception:
  a discovery page degrades to the caller's own assignments (every
  content read still passes rules 2/3, so the degradation cannot leak).
"""

import time

import flask

from . import classroom_auth as cauth

# The only Classroom scope the enrollment check itself needs.
COURSES_READONLY_SCOPE = "https://www.googleapis.com/auth/classroom.courses.readonly"

# Both course lookups are live Classroom calls, and several routes need
# them per request (the review gates fire once per by-assignment fetch in
# the staff inbox's N+1, the /a/ gates on every load/save/submit). Cache
# successes per user for this long — a membership change taking up to a
# minute to be visible is fine, failures are never cached.
_COURSES_CACHE_TTL_SECONDS = 60
_courses_cache: dict[str, tuple[float, dict[str, str]]] = {}


def linked_course_ids(assignment) -> set[str]:
    """Course ids referenced by the assignment's classroom_links."""
    return {
        link.course_id
        for link in (getattr(assignment, "classroom_links", None) or [])
        if getattr(link, "course_id", None)
    }


def _cached_courses(
        scope: str, *, taught_only: bool,
        required_scopes: tuple[str, ...] = (COURSES_READONLY_SCOPE,),
) -> dict[str, str]:
    """`user_courses`/`taught_courses` through a short per-user cache.

    Cache key carries the campus user id (the cache is process-wide and
    gunicorn serves many users concurrently — a userless key would leak
    one teacher's courses to another) AND the demanded scope set: a
    success cached for the MVP-only demand must never satisfy a wider
    demand (the /a/ mirror gate would silently stop demanding the
    scope). Outside a request (fixtures) there is no identity to key
    on: skip the cache entirely.
    """
    try:
        cache_key = (
            f"{flask.g.user.id}:{scope}:{','.join(sorted(required_scopes))}"
        )
    except (RuntimeError, AttributeError):
        cache_key = None
    if cache_key:
        hit = _courses_cache.get(cache_key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    with cauth.with_classroom_session(
            required_scopes=required_scopes) as classroom:
        courses = {
            course["id"]: course.get("name") or course["id"]
            for course in classroom.courses_list(
                page_size=100, teacher_only=taught_only)
            if course.get("id")
        }
    if cache_key:
        _courses_cache[cache_key] = (
            time.monotonic() + _COURSES_CACHE_TTL_SECONDS, courses)
    return courses


def user_courses(*, extra_required_scopes: tuple[str, ...] = ()) -> dict[str, str]:
    """The signed-in user's Google Classroom courses (id → name).

    Courses they are in as student or teacher — the rule 2/3/4 membership
    set. Raises the classroom_auth errors listed in the module docstring.

    `extra_required_scopes` widens the credential demand beyond
    `courses.readonly` (the /a/ answer sheet adds the #104 mirror scope
    so a stale credential reconnects before the submit whose turn-in
    would otherwise silently skip); every missing scope raises
    MissingClassroomScopesError as usual.
    """
    return _cached_courses(
        "any", taught_only=False,
        required_scopes=(COURSES_READONLY_SCOPE, *extra_required_scopes),
    )


def taught_courses() -> dict[str, str]:
    """Courses the signed-in user TEACHES (id → name).

    `courses.list(teacherId=me)` — the role-aware answer the review
    surface needs (#130): a course student shares the course with the
    assignment but must not see other students' submissions, so review
    gating cannot use the any-membership `user_courses`. Same failure
    semantics as `user_courses`.
    """
    return _cached_courses("taught", taught_only=True)


def teaches_linked_course(assignment) -> bool:
    """True when the user teaches a course the assignment links to.

    An assignment with no classroom_links teaches for nobody (only its
    owner can review it).
    """
    if not linked_course_ids(assignment):
        return False
    return bool(linked_course_ids(assignment) & taught_courses().keys())


def can_review(assignment, user_id) -> bool:
    """Review access, mirroring Classroom's submission contract (#130).

    True for the assignment creator — answered without any Classroom
    call — or for a teacher of one of its linked courses. Everyone else
    (course students included) is not a reviewer: Classroom lets students
    see only their own work.

    ClassroomAuthErrors from the taught check PROPAGATE (fail-closed with
    the designed connect/re-auth UX), exactly like `is_enrolled` for
    rules 2/3 — callers that cannot carry that contract (the inbox
    listing) degrade explicitly instead.
    """
    if str(getattr(assignment, "created_by", "")) == str(user_id):
        return True
    return teaches_linked_course(assignment)


def enrolled_courses(
        assignment, *, extra_required_scopes: tuple[str, ...] = (),
) -> list[dict]:
    """Courses the signed-in user shares with this assignment.

    Ordered by the assignment's classroom_links (the teacher's send
    order), each as {"course_id", "name"} — the share page keys a
    submission to one of these. Empty when the user is in none of them
    or the assignment has no classroom_links at all. See `user_courses`
    for `extra_required_scopes`.
    """
    mine = user_courses(extra_required_scopes=extra_required_scopes)
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
    "can_review",
    "enrolled_courses",
    "is_enrolled",
    "linked_course_ids",
    "taught_courses",
    "teaches_linked_course",
    "user_courses",
]
