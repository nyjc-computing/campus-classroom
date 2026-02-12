# Schema Proposal: Assignments & Submissions

This document proposes the schema for the Assignments and Submissions resources to be added to Campus API.

## Design Principles

Following Campus API patterns:
- Dataclass models inheriting from `Model`
- PostgreSQL storage via `PostgreSQLTable`
- JSONB for complex nested data (questions, classroom_links)
- Auto-generated Campus IDs (e.g., `assignment_xxxxxxxx`, `submission_xxxxxxxx`)
- `created_at` timestamp inherited from base Model

## Deadline Handling

**MVP Decision:** Skip deadline enforcement in campus-classroom.

**Rationale:**
- Google Classroom does not emit pub/sub events (no webhooks)
- Polling Classroom API adds complexity/latency
- Teachers already manage deadlines in Classroom workflow
- For MVP, let Classroom be the single source of truth

**Future:** Can add polling-based checks with caching layer if needed.

---

## 1. Assignment Model

### 1.1 Python Dataclass

**File:** `campus/model/assignment.py`

```python
"""campus.model.assignment

Assignment model for Campus Classroom.
"""

from dataclasses import dataclass, field

from campus.common import schema
from campus.common.utils import uid

from .base import Model


@dataclass(eq=False, kw_only=True)
class Question:
    """A single question within an assignment.

    Question IDs use hierarchical dot notation:
    - Level 1: q1, q2, q3, ... (main questions)
    - Level 2: q1.a, q1.b, q1.c, ... (parts)
    - Level 3: q1.a.i, q1.a.ii, q1.b.i, ... (subparts)
    - Level 4+: q1.a.i.1, q1.a.i.2, ... (further nesting)

    The hierarchy is self-describing in the ID - no parent_id needed.
    """
    id: str  # e.g., "q1", "q1.a", "q1.a.i"
    prompt: str  # Context/passage (may be empty)
    question: str  # The actual question/task

    @property
    def level(self) -> int:
        """Return the hierarchy level (1-indexed)."""
        return len(self.id.split("."))

    @property
    def parent_id(self) -> str | None:
        """Return the parent question ID, or None for top-level questions."""
        parts = self.id.split(".")
        if len(parts) <= 1:
            return None
        return ".".join(parts[:-1])

    @property
    def root_id(self) -> str:
        """Return the root question ID (e.g., 'q1' for 'q1.a.ii')."""
        return self.id.split(".")[0]


@dataclass(eq=False, kw_only=True)
class ClassroomLink:
    """Link to a Google Classroom assignment.

    One assignment can be linked to multiple Classroom classes.
    """
    course_id: str  # Google Classroom course ID
    coursework_id: str  # Google Classroom assignment ID
    attachment_id: str | None = None  # Google Classroom attachment ID
    linked_at: schema.DateTime = field(default_factory=schema.DateTime.utcnow)


@dataclass(eq=False, kw_only=True)
class Assignment(Model):
    """Dataclass representation of an assignment record.

    An assignment contains structured questions and can be linked
    to one or more Google Classroom classes.

    Note: Deadlines are managed by Google Classroom, not stored here.
    """
    id: schema.CampusID = field(default_factory=(
        lambda: uid.generate_category_uid("assignment", length=8)
    ))
    # created_at inherited from Model
    title: str
    description: str = ""
    questions: list[Question] = field(default_factory=list)
    created_by: schema.UserID  # Teacher who created the assignment
    updated_at: schema.DateTime = field(
        default_factory=schema.DateTime.utcnow,
        metadata={"mutable": True}
    )
    # Links to Google Classroom (one-to-many)
    classroom_links: list[ClassroomLink] = field(default_factory=list)

    def get_question_tree(self) -> dict:
        """Return questions as a nested tree structure."""
        tree: dict = {}
        for q in self.questions:
            node = {
                "id": q.id,
                "prompt": q.prompt,
                "question": q.question,
                "children": []
            }
            if q.level == 1:
                tree[q.id] = node
            elif q.parent_id in tree:
                tree[q.parent_id]["children"].append(node)
        return tree

    def get_question(self, question_id: str) -> Question | None:
        """Get a question by ID."""
        for q in self.questions:
            if q.id == question_id:
                return q
        return None

    def get_questions_for_root(self, root_id: str) -> list[Question]:
        """Get all questions under a root question (including subparts)."""
        prefix = f"{root_id}."
        return [
            q for q in self.questions
            if q.id == root_id or q.id.startswith(prefix)
        ]
```

