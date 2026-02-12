# AGENTS.md - Campus Architecture Reference

This document provides essential context for agents working on the `campus-classroom` project and other Campus applications.

## Project Overview

**campus-classroom** is a Google Classroom Add-On for interactive assignments with telemetry. It integrates with:
- **Campus API** (`d:\nyjc-computing\campus`) - Authentication and data services
- **campus-api-python** (`d:\nyjc-computing\campus-api-python`) - Python API client
- **Google Classroom API** - Add-on integration and grade passback

---

## Campus Ecosystem Architecture

### 1. Campus Framework (`d:\nyjc-computing\campus`)

The Campus framework is a **modular monolith** built on Flask with these key modules:

```
campus/
├── auth/           # OAuth authentication services
├── api/            # RESTful API endpoints
├── common/         # Shared utilities, schemas, validation
├── model/          # Entity dataclasses (NO business logic)
├── storage/        # PostgreSQL, MongoDB persistence
├── flask_campus/   # Flask integration utilities
└── yapper/         # Logging/event framework
```

**Key Design Principles:**
- Separation of concerns (routes → resources → storage)
- No circular imports (hierarchy: auth/api → model → storage → common)
- Environment-based configuration (development/staging/production)

### 2. flask_campus Helper

Located in `campus/flask_campus/`, provides:

```python
from campus import flask_campus

# OAuth Login Manager
login_manager = flask_campus.OAuthLoginManager(
    campus_client=campus,
    default_endpoint="index"
)
login_manager.init_app(app)

# Route protection
@app.get("/dashboard")
@login_manager.login_required
def dashboard(**_):
    return flask.render_template("dashboard.html")

# Request parameter unpacking (auto-extracts from URL params or JSON body)
@app.get("/circles")
@flask_campus.unpack_request
def list_circles(tag: str | None = None):
    # 'tag' automatically extracted from request
    result = resources.circle.list(**{"tag": tag} if tag else {})
    return {"data": [circle.to_resource() for circle in result]}
```

### 3. campus-api-python Client

Located in `d:\nyjc-computing\campus-api-python`, provides the Python interface to Campus services.

```python
from campus_python import Campus

# Initialize (reads CLIENT_ID, CLIENT_SECRET from env)
campus = Campus(timeout=60)

# User-scoped operations (with Flask session)
with campus.with_user_session() as client:
    user_info = client.auth.users.get_me()
    circles = client.api.circles.list()

# App-scoped operations (client credentials)
with campus.with_app_session() as client:
    all_users = client.auth.users.list()

# Direct service access
campus.auth.clients  # OAuth client management
campus.auth.users    # User management
campus.auth.logins   # Login sessions
campus.api.circles   # Circles API
```

**Environment Variables Required:**
```bash
CLIENT_ID="your_client_id"
CLIENT_SECRET="your_client_secret"
ENV="development"  # or "staging" or "production"
```

**Environment URLs:**
| Environment | Auth URL | API URL |
|-------------|----------|---------|
| development | https://campusauth-development.up.railway.app | https://campusapi-development.up.railway.app |
| staging | https://auth.campus.nyjc.dev | https://api.campus.nyjc.dev |
| production | https://auth.campus.nyjc.app | https://api.campus.nyjc.app |

---

## Reference Implementation: campus-admin

Located in `d:\nyjc-computing\campus-admin`, this is the canonical example for building Campus apps.

### Project Structure
```
campus-admin/
├── apps/admin/
│   ├── __init__.py      # App factory with all routes
│   ├── static/          # CSS, JS assets
│   └── templates/       # Jinja2 templates
└── main.py              # Entry point
```

### App Factory Pattern
```python
# apps/admin/__init__.py
from campus_python import campus_python
from campus import flask_campus

def create_app():
    app = flask.Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")

    # Initialize campus client
    campus = campus_python.Campus(timeout=60)

    # Setup OAuth
    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus,
        default_endpoint="index"
    )
    login_manager.init_app(app)

    # Register routes...
    return app
```

### Route Patterns
```python
# Login flow
@app.get("/login")
def login():
    return flask.redirect(login_manager.authorization_url())

@app.get("/finalize_login")
def finalize_login():
    return login_manager.finalize_login()

# Protected routes
@app.get("/dashboard")
@login_manager.login_required
def dashboard(**_):
    return flask.render_template("dashboard.html")

# Admin-only routes
@app.get("/clients")
@login_manager.login_required
@admin_required
def clients(**_):
    with campus.with_user_session() as client:
        data = client.auth.clients.list()
    return flask.render_template("clients.html", clients=data)
```

