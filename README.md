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

3. **Run the development server:**
   ```bash
   poetry run python main.py
   ```

4. **Visit:** http://localhost:5000

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