### 1.2 PostgreSQL Schema

**Table:** `assignments`

```sql
CREATE TABLE IF NOT EXISTS "assignments" (
    "id" TEXT PRIMARY KEY,
    "created_at" TIMESTAMP NOT NULL,
    "title" TEXT NOT NULL,
    "description" TEXT NOT NULL,
    "questions" JSONB NOT NULL DEFAULT '[]',
    "created_by" TEXT NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "classroom_links" JSONB NOT NULL DEFAULT '[]'
);

-- Indexes for common queries
CREATE INDEX idx_assignments_created_by ON "assignments"("created_by");
CREATE INDEX idx_assignments_questions ON "assignments" USING GIN("questions");
CREATE INDEX idx_assignments_classroom_links ON "assignments" USING GIN("classroom_links");
```

### 1.3 Question ID Format

**Hierarchical dot notation** - self-describing hierarchy:

| Level | Format | Examples |
|-------|--------|----------|
| 1 | `qN` | q1, q2, q3 |
| 2 | `qN.letter` | q1.a, q1.b, q2.a |
| 3 | `qN.letter.roman` | q1.a.i, q1.a.ii, q1.b.i |
| 4+ | `qN.letter.roman.number` | q1.a.i.1, q1.a.i.2 |

**Parsing logic:**
- Split by `.` to get level
- Remove last segment to get parent
- First segment is the root

### 1.4 questions JSONB Structure

```json
[
  {
    "id": "q1",
    "prompt": "Read the following passage about photosynthesis...",
    "question": "What is the main theme?"
  },
  {
    "id": "q1.a",
    "prompt": "",
    "question": "Identify two supporting details."
  },
  {
    "id": "q1.a.i",
    "prompt": "",
    "question": "Explain your first choice."
  },
  {
    "id": "q1.a.ii",
    "prompt": "",
    "question": "Explain your second choice."
  },
  {
    "id": "q2",
    "prompt": "Consider the following scenario...",
    "question": "Analyze the cause and effect."
  }
]
```

### 1.4 classroom_links JSONB Structure

```json
[
  {
    "course_id": "d123456789",
    "coursework_id": "987654321",
    "attachment_id": "112233",
    "linked_at": "2025-01-28T10:30:00Z"
  },
  {
    "course_id": "d987654321",
    "coursework_id": "123456789",
    "attachment_id": "445566",
    "linked_at": "2025-01-28T10:31:00Z"
  }
]
```

---

## 2. Submission Model

### 2.1 Python Dataclass

**File:** `campus/model/submission.py`

