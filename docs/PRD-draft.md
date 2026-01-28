# PRD: Classroom Assignment Platform with Telemetry

**Status:** Draft
**Version:** 0.1
**Date:** 2025-01-28

---

## 1. Overview

### 1.1 Purpose
Develop an assignment platform that integrates with Google Classroom as an Add-On. Teachers create interactive assignments on the platform; students complete them within the Classroom iframe. The platform collects telemetry data on student engagement. Grades are managed in Google Classroom.

### 1.2 Goals
- Seamless in-Classroom experience via Add-On iframe
- Rich telemetry collection on student behavior
- Grading handled within Google Classroom
- Students retain a link to their work in Google Drive

### 1.3 Non-Goals
- Grading dashboard on the platform (grading in Classroom only)
- Offline access to assignments
- Native Google Drive file types

---

## 2. User Personas

### 2.1 Teacher
- Creates assignments on the platform
- Pushes assignments to one or more Google Classroom classes
- Views student engagement telemetry
- Grades students within Google Classroom

### 2.2 Student
- Opens and completes assignments within Google Classroom iframe
- Uses Campus API login (same Google account as Classroom)
- Has a Drive link to access their work later

### 2.3 Admin
- [Define if needed]

---

## 3. User Stories

### 3.1 Teacher Stories
- [ ] As a teacher, I want to create an assignment on the platform
- [ ] As a teacher, I want to push an assignment to one or more Google Classroom classes
- [ ] As a teacher, I want to create assignments as drafts or published
- [ ] As a teacher, I want to view telemetry on how students engaged with assignments
- [ ] As a teacher, I want to grade students within Google Classroom

### 3.2 Student Stories
- [ ] As a student, I want to open and complete assignments without leaving Classroom
- [ ] As a student, I want to log in using my Campus account
- [ ] As a student, I want to have a link to my completed work in Google Drive

### 3.3 System Stories
- [ ] As the system, I want to collect telemetry on student interactions
- [ ] As the system, I want to send auto-grades to Google Classroom (optional)

---

## 4. Functional Requirements

### 4.1 Authentication
- [ ] **AUTH-1:** Platform must integrate with existing Campus API for authentication
- [ ] **AUTH-2:** Users log in with Google account (same as Classroom account)
- [ ] **AUTH-3:** Platform must identify user's Google identity for Classroom mapping

### 4.2 Assignment Creation
- [ ] **AC-1:** Teachers can create assignments with title, description, content
- [ ] **AC-2:** Teachers can set max points for the assignment
- [ ] **AC-3:** Teachers can select/deselect which Google Classroom classes to sync to
- [ ] **AC-4:** Assignments can be created as draft or published

### 4.3 Google Classroom Integration (Add-On)
- [ ] **GC-1:** Platform must register as a Google Classroom Add-On
- [ ] **GC-2:** Attachment Discovery iframe for content selection during assignment creation
- [ ] **GC-3:** Student View iframe for completing assignments
- [ ] **GC-4:** Student Work Review iframe for teacher to view submissions
- [ ] **GC-5:** Platform receives Classroom metadata via URL params (course_id, coursework_id, etc.)
- [ ] **GC-6:** Platform can set grades via Classroom API (pointsEarned)
- [ ] **GC-7:** Platform can create Drive shortcuts/links for student submissions

### 4.4 Telemetry Collection
- [ ] **TEL-1:** Track when student opens assignment
- [ ] **TEL-2:** Track time spent on assignment
- [ ] **TEL-3:** Track student interactions (clicks, navigation)
- [ ] **TEL-4:** Track student responses/answers
- [ ] **TEL-5:** Track when student submits
- [ ] **TEL-6:** Link telemetry to assignment, student, and course identifiers

### 4.5 Grade Passback (Optional)
- [ ] **GP-1:** Platform can calculate auto-grades based on answers
- [ ] **GP-2:** Platform sends grades to Classroom via API
- [ ] **GP-3:** Grades appear as draft grades in Classroom (teacher can edit)

### 4.6 Student Artifacts
- [ ] **SA-1:** Platform creates a Drive shortcut/link for each submission
- [ ] **SA-2:** Link points back to student's work on the platform

---

## 5. Non-Functional Requirements

### 5.1 Performance
- [ ] Telemetry events should not block student UI
- [ ] Grade passback should complete within 30 seconds

### 5.2 Security
- [ ] All API calls must use OAuth 2.0 with appropriate scopes
- [ ] Teacher credentials must be stored securely for offline grade passback
- [ ] Student data must be isolated by course/assignment

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

### 6.2 APIs Used
- **Campus API:** Authentication (existing)
- **Classroom API:** Assignment creation, grade passback
- **Drive API:** Create shortcuts/links for student submissions

### 6.3 Data Model (Draft)
```
Users
├── id (Campus user ID)
├── google_user_id (for Classroom mapping)
├── email
└── role (teacher/student)

Assignments
├── id (platform ID)
├── title
├── description
├── content (activity data)
├── max_points
├── created_by (teacher ID)
└── classroom_syncs (one-to-many)

ClassroomSyncs
├── assignment_id
├── course_id (Classroom course)
├── coursework_id (Classroom assignment)
├── attachment_id (Classroom attachment)
└── created_at

Submissions
├── id
├── assignment_id
├── student_id
├── course_id
├── coursework_id
├── submission_id (Classroom)
├── responses (JSON)
├── submitted_at
└── drive_link_id

TelemetryEvents
├── id
├── submission_id
├── event_type
├── timestamp
└── data (JSON)
```

---

## 7. Open Questions

| # | Question | Owner | Status |
|---|----------|-------|--------|
| 1 | What specific telemetry events do we need to track? | | |
| 2 | Do we need auto-grading or is teacher-only grading sufficient? | | |
| 3 | Should grades pass back immediately or when teacher opens submission? | | |
| 4 | What happens when teacher deletes assignment on platform? | | |
| 5 | Do we need to support students retaking assignments? | | |
| 6 | What is the approval process for becoming a Classroom Add-On? | | |
| 7 | Can we use existing Campus OAuth tokens for Classroom API calls? | | |

---

## 8. Milestones

### Phase 1: Proof of Concept
- [ ] Campus API login working
- [ ] Basic assignment creation/viewing
- [ ] Telemetry collection
- [ ] Manual link to Classroom (no Add-On yet)

### Phase 2: Add-On Development
- [ ] Register as Classroom Add-On
- [ ] Implement iframe views (Discovery, Student, Review)
- [ ] API integration for assignment creation
- [ ] API integration for grade passback

### Phase 3: Drive Integration
- [ ] Create Drive shortcuts for submissions
- [ ] Test student access to work via Drive

### Phase 4: Testing & Launch
- [ ] Pilot with teachers
- [ ] Google marketplace approval
- [ ] Launch

---

## 9. Appendix

### 9.1 References
- [Google Classroom Add-Ons Documentation](https://developers.google.com/workspace/classroom/add-ons)
- [Classroom API Reference](https://developers.google.com/workspace/classroom/reference/rest)
- [Grade Passback Walkthrough](https://developers.google.com/workspace/classroom/add-ons/walkthroughs/grade-passback)

### 9.2 Glossary
- **Add-On:** A Google Classroom integration that loads third-party content in an iframe
- **Attachment Discovery iframe:** The iframe shown to teachers when adding your content to an assignment
- **Student View iframe:** The iframe where students complete your activity
- **Student Work Review iframe:** The iframe where teachers review student submissions
- **Grade passback:** The process of sending grades from your platform to Classroom via API