### Frontend Patterns
- Bootstrap 5 for responsive UI
- Jinja2 templates with base layout
- Flash messages for notifications
- Modal dialogs for forms

---

## Building a New Campus App

### Step 1: Project Structure
```
your-app/
├── apps/
│   └── main/
│       ├── __init__.py      # App factory
│       ├── static/          # CSS, JS
│       └── templates/       # Jinja2 templates
├── .env                     # Environment variables
├── .env.example             # Template
├── main.py                  # Entry point
└── pyproject.toml           # Dependencies
```

### Step 2: Required Dependencies
```toml
[tool.poetry.dependencies]
python = "^3.11"
flask = "^3.0"
python-dotenv = "^1.0"
campus-python = { path = "../campus-api-python" }
```

### Step 3: Environment Variables
```bash
# Required
CLIENT_ID="your_client_id"
CLIENT_SECRET="your_client_secret"
SECRET_KEY="flask_session_secret"
ENV="development"

# Optional
HOSTNAME="localhost"
PORT="5000"
```

### Step 4: Main Entry Point
```python
# main.py
from apps.main import create_app

app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
```

### Step 5: App Factory
```python
# apps/main/__init__.py
import os
import flask
from campus_python import campus_python
from campus import flask_campus

def create_app():
    app = flask.Flask(__name__)

    # Required config
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
    if not app.config["SECRET_KEY"]:
        raise ValueError("SECRET_KEY environment variable is required")

    # Initialize campus client
    campus = campus_python.Campus(timeout=60)

    # Setup OAuth login manager
    login_manager = flask_campus.OAuthLoginManager(
        campus_client=campus,
        default_endpoint="index"
    )
    login_manager.init_app(app)

    # Make campus available globally
    app.campus = campus
    app.login_manager = login_manager

    # Register routes
    register_routes(app)

    return app

def register_routes(app):
    @app.get("/")
    def index():
        return flask.render_template("index.html")

    @app.get("/login")
    def login():
        return flask.redirect(app.login_manager.authorization_url())

    @app.get("/finalize_login")
    def finalize_login():
        return app.login_manager.finalize_login()

    @app.get("/dashboard")
    @app.login_manager.login_required
    def dashboard(**_):
        return flask.render_template("dashboard.html")
```

---

## campus-classroom Specific Considerations

### Storage Architecture

**IMPORTANT:** `campus-classroom` does **NOT** have its own database. All data storage is handled by Campus API via the `campus-python` client.