```python
"""campus.model.submission

Submission model for Campus Classroom.
"""

from dataclasses import dataclass, field

from campus.common import schema
from campus.common.utils import uid

from .base import Model


@dataclass(eq=False, kw_only=True)
class Response:
    """A student's response to a single question."""
    question_id: str  # References Assignment.question.id
    response_text: str  # Student's free-text response


@dataclass(eq=False, kw_only=True)
class Feedback:
    """Teacher feedback on a specific question response."""
    question_id: str  # References Response.question_id
    feedback_text: str
    teacher_id: schema.UserID
    created_at: schema.DateTime = field(default_factory=schema.DateTime.utcnow)


@dataclass(eq=False, kw_only=True)
class Submission(Model):
    """Dataclass representation of a student submission.

    A submission contains student responses to an assignment's questions,
    along with optional teacher feedback.

    Deadline enforcement: Check Classroom API for due date and lock
    editing when passed.
    """
    id: schema.CampusID = field(default_factory=(
        lambda: uid.generate_category_uid("submission", length=8)
    ))
    # created_at inherited from Model
    assignment_id: schema.CampusID  # References Assignment.id
    student_id: schema.UserID
    course_id: str  # Google Classroom course ID (for querying)
    responses: list[Response] = field(default_factory=list)
    feedback: list[Feedback] = field(default_factory=list)
    submitted_at: schema.DateTime | None = None
    updated_at: schema.DateTime = field(
        default_factory=schema.DateTime.utcnow,
        metadata={"mutable": True}
    )

    def get_response(self, question_id: str) -> Response | None:
        """Get a response by question ID."""
        for r in self.responses:
            if r.question_id == question_id:
                return r
        return None

    def get_feedback(self, question_id: str) -> Feedback | None:
        """Get feedback for a specific question."""
        for f in self.feedback:
            if f.question_id == question_id:
                return f
        return None

    def is_submitted(self) -> bool:
        """Check if submission has been finalized."""
        return self.submitted_at is not None
```

### 2.2 PostgreSQL Schema

**Table:** `submissions`

```sql
CREATE TABLE IF NOT EXISTS "submissions" (
    "id" TEXT PRIMARY KEY,
    "created_at" TIMESTAMP NOT NULL,
    "assignment_id" TEXT NOT NULL,
    "student_id" TEXT NOT NULL,
    "course_id" TEXT NOT NULL,
    "responses" JSONB NOT NULL DEFAULT '[]',
    "feedback" JSONB NOT NULL DEFAULT '[]',
    "submitted_at" TIMESTAMP,
    "updated_at" TIMESTAMP NOT NULL,
    -- Foreign key (soft reference, no constraint)
    CONSTRAINT fk_assignment FOREIGN KEY ("assignment_id")
        REFERENCES "assignments"("id") ON DELETE CASCADE
);

-- Indexes for common queries
CREATE INDEX idx_submissions_assignment ON "submissions"("assignment_id");
CREATE INDEX idx_submissions_student ON "submissions"("student_id");
CREATE INDEX idx_submissions_course ON "submissions"("course_id");
CREATE INDEX idx_submissions_responses ON "submissions" USING GIN("responses");
CREATE INDEX idx_submissions_feedback ON "submissions" USING GIN("feedback");

-- Unique constraint: one submission per student per assignment per course
CREATE UNIQUE INDEX idx_submissions_unique
    ON "submissions"("assignment_id", "student_id", "course_id");
```

### 2.3 responses JSONB Structure

```json
[
  {
    "question_id": "q1",
    "response_text": "The main theme is the cycle of energy in nature..."
  },
  {
    "question_id": "q1.a",
    "response_text": "1. Sunlight provides energy... 2. Plants convert..."
  },
  {
    "question_id": "q1.a.i",
    "response_text": "Sunlight is captured by chlorophyll..."
  },
  {
    "question_id": "q2",
    "response_text": "The cause is..."
  }
]
```

### 2.4 feedback JSONB Structure

```json
[
  {
    "question_id": "q1",
    "feedback_text": "Good overview, but expand on the energy transfer aspect.",
    "teacher_id": "teacher@example.com",
    "created_at": "2025-01-28T14:30:00Z"
  },
  {
    "question_id": "q1.a",
    "feedback_text": "Excellent details! Consider mentioning glucose.",
    "teacher_id": "teacher@example.com",
    "created_at": "2025-01-28T14:30:00Z"
  }
]
```

---

## 3. Deadline Enforcement via Classroom API

Since deadlines are managed by Google Classroom, the platform must check the Classroom API for due dates.

### 3.1 Get Due Date from Classroom

