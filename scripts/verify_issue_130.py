"""Verify the issue #130 fix: co-tutors can review submissions.

Classroom's own submission contract is: course students may only view
their own work; any course teacher (creator or co-tutor) may view all
student submissions. Campus Classroom was stricter — every review surface
gated on the campus assignment creator, so a co-tutor got 404s on work
they can already see in Classroom.

Fix: `enrollment.taught_courses()` (courses.list(teacherId=me), the same
courses.readonly scope, live-proven in the #10 smoke test) +
`enrollment.can_review(assignment, user_id)` (creator OR teacher of a
linked course), applied to the three submission API gates, the feedback
POST (previously open to ANY authenticated user), the two page routes,
and the staff inbox filter (taught-course ids passed server-side, degrading
to own-only on bridge failure like the rule-4 listing).

Usage:
  python scripts/verify_issue_130.py            # behavioral + static checks (CI-safe)

The behavioral checks run `can_review` against a stubbed auth bridge
(per-user course sets, call-counting): creator pass-through, co-tutor
pass, student refusal, TTL-cache reuse, cross-user cache isolation, and
error propagation. The static checks pin every gate site.
"""

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

import flask

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from apps.classroom import classroom_auth as cauth  # noqa: E402
from apps.classroom import enrollment  # noqa: E402

checks = []


def check(name, ok, detail=""):
    checks.append(ok)
    marker = "PASS  " if ok else "FAIL  "
    suffix = f"  [{detail}]" if (detail and not ok) else ""
    print(marker + name + suffix)


def assignment(created_by="creator", course_ids=("c-1",)):
    return SimpleNamespace(
        created_by=created_by,
        classroom_links=[
            SimpleNamespace(course_id=cid) for cid in course_ids
        ],
    )


class _FakeClient:
    """courses_list stub resolving per-user course sets from flask.g."""

    calls = []

    def __init__(self, required):
        self.required = required

    def courses_list(self, *, page_size=30, teacher_only=False):  # noqa: ARG002
        user_id = flask.g.user.id
        any_courses, taught = PER_USER_COURSES[user_id]
        _FakeClient.calls.append((user_id, "taught" if teacher_only else "any"))
        return taught if teacher_only else any_courses


@contextlib.contextmanager
def _fake_bridge(required_scopes=None):
    yield _FakeClient(required_scopes)


# user -> (courses they are in, courses they teach), API-shaped
# (courses.list payloads) so _cached_courses consumes them unchanged.
_C1 = [{"id": "c-1", "name": "CampusClass"}]
PER_USER_COURSES = {
    "creator": (_C1, _C1),
    "co-tutor": (_C1, _C1),
    "student": (_C1, []),
    "outsider": ([], []),
}


