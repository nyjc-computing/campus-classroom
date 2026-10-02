# PRD: Campus Classroom - Assignment Platform

**Status:** Draft
**Version:** 1.3
**Date:** 2026-10-02

## Changelog
- **v1.3** (2026-10-02): Authorization model made explicit (§6.9): assignments are editable only by their owner (group ownership post-MVP), assignment content is never visible to the public (the `/a/` share page becomes gated — Campus sign-in plus, for students, enrollment in a linked Classroom course; this deliberately supersedes the anonymous link-preview behavior shipped via issue #24), attempting/submitting requires enrollment in a Classroom course the assignment was assigned to, and logged-in students see only assignments assigned to them. Found during the issue #24 review: none of these rules are enforced today (no ownership/visibility checks on Campus API assignment routes; the share page renders anonymously)
- **v1.2** (2026-10-02): Corrected §6.4 scope names to Google's canonical set — `coursework.readonly` → `course-work.readonly` (hyphenated; unhyphenated name does not exist), dropped `coursework.students.readonly` (noncanonical alias of `student-submissions.students.readonly`), added `courses.readonly` (required for `courses.list()`). Found by Google rejecting the scopes at first real consent (issue #9); canonical source of truth is the GCP "Data access" scope list
- **v1.1** (2025-02-12): Resolved question hierarchy UI (flat list for MVP, anticipate tree view if complexity grows)
- **v1.0** (2025-02-12): Added grade passback implementation reference (API patterns for maxPoints, pointsEarned, assignedGrade)
- **v0.9** (2025-02-11): Updated assignment creation flow to API-first with eligibility check (CREATE_ADD_ON_ATTACHMENT capability), Link Upgrade demoted to post-MVP fallback
- **v0.8** (2025-02-11): Added Link Upgrade feature documentation, resolved feedback release timing (uses Classroom Pub/Sub to detect RETURNED state), added push notifications scope, resolved auto-save behavior (follow Google Suite)
- **v0.7** (2025-02-11): Resolved Google Workspace Marketplace SDK configuration (Private/Admin-only = no public approval), documented incremental scope approval workflow for Classroom scopes via Campus API
- **v0.6** (2025-02-10): Added iframe URL parameter details, proposed routes, clarified authorization model via Google Classroom API, updated scopes based on API discovery
- **v0.5** (2025-01-28): Initial draft

---

## 1. Overview

### 1.1 Purpose
Develop an assignment platform that integrates with Google Classroom as an Add-On. Teachers create question-based assignments on the platform; students complete them within the Classroom iframe. Teachers provide feedback via the platform; grades are managed in Google Classroom.

### 1.2 Goals (MVP)
- Seamless in-Classroom experience via Add-On iframe
- Structured question assignments (prompt + question + answer)
- Teacher feedback on student submissions
- Authentication via Campus API
- Data storage via Campus API (Assignments, Submissions resources)

### 1.3 Future Goals (Post-MVP)
- Telemetry collection on student engagement
- Grade passback to Google Classroom
- Rich response types (equations, diagrams)
- Annotated feedback (on student answers or prompts)
- Highlighting and annotation tools

### 1.4 Non-Goals
- Grading dashboard on the platform (grading in Classroom only)
- Offline access to assignments
- Native Google Drive file types
- Telemetry in MVP

---

## 2. User Personas

### 2.1 Teacher
- Creates assignments on the platform (prompt + structured questions)
- Provides text-based feedback on student submissions
- Grades students within Google Classroom interface
- Manages assignment deadlines (students can edit until deadline)

### 2.2 Student
- Opens and completes assignments within Google Classroom iframe
- Logs in using Campus API (same Google account as Classroom)
- Submits free-text responses to structured questions
- Can edit submission until deadline

### 2.3 Admin
- [Define if needed]

---

## 3. User Stories

### 3.1 Teacher Stories (MVP)
- [ ] As a teacher, I want to create an assignment with prompt, questions, and answers
- [ ] As a teacher, I want to set a deadline for student submissions
- [ ] As a teacher, I want to provide text feedback on student submissions
- [ ] As a teacher, I want to link assignments to Google Classroom courses
- [ ] As a teacher, I want to edit assignments until they are posted

### 3.2 Student Stories (MVP)
- [ ] As a student, I want to open and complete assignments without leaving Classroom
- [ ] As a student, I want to log in using my Campus account
- [ ] As a student, I want to submit free-text responses to structured questions
- [ ] As a student, I want to edit my submission until the deadline

### 3.3 System Stories (MVP)
- [ ] As the system, I want to store assignments in Campus API
- [ ] As the system, I want to store submissions in Campus API
- [ ] As the system, I want to authenticate via Campus API

### 3.4 Future Stories (Post-MVP)
- [ ] As a teacher, I want to view telemetry on how students engaged with assignments
- [ ] As a teacher, I want to provide annotated feedback on student answers
- [ ] As the system, I want to send grades to Google Classroom automatically
- [ ] As a student, I want to have a link to my work in Google Drive

---

## 4. Functional Requirements

### 4.1 Authentication
- [ ] **AUTH-1:** Platform must integrate with existing Campus API for authentication
- [ ] **AUTH-2:** Users log in with Google account (same as Classroom account)
- [ ] **AUTH-3:** Platform validates user access via Campus API (no course mapping in MVP)

### 4.2 Assignment Structure (MVP)
- [ ] **AC-1:** Assignment contains title and description
- [ ] **AC-2:** Assignment contains one or more questions, each with:
  - `prompt` (context/passage for the question)
  - `question` (question ending in `?` or task ending in `.`)
  - `answer` (student's free-text response area)
- [ ] **AC-3:** Questions support hierarchical structure (Question 1 → Part a → Subpart i)
- [ ] **AC-4:** Assignments editable by teacher until posted to Classroom

**Note:** Deadline enforcement skipped for MVP - managed by Google Classroom only.

### 4.3 Submission & Feedback (MVP)
- [ ] **SUB-1:** Student submits free-text responses to each question
- [ ] **SUB-2:** Student can edit submission until they choose to submit
- [ ] **SUB-3:** Teacher views student submissions in platform
- [ ] **SUB-4:** Teacher provides plain-text feedback on each question response
- [ ] **SUB-5:** Submissions stored in Campus API

**Note:** No deadline enforcement - students can edit whenever Classroom allows.

### 4.4 Google Classroom Integration (Add-On)
- [ ] **GC-1:** Platform must register as a Google Classroom Add-On via Workspace Marketplace SDK
- [x] **GC-2:** **Attachment Discovery iframe** - Teacher selects assignment during Classroom assignment creation — *resolved API-first (§6.7, §7.3 D1): teachers post from the platform's "Send to Google Classroom" flow instead of an in-Classroom picker; `/addon/discovery` renders a public pointer page to that flow (issue #13)*
- [ ] **GC-3:** **Teacher View iframe** - Teacher creates and configures assignments
- [ ] **GC-4:** **Student View iframe** - Student completes assignment within Classroom
- [ ] **GC-5:** **Student Work Review iframe** - Teacher views submissions and provides feedback
- [ ] **GC-6:** Platform receives Classroom metadata via URL params (see §6.9 for details)
- [ ] **GC-7:** Platform validates iframe context via `getAddOnContext` API
- [ ] **GC-8:** Google Classroom metadata stored in Campus Assignment resource

**Iframe URL Parameters (passed by Google Classroom):**

| Parameter | Description | Present In |
|-----------|-------------|------------|
| `courseId` | Identifier of the course | All iframe views |
| `itemId` | Identifier of Announcement, CourseWork, or CourseWorkMaterial | All iframe views |
| `itemType` | Type of item (courseWork, announcement, etc.) | All iframe views |
| `attachmentId` | Identifier of the add-on attachment | Teacher/Student View, Review |
| `submissionId` | Student's submission ID | Student Work Review only |
| `addOnToken` | Authorization token for in-Classroom attachment creation | Attachment Discovery |

**Note:** Parameter names use **camelCase** (not snake_case) as per Google Classroom API.

### 4.5 Data Storage (MVP)
- [ ] **DS-1:** All assignment data stored in Campus API (new Assignments resource)
- [ ] **DS-2:** All submission data stored in Campus API (new Submissions resource)
- [ ] **DS-3:** Platform only stores client-side session information (cookies)
- [ ] **DS-4:** Google Classroom metadata (course_id, coursework_id) stored in Campus Assignment

### 4.6 Telemetry Collection (Future - Post-MVP)
- [ ] **TEL-1:** Track when student opens assignment
- [ ] **TEL-2:** Track time spent on assignment
- [ ] **TEL-3:** Track student interactions (clicks, navigation)
- [ ] **TEL-4:** Track student responses/answers
- [ ] **TEL-5:** Track when student submits
- [ ] **TEL-6:** Link telemetry to assignment, student, and course identifiers

### 4.7 Grade Passback (Future - Post-MVP)
- [ ] **GP-1:** Platform can calculate auto-grades based on answers
- [ ] **GP-2:** Platform sends grades to Classroom via API
- [ ] **GP-3:** Grades appear as draft grades in Classroom (teacher can edit)

### 4.8 Student Artifacts (Future - Post-MVP)
- [ ] **SA-1:** Platform creates a Drive shortcut/link for each submission
- [ ] **SA-2:** Link points back to student's work on the platform

---

## 5. Non-Functional Requirements

### 5.1 Performance
- [ ] Submission save should complete within 5 seconds
- [ ] Assignment load should complete within 3 seconds

### 5.2 Security
- [ ] All API calls must use OAuth 2.0 with appropriate scopes
- [ ] Student data must be isolated by assignment/submission
- [ ] Access validation via Campus API
- [ ] Assignments are editable only by their owner (§6.9); group ownership is post-MVP
- [ ] Assignment content is never served to anonymous visitors (§6.9)
- [ ] Attempting/submitting requires enrollment in a Classroom course the assignment was assigned to (§6.9, §6.11)

### 5.3 Compatibility
- [ ] Platform must work in iframe context (no X-Frame-Options issues)
- [ ] Platform must handle Google's postMessage communication protocol

---

## 6. Technical Architecture

### 6.1 Components
```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Google Classroom                                │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  Your Platform Add-On (iframe)                                     │ │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐    │ │
│  │  │ Assignment      │  │ Student View    │  │ Telemetry       │    │ │
│  │  │ Creator         │  │ (student iframe)│  │ Collector       │    │ │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────┘    │ │
│  └────────────────────────────────────────────────────────────────────┘ │
│                              ↑                                          │
│                              │ API calls                                │
│                              │                                          │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │  Campus API (existing authentication)                              │ │
│  └────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

### 6.2 Add-On Iframe Views

| View | Purpose | User | Trigger |
|------|---------|------|---------|
| Attachment Discovery | Select/link assignment when creating Classroom assignment | Teacher | Creating assignment in Classroom |
| Teacher View | Create, edit, preview assignments | Teacher | Opening from platform |
| Student View | Complete assignment, submit responses | Student | Opening assignment in Classroom |
| Student Work Review | View submissions, provide feedback | Teacher | Opening student submission in Classroom |

### 6.3 APIs Used

**Campus API:**
- Authentication (existing - `auth.users`, `auth.logins`)
- Assignments (✅ implemented - `campus.model.assignment`, `campus.api.resources.assignment`)
- Submissions (✅ implemented - `campus.model.submission`, `campus.api.resources.submission`)
- Note: Campus API provides authentication only; role-based authorization comes from Google Classroom

**Google APIs:**
- **Classroom API** - Create draft assignments, read course/assignment metadata
- **Google OAuth 2.0** - User authentication

### 6.4 Google API Scopes (MVP)

**OAuth 2.0 Scopes:**

| Scope | Purpose | Required For |
|-------|---------|--------------|
| `openid` | Basic OAuth, get user ID | All users |
| `email` | Get user email | All users |
| `profile` | Get user profile info | All users |

**Classroom API Scopes:**

Scope names below are Google's **canonical** names (per the GCP "Data access"
list) — v1.2 corrects two names that Google rejects at the authorize URL.

| Scope | Purpose | Required For |
|-------|---------|--------------|
| `https://www.googleapis.com/auth/classroom.courses.readonly` | List/view courses (courses.list — course pickers) | All users |
| `https://www.googleapis.com/auth/classroom.addons.teacher` | Teacher iframe views, create attachments | Teachers |
| `https://www.googleapis.com/auth/classroom.addons.student` | Student iframe views | Students |
| `https://www.googleapis.com/auth/classroom.course-work.readonly` | Read assignment metadata (hyphenated: `course-work`, NOT `coursework` — the unhyphenated name does not exist) | All users |
| `https://www.googleapis.com/auth/classroom.student-submissions.me.readonly` | View student's own submissions | Students |
| `https://www.googleapis.com/auth/classroom.student-submissions.students.readonly` | View student submissions (canonical; `classroom.coursework.students.readonly` is a rejected noncanonical alias of this) | Teachers |
| `https://www.googleapis.com/auth/classroom.rosters.readonly` | View course rosters (for teacher verification) | Teachers |
| `https://www.googleapis.com/auth/classroom.push-notifications` | Register for Pub/Sub push notifications | Feedback release |
| `https://www.googleapis.com/auth/classroom.coursework.students` | Modify student submissions (return assignment) | Feedback release |

**Future Scopes (Post-MVP):**
- `https://www.googleapis.com/auth/classroom.courses` - Grade passback, manage courses (read-**write** scope; the readonly variant above is already MVP)
- `https://www.googleapis.com/auth/drive.readonly` - Drive shortcuts

### 6.5 Data Model (Draft)

**To be implemented in Campus API:**

```
Assignments (Campus API resource - NEW)
├── id (Campus auto-generated UID: assignment_xxxxxxxx)
├── title (string)
├── description (string, optional)
├── questions (JSON array)
│   └── [{id, prompt, question}]
├── created_by (Campus user ID)
├── created_at (DateTime)
├── updated_at (DateTime)
└── classroom_links (JSON array, one-to-many)
    └── [{course_id, coursework_id, attachment_id, linked_at}]

Submissions (Campus API resource - NEW)
├── id (Campus auto-generated UID: submission_xxxxxxxx)
├── assignment_id (Assignment ID)
├── student_id (Campus user ID)
├── course_id (Google Classroom course)
├── responses (JSON array)
│   └── [{question_id, response_text}]
├── feedback (JSON array, optional)
│   └── [{question_id, feedback_text, teacher_id, created_at}]
├── submitted_at (DateTime, nullable)
└── updated_at (DateTime)

Note: Deadlines NOT enforced by campus-classroom (Google Classroom concern only).
```

### 6.6 Question Structure (MVP)

**Question IDs use hierarchical dot notation** (self-describing, no parent_id needed):

| Level | Format | Examples |
|-------|--------|----------|
| 1 | `qN` | q1, q2, q3 |
| 2 | `qN.letter` | q1.a, q1.b, q2.a |
| 3 | `qN.letter.roman` | q1.a.i, q1.a.ii, q1.b.i |

```json
{
  "questions": [
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
    }
  ]
}
```

### 6.7 Multi-Class Workflow (MVP)

**Primary Flow: API-Based Attachment Creation**

1. Teacher creates assignment in campus-classroom (Teacher View)
2. Teacher clicks "Send to Google Classroom"
3. Platform shows list of teacher's Classroom courses (via `courses.list()`)
4. Teacher selects one or more classes
5. For each selected class:
   - Check eligibility: `userProfiles.checkUserCapability(userId="me", capability="CREATE_ADD_ON_ATTACHMENT")`
   - **If eligible:**
     - Create CourseWork assignment via API (without materials)
     - Create AddOnAttachment with teacher/student/review view URIs
     - Store `course_id`, `coursework_id`, `attachment_id` in `classroom_links`
   - **If ineligible (fallback):**
     - Create CourseWork assignment with Link Material pointing to assignment URL
     - Store `course_id`, `coursework_id` in `classroom_links` (no `attachment_id`)
     - Note: Students will open content in new tab instead of iframe
     - The share page is gated, not public (§6.9): after Campus sign-in, students see content only if enrolled in this course
6. Teacher can edit campus-classroom assignment until Classroom assignment is posted
7. Once posted in Classroom, assignment is "locked" for editing (future: configurable)

**Eligibility Check Requirements:**
- Scope: `https://www.googleapis.com/auth/classroom.coursework.students` (or readonly)
- Preview version: `V1_20240930_PREVIEW` (public preview, requires Workspace Developer Preview Program)
- Response: `{ "allowed": true/false }`

**Why API-First is Better:**
- No iframe context required (no `addOnToken` dependency)
- Works for any course where teacher has permissions
- Fallback to Link Material for ineligible users (graceful degradation)
- No need to email Google for Link Upgrade URL pattern configuration

### 6.8 Link Upgrade Feature (Manual Fallback)

Google Classroom supports "Link Upgrade" as an **alternative** workflow for teachers who prefer to create assignments directly in Classroom and attach campus-classroom content afterward.

**When this is used:**
- Teacher creates assignment directly in Google Classroom (not via campus-classroom)
- Teacher wants to attach existing campus-classroom assignment
- As fallback if API-based creation has issues

**How it works:**
1. Teacher creates assignment on campus-classroom platform
2. Platform generates shareable URL: `https://classroom.campus.nyjc.dev/a/{assignment_id}`
3. Teacher creates assignment in Google Classroom and pastes the link
4. Classroom detects the URL pattern and prompts: "Upgrade to add-on attachment?"
5. If teacher agrees, Link Upgrade iframe opens with context

**Link Upgrade iframe parameters:**

| Parameter | Description |
|-----------|-------------|
| `courseId` | Target Classroom course |
| `itemId` | Target Classroom assignment |
| `itemType` | Type (courseWork, announcement, etc.) |
| `addOnToken` | Authorization token |
| `urlToUpgrade` | The campus-classroom URL (URI-encoded) |

**URL pattern configuration** (email to classroom-link-upgrade-external@google.com):
```
Google Cloud Project number: GCP_PROJECT_NUMBER
Link Upgrade iframe URL: https://classroom.campus.nyjc.dev/addon/link-upgrade
URL Patterns:
- Host: classroom.campus.nyjc.dev
- Path prefixes: /a/
```

**Implementation:**
- Decode `urlToUpgrade` to extract assignment ID
- Call `CreateAddOnAttachment` with the assignment's pre-configured view URIs
- Show loading spinner, then close iframe via `postMessage`

**MVP Priority:** Post-MVP enhancement. Primary workflow is API-based creation (§6.7). Link Upgrade requires email configuration with Google and adds manual steps for teachers.

### 6.9 Iframe Routes

| Route | View Type | User | URL Params Received | Purpose | Priority |
|-------|-----------|------|-------------------|---------|----------|
| `/addon/teacher` | Teacher View | Teacher | `courseId`, `itemId`, `attachmentId` | Create, edit, preview assignments | MVP |
| `/addon/student` | Student View | Student | `courseId`, `itemId`, `attachmentId` | Complete assignment, submit responses | MVP |
| `/addon/review` | Student Work Review | Teacher | `courseId`, `itemId`, `attachmentId`, `submissionId` | View submissions, provide feedback | MVP |
| `/` | Landing | All | None | Main platform landing page | MVP |
| `/sign-in` | Sign In | All | None | Campus OAuth login | MVP |
| `/dashboard` | Dashboard | All | None | User's assignment overview | MVP |
| `/addon/discovery` | Attachment Discovery | Teacher | `courseId`, `itemId`, `addOnToken` | Select/create assignment to attach to Classroom | Post-MVP |
| `/addon/link-upgrade` | Link Upgrade | Teacher | `courseId`, `itemId`, `addOnToken`, `urlToUpgrade` | Upgrade pasted link to add-on attachment | Post-MVP |
| `/a/{assignment_id}` | Assignment Share | Signed-in users | None | Share URL for the Link Material fallback — gated per §6.9, never public | MVP |

**Iframe Specifications:**
- Responsive design supporting 1366x768 (low-end) to 4K displays
- Auto-resize enabled via `window.postMessage` to parent Classroom
- No `X-Frame-Options` or `Content-Security-Policy` restrictions blocking iframe embedding

### 6.9 Authorization Model

**Assignment visibility and ownership (platform rules):**

1. **Owner-only editing.** An assignment may be edited or deleted only by its owner (`created_by`). Group ownership (co-teachers) is a future feature; until then, no other principal may mutate an assignment — not even a co-teacher of the same course.
2. **Never public.** Assignment content is not visible to anonymous visitors. If it were, a student could forward the link and a non-enrolled person could read the assignment or attempt it on their behalf. The `/a/{assignment_id}` share page (Link Material fallback, §6.7) therefore requires Campus sign-in, and for students additionally enrollment in at least one Classroom course in the assignment's `classroom_links`. Google's anonymous link crawler sees the gated state, not the content — a deliberate trade-off that supersedes the anonymous link-preview behavior shipped via issue #24.
3. **Classroom-scoped attempt/submit.** Only students enrolled in a Classroom course to which the assignment was assigned (a course in its `classroom_links`) may attempt and submit it. Enrollment is checked against the signed-in student's Google-linked Classroom courses.
4. **Assigned-only listing for students.** A logged-in student's assignment list contains only assignments assigned to them via their courses — never an enumerable index of all assignments.

Teachers see their own assignments (`created_by = me`) and, for review, submissions from courses they teach (§6.11).

**Campus API** provides authentication (user identity) but **does not provide role-based authorization**. The Campus User model contains only: `id`, `email`, `name`, `activated_at`. The rules above must therefore be enforced by this platform's route layer (with owner checks on Campus API assignment mutations as defense in depth).

**Authorization (teacher vs student roles) is determined via Google Classroom API:**

1. When iframe loads, platform calls `getAddOnContext` with provided URL parameters
2. Response includes either `studentContext` or `teacherContext` (but never both)
3. Presence of `teacherContext` = user is a teacher for this course
4. Presence of `studentContext.submissionId` = user is a student with a submission

### 6.11 Feedback Release via Google Classroom Pub/Sub

To match teacher expectations set by Google Classroom, feedback is released to students **only when the teacher returns the assignment in Classroom**. This uses Google Classroom's push notification system via Cloud Pub/Sub.

**StudentSubmission State Values:**

| State | Meaning |
|-------|---------|
| `CREATED` | Submission created |
| `TURNED_IN` | Student turned in |
| `RETURNED` | **Teacher returned assignment - trigger feedback release** |
| `RECLAIMED_BY_STUDENT` | Student unsubmitted |
| `STUDENT_EDITED_AFTER_TURN_IN` | Student edited after turn-in |

**Implementation:**

1. **Create Cloud Pub/Sub topic** in Google Cloud Project
2. **Register for "course work changes" feed** using `registrations.create()`:
   ```python
   classroom_service.registrations().create(
       body={
           "feed": {
               "feedType": "courseWorkChangesPerCourse",
               "courseId": course_id
           },
           "cloudPubsubTopic": f"projects/{project}/topics/{topic}"
       }
   ).execute()
   ```
3. **Handle incoming notifications** (delivered within minutes):
   ```json
   {
     "collection": "courses.courseWork.studentSubmissions",
     "eventType": "UPDATED",
     "resourceId": {
       "courseId": "12345",
       "courseWorkId": "987654321",
       "id": "submission_id"
     }
   }
   ```
4. **Fetch submission state** and check if `state === "RETURNED"`
5. **Release feedback** to student (mark as visible)

**Required Scope:**
- `https://www.googleapis.com/auth/classroom.push-notifications`
- `https://www.googleapis.com/auth/classroom.student-submissions.students.readonly`

**Registration Management:**
- Registrations expire after **one week**
- Renew by calling `registrations.create()` with same parameters before expiry
- Store registration IDs for cleanup

**Cross-course access prevention:**
- Students can only access assignments for their enrolled courses (enforced by Classroom iframe context)
- Any teacher of the Google Classroom class can provide feedback (not just assignment creator)
- Platform validates `courseId` matches the context returned by `getAddOnContext`

### 6.10 Deployment Requirements

#### 6.10.1 Google Workspace Marketplace SDK Configuration

**App Visibility: Private (Internal Organization-Only)**

| Setting | Value | Notes |
|---------|-------|-------|
| App visibility | **Private** | Domain-only (nyjc.edu.sg) - not publicly listed |
| Unlisted | **Yes** | Does not appear in browse/search results |
| Installation | **Admin-only install** | Only Workspace admins can install for the domain |
| App integrations | **Classroom add-on** (checked only) | NOT: Google Workspace add-on, web app, Drive app, or Docs/Sheets/Slides add-ons |

**Implication:** Private visibility with Admin-only install = **no public Marketplace approval required**. The add-on can be used internally immediately after configuration.

**Google Cloud Console:**
- Google Cloud Project
- OAuth 2.0 Client ID (Web application)
- OAuth 2.0 Client Secret
- Authorized redirect URIs (e.g., `https://classroom.campus.nyjc.dev/finalize_login`)
- Authorized JavaScript origins
- Google Workspace Marketplace SDK configuration (for Add-On)

**Environment Variables:**

```bash
# Campus API (existing)
CLIENT_ID="campus_client_id"
CLIENT_SECRET="campus_client_secret"
ENV="development"  # or "staging" or "production"
SECRET_KEY="flask_session_secret"

# Canonical public origin of this service (scheme://host[:port], no path).
# Required for login: builds the OAuth callback {PUBLIC_URL}/finalize_login,
# which must be registered on the campus client's redirect_uris before use.
# HOSTNAME is a bind address and is NOT used for URL generation (campus#652).
PUBLIC_URL="https://classroom.campus.nyjc.dev"

# Google OAuth - NOT NEEDED (use Campus OAuth with incremental scope approval)
# Campus OAuth tokens will be refreshed to include Classroom scopes via incremental approval
# See Resolved Question #2 in §7.1

# Optional
PORT="5000"
```

**Google Cloud Console Setup:**
1. Create/select Google Cloud Project
2. Enable APIs:
   - Google Classroom API
   - Google+ API (or People API for user info)
   - Cloud Pub/Sub API (for push notifications)
3. Configure Workspace Marketplace SDK (see §6.10.1 above)
   - Set visibility to Private
   - Enable Unlisted
   - Configure Admin-only install
   - Select "Classroom add-on" app integration
4. Configure OAuth consent screen (through Campus API)
   - Add Classroom API scopes to Campus OAuth configuration
   - Implement incremental scope approval workflow for Classroom permissions
5. (Optional) Join Workspace Developer Preview Program
   - Required for `userProfiles.checkUserCapability()` API (CREATE_ADD_ON_ATTACHMENT check)
   - Currently in public preview (V1_20240930_PREVIEW)
   - Without this: fallback to Link Material for all users (graceful degradation)

---

## 7. Open Questions

### 7.1 Resolved Questions

| # | Question | Resolution |
|---|----------|------------|
| 1 | What is the approval process for becoming a Classroom Add-On? | **Not applicable** - Private deployment with Admin-only install (visibility: Private, Unlisted: Yes); no public Marketplace approval needed. See §6.10.1 for SDK configuration details |
| 2 | Do we need separate Google OAuth credentials for Classroom API calls? | **No** - Use incremental scope approval workflow through Campus API. Campus OAuth tokens don't include Classroom scopes by default; will need to request additional scope approval and refresh tokens with Classroom scopes |
| 3 | What happens when teacher deletes assignment on platform? | Warn about connected Classroom assignments, warn about inaccessibility, require confirmation before deletion |
| 4 | Should assignments be locked after posting to Classroom? | **Yes, for MVP** |
| 5 | Should teachers be able to "unlink" from a specific Classroom class? | Use "sync" button instead - checks linked Google Classrooms and unlinks if Classroom/link is deleted |
| 6 | Should we create one `AddOnAttachment` per class or share across classes? | One Campus Assignment with multiple `classroom_links`; each course gets its own `AddOnAttachment` |
| 7 | What happens if a student is enrolled in the same class multiple times? | Not possible in Google Classroom API |
| 8 | Should assignments be soft-deleted or hard-deleted? | Soft-delete with cascade via student ID (depth-first deletion) |
| 9 | Should we track deletion of Classroom links? | Won't track - not feasible as trigger |

### 7.2 Unresolved Questions

None - all questions resolved for MVP implementation.

### 7.3 Design Decisions Needed

| # | Decision | Options | Status |
|---|----------|---------|--------|
| 1 | Assignment creation flow | **Resolved: API-first with eligibility check** - Create CourseWork + AddOnAttachment via Classroom API, fallback to Link Material for ineligible users |
| 2 | Question hierarchy UI | **Resolved: Flat list for MVP** (easy to implement, clear mapping to schema's dot notation). Future: Consider tree view if complexity grows. Anticipate potential need to switch to tree structure as questions become more nested. |
| 3 | Auto-save behavior | **Resolved: Follow Google Suite** - Auto-save on input change (debounced ~1 second) |
| 4 | Feedback release timing | **Resolved: Release on "RETURNED" state** - Use Google Classroom Pub/Sub notifications to detect when teacher returns assignment |

### 7.4 Resolved for MVP

| # | Question | Resolution |
|---|----------|------------|
| 1 | Workspace Marketplace SDK for internal addon | Private visibility, Unlisted, Admin-only install - no public approval needed (see §6.10.1) |
| 2 | Google OAuth credentials for Classroom API | Use Campus OAuth with incremental scope approval workflow |
| 3 | What happens when teacher deletes assignment on platform? | Warn about connected Classroom assignments, warn about inaccessibility, require confirmation |
| 4 | Should assignments be locked after posting to Classroom? | Yes, for MVP |
| 5 | Should teachers be able to "unlink" from a specific Classroom class? | Use "sync" button to check and unlink deleted Classrooms |
| 6 | Multi-class assignment AddOnAttachment strategy | One Campus Assignment, multiple `classroom_links`, one `AddOnAttachment` per course |
| 7 | What happens if a student is enrolled in the same class multiple times? | Not possible in Google Classroom API |
| 8 | Should assignments be soft-deleted or hard-deleted? | Soft-delete with cascade via student ID (depth-first) |
| 9 | Should we track deletion of Classroom links? | Won't track - not feasible as trigger |
| 10 | What specific telemetry events do we need to track? | Deferred to post-MVP |
| 11 | Do we need auto-grading or is teacher-only grading sufficient? | Teacher grading in Classroom only |
| 12 | Should grades pass back immediately or when teacher opens submission? | Deferred to post-MVP |
| 13 | Do we need to support students retaking assignments? | No, edit until deadline only |
| 14 | Draft vs published assignments? | Google Classroom handles drafts |
| 15 | Multiple classes support? | Yes, required for MVP |
| 16 | Campus API role verification for teachers? | Not available - use Google Classroom `getAddOnContext` API |
| 17 | URL parameter naming? | Use camelCase (`courseId`, `itemId`, etc.) per Google API |
| 18 | Workspace Marketplace SDK installation? | Not a Python package - configure in Google Cloud Console |

### 7.5 Grade Passback Implementation (Post-MVP Reference)

When implementing grade passback, use these API patterns:

**Setting Max Points (when creating AddOnAttachment):**
```python
POST /v1/courses/{courseId}/courseWork/{itemId}/addOnAttachments

{
  "title": "Assignment Title",
  "teacherViewUri": {"uri": "https://..."},
  "studentViewUri": {"uri": "https://..."},
  "studentWorkReviewUri": {"uri": "https://..."},
  "maxPoints": 50
}
```

**Setting Student Grade:**
```python
PATCH /v1/courses/{courseId}/courseWork/{itemId}/addOnAttachments/{attachmentId}/studentSubmissions/{submissionId}

{
  "pointsEarned": 45,
  "assignedGrade": 45
}

UpdateMask: pointsEarned,assignedGrade
```

**Important Notes:**
- 5-30 second delay for grades to appear in Classroom UI after API call
- Only one AddOnAttachment per assignment can have "Grade sync" capability
- Draft grades (not visible to students) vs assigned grades (visible)

### 7.6 AddOnAttachment Structure (Reference)

The `AddOnAttachment` object created via Classroom API:

```json
{
  "id": "string (Classroom-assigned)",
  "courseId": "string",
  "itemId": "string (CourseWork/CourseWorkMaterial/Announcement ID)",
  "title": "string (1-1000 chars, required)",
  "teacherViewUri": {"uri": "https://your-domain/addon/teacher (required)"},
  "studentViewUri": {"uri": "https://your-domain/addon/student (required)"},
  "studentWorkReviewUri": {"uri": "https://your-domain/addon/review (optional)"},
  "maxPoints": "number (for grade passback)",
  "dueDate": {"year": 2025, "month": 1, "day": 15},
  "dueTime": {"hours": 23, "minutes": 59, "seconds": 0}
}
```

**Key points:**
- One `CourseWork` (assignment) can have multiple `AddOnAttachment`s
- Each attachment is **per-CourseWork**, not per-course
- Multi-class posting = multiple `CourseWork` items = multiple `AddOnAttachment`s
- All share the same Campus Assignment ID stored in your platform

---

## 8. Milestones

### Phase 1: MVP - Core Functionality
- [ ] Campus API authentication working
- [ ] Assignment creation (prompt + structured questions)
- [ ] Question hierarchy (parent/child relationships via dot notation)
- [ ] Assignment storage in Campus API (new resource)
- [ ] Student submission (free-text responses)
- [ ] Submission storage in Campus API (new resource)
- [ ] Teacher feedback (plain text per question)
- [ ] Manual testing (no Add-On yet)

### Phase 2: Google Classroom Integration
- [ ] Google Cloud Console project setup
- [ ] OAuth 2.0 integration with Google
- [ ] Register as Classroom Add-On
- [x] Implement Attachment Discovery iframe (superseded API-first §6.7: `/addon/discovery` is a public pointer page to the platform flow, not a picker — issue #13)
- [ ] Implement Teacher View iframe (create/edit assignments)
- [ ] Implement Student View iframe (complete assignments)
- [ ] Implement Student Work Review iframe (feedback)
- [ ] Multi-class selection and assignment creation
- [ ] Store Classroom metadata in Campus Assignment

### Phase 3: Testing & Launch
- [ ] Internal testing with test accounts
- [ ] Pilot with teachers
- [ ] Google Workspace Marketplace submission
- [ ] Launch to production

### Future (Post-MVP)
- [ ] Telemetry collection
- [ ] Grade passback to Classroom
- [ ] Drive shortcuts for submissions
- [ ] Rich response types (equations, diagrams)
- [ ] Annotated feedback
- [ ] Highlighting and annotation tools

---

## 9. Appendix

### 9.1 References
- [Google Classroom Add-Ons Documentation](https://developers.google.com/workspace/classroom/add-ons)
- [iframe and query parameter details](https://developers.google.com/workspace/classroom/add-ons/developer-guides/iframes)
- [Create attachments outside of Google Classroom](https://developers.google.com/workspace/classroom/add-ons/developer-guides/create-attachments-outside-classroom)
- [Classroom API Reference](https://developers.google.com/workspace/classroom/reference/rest)
- [Classroom API Discovery Document (local copy)](./classroom-googleapis-com_discovery_rest_v1.json)
- [Grade Passback Walkthrough](https://developers.google.com/workspace/classroom/add-ons/walkthroughs/grade-passback)
- [Push Notifications in Classroom API](https://developers.google.com/workspace/classroom/best-practices/push-notifications)
- [StudentSubmission Resource](https://developers.google.com/workspace/classroom/reference/rest/v1/courses.courseWork.studentSubmissions)

### 9.2 Glossary
- **Add-On:** A Google Classroom integration that loads third-party content in an iframe
- **API-First Attachment Creation:** Primary workflow where campus-classroom creates CourseWork and AddOnAttachment directly via Classroom API, with eligibility check for CREATE_ADD_ON_ATTACHMENT capability
- **Attachment Discovery iframe:** The iframe shown to teachers when adding content to a Classroom assignment
- **Link Material:** Fallback attachment type for ineligible users - opens campus-classroom content in new tab instead of iframe
- **Link Upgrade:** (Post-MVP) Feature where teachers paste a campus-classroom URL and Classroom offers to upgrade it to a proper add-on attachment
- **Teacher View iframe:** The iframe where teachers create, edit, and preview assignments
- **Student View iframe:** The iframe where students complete assignments
- **Student Work Review iframe:** The iframe where teachers review student submissions and provide feedback
- **Pub/Sub Push Notifications:** Google Cloud messaging service used to receive real-time notifications when Classroom resources change (e.g., student submissions are returned)
- **Feedback Release:** The moment when student can see teacher feedback, triggered by Classroom's `RETURNED` state on StudentSubmission
- **Grade passback:** (Future) The process of sending grades from your platform to Classroom via API
- **Prompt:** Context or passage provided to students before a question
- **Question hierarchy:** Nested question structure using dot notation (q1 → q1.a → q1.a.i)
- **Question ID:** Hierarchical dot notation (q1, q1.a, q1.a.i) that encodes hierarchy in the ID itself