```python
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def get_assignment_due_date(
    classroom_service,
    course_id: str,
    coursework_id: str
) -> schema.DateTime | None:
    """Get the due date for a Classroom assignment."""
    coursework = classroom_service.courses().courseWork().get(
        courseId=course_id,
        id=coursework_id
    ).execute()

    if 'dueDate' in coursework:
        # Classroom returns dueDate as { year, month, day }
        # Optionally with dueTime { hours, minutes, seconds }
        due = coursework['dueDate']
        return schema.DateTime.from_parts(
            due['year'], due['month'], due['day']
        )
    return None  # No due date set
```

### 3.2 Check if Submission is Editable

```python
def is_submission_editable(
    assignment: Assignment,
    student_id: str,
    classroom_service
) -> bool:
    """Check if a student can still edit their submission."""
    # Get the Classroom link for this student's course
    course_id = get_course_id_for_student(assignment, student_id)

    # Check Classroom due date
    for link in assignment.classroom_links:
        if link.course_id == course_id:
            due_date = get_assignment_due_date(
                classroom_service,
                link.course_id,
                link.coursework_id
            )
            if due_date and due_date < schema.DateTime.utcnow():
                return False  # Past deadline, locked
            return True  # No deadline or not yet passed

    return False  # No Classroom link found
```

---

## 3. Storage Implementation

### 3.1 Assignment Storage

**File:** `campus/api/resources/assignment.py` (new module)

```python
from campus.storage.tables import get_table
from campus.model.assignment import Assignment

# Initialize table
assignments_table = get_table("assignments")

def create_assignment(assignment: Assignment) -> Assignment:
    """Create a new assignment."""
    assignments_table.insert_one(assignment.to_storage())
    return assignment

def get_assignment(assignment_id: str) -> Assignment:
    """Get an assignment by ID."""
    record = assignments_table.get_by_id(assignment_id)
    return Assignment.from_storage(record)

def list_assignments_by_teacher(teacher_id: str) -> list[Assignment]:
    """List all assignments created by a teacher."""
    records = assignments_table.get_matching({"created_by": teacher_id})
    return [Assignment.from_storage(r) for r in records]

def update_assignment(assignment_id: str, update: dict) -> Assignment:
    """Update an assignment."""
    Assignment.validate_update(update)
    update["updated_at"] = schema.DateTime.utcnow()
    assignments_table.update_by_id(assignment_id, update)
    return get_assignment(assignment_id)

def add_classroom_link(assignment_id: str, link: ClassroomLink) -> None:
    """Add a Google Classroom link to an assignment."""
    assignment = get_assignment(assignment_id)
    assignment.classroom_links.append(link)
    update_assignment(assignment_id, {
        "classroom_links": [asdict(l) for l in assignment.classroom_links]
    })
```

### 3.2 Submission Storage

**File:** `campus/api/resources/submission.py` (new module)