def behavioral_checks():
    # Real semantics, fake bridge: can_review must answer from course
    # sets exactly like the live path, with the documented error and
    # caching contracts.
    cauth.with_classroom_session = _fake_bridge

    _app = flask.Flask("verify_issue_130")

    @contextlib.contextmanager
    def as_user(user_id):
        with _app.app_context():
            flask.g.user = SimpleNamespace(id=user_id)
            yield

    _FakeClient.calls.clear()

    with as_user("creator"):
        check("creator passes without any Classroom call",
              enrollment.can_review(assignment(), "creator")
              and not _FakeClient.calls)

    with as_user("co-tutor"):
        ok = enrollment.can_review(assignment(), "co-tutor")
        taught_calls = [c for c in _FakeClient.calls
                        if c[0] == "co-tutor" and c[1] == "taught"]
        check("co-tutor (teacher of a linked course) passes", ok)
        check("co-tutor check consulted courses.list(teacherId=me)",
              len(taught_calls) == 1, f"calls={_FakeClient.calls}")

    before = len(_FakeClient.calls)
    with as_user("co-tutor"):
        enrollment.can_review(assignment(), "co-tutor")
    check("repeat check inside the TTL reuses the cache",
          len(_FakeClient.calls) == before, f"calls={_FakeClient.calls}")

    with as_user("student"):
        check("course student is NOT a reviewer",
              not enrollment.can_review(assignment(), "student"))

    # Cross-user isolation: the student's taught lookup must be its own
    # Classroom call, not the co-tutor's cached set.
    with as_user("student"):
        enrollment.can_review(assignment("creator", ("c-1",)), "student")
    check("cache is per-user (student got their own lookup)",
          any(c[0] == "student" for c in _FakeClient.calls),
          f"calls={_FakeClient.calls}")

    with as_user("outsider"):
        check("user in no linked course is not a reviewer",
              not enrollment.can_review(assignment(), "outsider"))

    with as_user("creator"):
        check("assignment without classroom_links is creator-only",
              enrollment.can_review(assignment(course_ids=[]), "creator"))
    with as_user("co-tutor"):
        check("assignment without classroom_links refuses non-creator",
              not enrollment.can_review(assignment(course_ids=[]), "co-tutor"))

    @contextlib.contextmanager
    def _dead_bridge(required_scopes=None):  # noqa: ARG001
        raise cauth.NotConnectedError()
        yield  # pragma: no cover

    enrollment._courses_cache.clear()  # a warm cache answers without the bridge
    cauth.with_classroom_session = _dead_bridge
    try:
        with as_user("co-tutor"):
            enrollment.can_review(assignment(), "co-tutor")
        check("bridge failure propagates (fail-closed contract)", False,
              "NotConnectedError was swallowed")
    except cauth.ClassroomAuthError:
        check("bridge failure propagates (fail-closed contract)", True)

    # The inbox degrades to own-only on bridge failure (route-level try).
    enrollment._courses_cache.clear()


def static_checks():
    src_submissions = (
        REPO_ROOT / "apps" / "classroom" / "routes" / "submissions.py"
    ).read_text(encoding="utf-8")
    src_app = (
        REPO_ROOT / "apps" / "classroom" / "__init__.py"
    ).read_text(encoding="utf-8")
    src_assignments = (
        REPO_ROOT / "apps" / "classroom" / "routes" / "assignments.py"
    ).read_text(encoding="utf-8")
    src_view = (
        REPO_ROOT / "apps" / "classroom" / "templates" / "submissions"
        / "view.html"
    ).read_text(encoding="utf-8")
    src_list = (
        REPO_ROOT / "apps" / "classroom" / "templates" / "submissions"
        / "list.html"
    ).read_text(encoding="utf-8")

    check("all three submission API gates use can_review",
          src_submissions.count("enrollment.can_review") >= 4)
    check("the open feedback door is closed",
          "any authenticated user can add feedback" not in src_submissions)
    check("drill-down page gate widened",
          "can_review" in src_app and
          "created_by" not in src_app.split(
              "def assignment_submissions")[1].split("def ")[0])
    check("review page gate widened + serves assignment payload",
          "assignment_questions" in src_app
          and "can_review" in src_app.split("def submission_view")[1]
          .split("def ")[0])
    check("GET /api/v1/assignments/<id> stays creator-only (no builder for co-tutors)",
          "Owner-only (PRD §6.9 rule 1)" in src_assignments)
    check("review page no longer client-fetches the creator-only assignment API",
          "fetch(`/api/v1/assignments/" not in src_view
          and "assignment_questions" in src_view)
    check("inbox filter widens to taught courses",
          "TAUGHT_COURSE_IDS" in src_list and "isReviewable" in src_list)
    check("inbox route degrades taught set to own-only on bridge failure",
          "taught_course_ids" in src_app
          and "degraded to own assignments" in src_app)


def main():
    behavioral_checks()
    static_checks()
    failed = sum(1 for ok in checks if not ok)
    if failed:
        print(f"\n{failed} check(s) FAILED")
        return 1
    print(f"\nAll {len(checks)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