```
┌─────────────────────────────────────────────────────────────────────┐
│                   campus-classroom Flask App                    │
│                                                              │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  Routes (apps/classroom/routes/)                    │    │
│  │  - assignments.py  (uses campus.api.v1.assignments) │    │
│  │  - submissions.py  (uses campus.api.v1.submissions) │    │
│  └────────────────────────────────────────────────────────────┘    │
│                           │                                   │
│                           ▼                                   │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  campus-python Client (campus_python.Campus)          │    │
│  │  - with_user_session() for user-scoped operations      │    │
│  └────────────────────────────────────────────────────────────┘    │
│                           │                                   │
│                           ▼                                   │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  Campus API (campus/api/resources/)                   │    │
│  │  - assignments.py (AssignmentsResource)                │    │
│  │  - submissions.py (SubmissionsResource)                │    │
│  └────────────────────────────────────────────────────────────┘    │
│                           │                                   │
│                           ▼                                   │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  Storage Layer (campus/storage/)                      │    │
│  │  - MongoDB collections (assignments, submissions)        │    │
│  └────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

**What this means for implementation:**
- **DO NOT** create local database tables, migrations, or storage code
- **DO** use `campus.with_user_session(user_id) as client:` context for all data operations
- **DO** access resources via `client.api.v1.assignments` and `client.api.v1.submissions`
- **Models** are imported from `campus.model` (Assignment, Submission, Question, Response, Feedback, ClassroomLink)

**Models are defined in:** `d:\nyjc-computing\campus\campus\model\`
- `assignment.py` - Assignment, Question, ClassroomLink
- `submission.py` - Submission, Response, Feedback

**API Resources are defined in:** `d:\nyjc-computing\campus\campus\api\resources\`
- `assignment.py` - AssignmentsResource, AssignmentResource
- `submission.py` - SubmissionsResource, SubmissionResource

**Client is defined in:** `d:\nyjc-computing\campus-api-python\campus_python\api\v1\`
- `assignments.py` - Assignments, Assignment, Assignment.Links
- `submissions.py` - Submissions, Submission, Submission.Responses, Submission.Feedback

### What DOESN'T need local storage

| Data | Stored In | Accessed Via |
|-------|-------------|--------------|
| Assignments (questions, title, description) | Campus API (MongoDB) | `client.api.v1.assignments` |
| Submissions (responses, feedback) | Campus API (MongoDB) | `client.api.v1.submissions` |
| User authentication | Campus API (PostgreSQL) | `login_manager` (flask_campus) |
| Session state | Flask session | `flask.session` |

### What MAY need local storage (future consideration)

| Feature | Storage Option | Notes |
|----------|----------------|-------|
| Google OAuth tokens for Classroom API | Flask session or Campus tokens | Use incremental scope approval via Campus API |
| Pub/Sub registration state | Campus API or local | For feedback release notifications |
| Temporary draft state | Flask session or local | For auto-save before assignment creation |

### Google Classroom Add-On Requirements

### Google Classroom Add-On Requirements

1. **Iframe Views** (must work within Classroom iframe):
   - Attachment Discovery (teacher assignment creation)
   - Student View (student completes assignment)
   - Student Work Review (teacher reviews submissions)

2. **URL Parameters** (provided by Classroom):
   - `courseId` - The Classroom course ID
   - `courseWorkId` - The assignment ID
   - `attachmentId` - The attachment ID
   - `addOnToken` - Authorization token

3. **postMessage API** (communicate with Classroom):
   ```javascript
   // Enable grade passback button
   window.parent.postMessage({
     action: 'gradePassback',
     gradingId: gradingId
   }, '*');

   // Notify Classroom of completion
   window.parent.postMessage({
     action: 'workCompletion'
   }, '*');
   ```

4. **Grade Passback** (optional):
   - Call Classroom API with teacher's credentials
   - Submit `pointsEarned` for student submissions
   - Grades appear as draft grades (teacher editable)

### Authentication Strategy

**Challenge:** Google Classroom uses one Google account, but Campus uses OAuth.

**Solution:**
1. User logs into Classroom with Google account
2. Campus OAuth stores `google_user_id` mapping
3. Platform identifies user via Campus token + validates Google email matches

### Data Model Considerations

```python
# From PRD - relationships to consider
Assignments
├── classroom_syncs (one-to-many)
└── submissions (one-to-many)

Submissions
├── telemetry_events (one-to-many)
└── drive_link (one-to-one)
```

---

## Common Patterns

### Error Handling
```python
from campus_python.errors import AuthenticationError, AccessDeniedError

@app.get("/protected")
@login_manager.login_required
def protected(**_):
    try:
        with app.campus.with_user_session() as client:
            data = client.api.something.list()
        return {"data": data}
    except AuthenticationError:
        flask.flash("Authentication required", "error")
        return flask.redirect("/login")
    except AccessDeniedError:
        flask.abort(403)
```

### Admin Role Check
```python
def admin_required(f):
    @wraps(f)
    def decorated_function(**kwargs):
        user_id = flask.session.get("user_id")
        # Check if user is admin via campus API
        with app.campus.with_user_session() as client:
            user = client.auth.users.get(user_id)
            if not user.get("is_admin"):
                flask.abort(403)
        return f(**kwargs)
    return decorated_function
```

### Flash Messages
```python
# In routes
flask.flash("Assignment created successfully", "success")
flask.flash("Error saving assignment", "error")

# In templates (Bootstrap)
{% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    {% for category, message in messages %}
      <div class="alert alert-{{ category }}">{{ message }}</div>
    {% endfor %}
  {% endif %}
{% endwith %}
```

---

## Development Workflow

1. **Set environment:** Copy `.env.example` to `.env` and configure
2. **Install dependencies:** `poetry install`
3. **Run locally:** `python main.py` or `flask run`
4. **Test OAuth:** Use `scripts/test_auth.py` from campus-admin as reference
5. **Deploy:** Configure `ENV=staging` or `ENV=production`

---

## Related Codebases

| Path | Purpose |
|------|---------|
| `d:\nyjc-computing\campus` | Campus framework and API |
| `d:\nyjc-computing\campus-api-python` | Python API client |
| `d:\nyjc-computing\campus-admin` | Reference Flask app implementation |
| `d:\nyjc-computing\campus-classroom` | This project |

---

## Key Files to Reference

| File | Purpose |
|------|---------|
| `campus/flask_campus/__init__.py` | OAuthLoginManager, decorators |
| `campus/apps/auth/routes/authorize.py` | OAuth flow implementation |
| `campus-admin/apps/admin/__init__.py` | App factory example |
| `campus-api-python/campus_python/__init__.py` | Client initialization |
| `campus-classroom/docs/PRD-draft.md` | Product requirements |