```python
from campus.storage.tables import get_table
from campus.model.submission import Submission

# Initialize table
submissions_table = get_table("submissions")

def create_submission(submission: Submission) -> Submission:
    """Create a new submission."""
    submissions_table.insert_one(submission.to_storage())
    return submission

def get_submission(submission_id: str) -> Submission:
    """Get a submission by ID."""
    record = submissions_table.get_by_id(submission_id)
    return Submission.from_storage(record)

def get_submission_for_student(
    assignment_id: str,
    student_id: str
) -> Submission | None:
    """Get a student's submission for an assignment."""
    records = submissions_table.get_matching({
        "assignment_id": assignment_id,
        "student_id": student_id
    })
    if records:
        return Submission.from_storage(records[0])
    return None

def list_submissions_by_assignment(assignment_id: str) -> list[Submission]:
    """List all submissions for an assignment."""
    records = submissions_table.get_matching({"assignment_id": assignment_id})
    return [Submission.from_storage(r) for r in records]

def update_submission(submission_id: str, update: dict) -> Submission:
    """Update a submission."""
    Submission.validate_update(update)
    update["updated_at"] = schema.DateTime.utcnow()
    submissions_table.update_by_id(submission_id, update)
    return get_submission(submission_id)

def submit_response(
    submission_id: str,
    question_id: str,
    response_text: str
) -> None:
    """Add or update a response for a question."""
    submission = get_submission(submission_id)

    # Update existing response or add new one
    for i, r in enumerate(submission.responses):
        if r.question_id == question_id:
            submission.responses[i] = Response(
                question_id=question_id,
                response_text=response_text
            )
            break
    else:
        submission.responses.append(Response(
            question_id=question_id,
            response_text=response_text
        ))

    update_submission(submission_id, {
        "responses": [asdict(r) for r in submission.responses]
    })

def add_feedback(
    submission_id: str,
    question_id: str,
    feedback_text: str,
    teacher_id: str
) -> None:
    """Add teacher feedback for a question response."""
    submission = get_submission(submission_id)

    # Remove existing feedback for this question
    submission.feedback = [
        f for f in submission.feedback if f.question_id != question_id
    ]

    # Add new feedback
    submission.feedback.append(Feedback(
        question_id=question_id,
        feedback_text=feedback_text,
        teacher_id=teacher_id
    ))

    update_submission(submission_id, {
        "feedback": [asdict(f) for f in submission.feedback]
    })

def finalize_submission(submission_id: str) -> Submission:
    """Mark submission as submitted (sets submitted_at)."""
    return update_submission(submission_id, {
        "submitted_at": schema.DateTime.utcnow()
    })
```

---

## 4. API Endpoints

### 4.1 Assignment Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/assignments` | Create assignment |
| GET | `/api/v1/assignments` | List assignments (filtered by query) |
| GET | `/api/v1/assignments/{id}` | Get assignment |
| PATCH | `/api/v1/assignments/{id}` | Update assignment |
| DELETE | `/api/v1/assignments/{id}` | Delete assignment |
| POST | `/api/v1/assignments/{id}/links` | Add Classroom link |

### 4.2 Submission Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/submissions` | Create submission |
| GET | `/api/v1/submissions` | List submissions (filtered) |
| GET | `/api/v1/submissions/{id}` | Get submission |
| GET | `/api/v1/submissions/by-assignment/{assignment_id}` | List by assignment |
| GET | `/api/v1/submissions/by-student/{student_id}` | List by student |
| PATCH | `/api/v1/submissions/{id}` | Update submission |
| POST | `/api/v1/submissions/{id}/responses` | Add/update response |
| POST | `/api/v1/submissions/{id}/feedback` | Add feedback |
| POST | `/api/v1/submissions/{id}/submit` | Finalize submission |

---

## 5. Migration

**File:** `migrations/versions/001_add_classroom_resources.py`

```python
"""Add assignments and submissions tables.

Revision ID: 001
Create Date: 2025-01-28
"""
from campus.storage.tables.backend.postgres import PostgreSQLTable

def upgrade():
    """Create assignments and submissions tables."""
    from campus.model.assignment import Assignment
    from campus.model.submission import Submission

    assignments = PostgreSQLTable("assignments")
    submissions = PostgreSQLTable("submissions")

    assignments.init_from_model("assignments", Assignment)
    submissions.init_from_model("submissions", Submission)

def downgrade():
    """Drop assignments and submissions tables."""
    # Implementation for rollback
    pass
```

---

## 6. Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | Should assignments be soft-deleted or hard-deleted? | Cascade delete submissions? |
| 2 | Should we track deletion of Classroom links? | Add `deleted_links` array? |
| 3 | Should feedback be versioned? | Track edits to feedback? |
| 4 | What happens if Classroom has no due date? | Allow editing indefinitely? |

**Resolved:**
- ~~Deadline timezone handling?~~ → Use Classroom API, handles timezones
- ~~Deadline storage location?~~ → Managed by Google Classroom
