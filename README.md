# Campus Classroom

An assignment platform that integrates with Google Classroom as an Add-On.

## Status

Early development. Login page implemented; assignment creation and Classroom integration coming soon.

## Setup

1. **Install dependencies:**
   ```bash
   poetry install
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your Campus API credentials
   ```
   `PUBLIC_URL` is required for login: it is the canonical origin of this
   service and determines the OAuth callback (`{PUBLIC_URL}/finalize_login`),
   which must be registered on the campus client before login works (see
   "Redirect URI Registration Contract" in AGENTS.md). Locally use
   `PUBLIC_URL=http://localhost:5000`.

3. **Run the development server:**
   ```bash
   poetry run python main.py
   ```

4. **Visit:** http://localhost:5000

## Audit tracing (optional)

Set `AUDIT_TRACING_ENABLED=1` and `AUDIT_API_KEY=<producer key>` to make
classroom a campus audit trace producer (campus#816): classroom requests
are recorded as spans and campus SDK calls made while handling a request
land as child spans, so the audit waterfall shows which classroom route
fired each campus call. Key minting and the producer runbook live in the
campus repo's `docs/audit-tracing.md`. Tracing is fail-safe — without a
valid key only spans are lost, never requests.

## Project Structure

```
campus-classroom/
├── apps/classroom/
│   ├── __init__.py         # Flask app factory
│   ├── static/
│   │   └── css/style.css   # Custom styles
│   └── templates/          # Jinja2 templates
├── main.py                 # Entry point
├── pyproject.toml          # Dependencies
└── docs/                   # PRD and design docs
```

## Documentation

- [PRD Draft](docs/PRD-draft.md) - Product Requirements
- [Schema Proposal](docs/schema-proposal.md) - Database schema
- [Feedback Taxonomy](docs/feedback-taxonomy.md) - Feedback design framework
