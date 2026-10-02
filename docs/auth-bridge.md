# Google Classroom Auth Bridge (issue #9)

How a Campus-authenticated user gets a Google credential that can call the
Classroom API, and how the other add-on sessions (#10–#16) use it.

**TL;DR for other sessions**

```python
from apps.classroom import classroom_auth  # apps/classroom/classroom_auth.py

@app.post("/api/whatever")
@login_manager.login_required
def whatever(**_):
    try:
        with classroom_auth.with_classroom_session() as classroom:
            courses = classroom.courses_list()
            # classroom.request("GET", "v1/courses/<id>/courseWork") etc.
    except classroom_auth.NotConnectedError:
        ...            # app-wide handler already covers this: clean 403/redirect
```

Raising is enough — an app-wide errorhandler converts every
`ClassroomAuthError` into either a machine-readable JSON 4xx (API and
`/addon/` paths, including an `authorize_url` for the missing scopes) or a
flash + redirect (browser paths). None of them surface as 500.

---

## 1. Why this design (and not what the PRD assumed)

PRD §7.1 RQ2 resolved "do we need separate Google OAuth credentials?" with
"no — extend Campus tokens with Classroom scopes incrementally". That
presumes Campus capabilities that **do not exist today** (verified against
`campus@main`, 2026-10-01):

| PRD assumption | Campus reality |
|---|---|
| Campus tokens can carry Classroom scopes | The Google proxy (`campus/auth/oauth_proxy/google/proxy.py`) hardcodes `scopes = ["email", "profile"]`; no Classroom scope is ever requested from Google |
| Refreshing a Campus token yields Google credentials | Campus-issued tokens are opaque Campus-API-only bearers; the upstream Google tokens Campus stores are internal to the Google proxy and there is no endpoint that re-issues or exchanges them for downstream apps |
| Scope consent config on the Campus OAuth client | `/auth/v1/authorize` ignores the `scope` parameter entirely (TODO in code); actual scopes come from `POST /auth/v1/sessions/campus/` and are granted verbatim; a Campus scope string is meaningless to Google anyway |
| Store third-party Google tokens via Campus credentials API | `POST /auth/v1/credentials/<provider>/<user_id>` (`UserCredentialsResource.new()`) asserts `provider == "campus"` — third-party provider rows cannot be created through the API |

So the MVP implementation uses the **app's own Google OAuth client**
(`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, PRD §6.10.2 step 3 — the same
GCP project the add-on manifest in #11 needs) and Google's **native
incremental authorization** to stand in for the Campus-side flow:

- The user is authenticated by Campus first (identity anchor, no second
  login to Campus).
- On first Classroom action, `/classroom/authorize` sends them to Google
  with `login_hint=<campus email>` and `hd=<workspace domain>`, so the
  right account is pre-selected and pinned.
- When new scopes are needed later (e.g. `classroom.push-notifications`
  for #16), the app requests **only the missing scopes** with
  `include_granted_scopes=true` — Google merges them into the existing
  grant. That is the "incremental scope approval" acceptance criterion.
- `prompt=consent` + `access_type=offline` guarantee a refresh token on
  every consent round.

If Campus later ships a Classroom-scope token bridge (tracked upstream),
`with_classroom_session()` is the single seam to swap — callers don't
change.

## 2. Identity mapping (Google ↔ Campus)

Campus provisions users from Google Workspace userinfo with
`user_id = email` and restricts logins to `WORKSPACE_DOMAIN`. The bridge
therefore keys identity on **email equality**:

1. Google consent is pre-pinned (`login_hint`, `hd`) to the Campus email.
2. At the callback, the app fetches userinfo and **refuses** unless
   `email_verified` and Google email == Campus user email
   (`IdentityMismatchError` → loud 403 page, logged at ERROR, **nothing
   stored**).
3. The state parameter (CSRF) is validated before any exchange; mismatch →
   loud 400, nothing exchanged.

## 3. Token persistence + refresh (the decision)

**Decision: Flask session (signed cookie).** Alternatives considered:

- *Campus credentials store* — the right long-term home, but the API
  refuses third-party provider rows (see table above); would require a
  campus-repo change. Revisit when that ships.
- *Server-side session store* — needs a new local store, violating the
  epic's "no local DB; sessions in Flask session" rule.

Properties of the chosen approach: the cookie is signed (tamper-proof) and
HttpOnly, but **not encrypted** — the token is readable by its own user
(acceptable: it's their own Google grant) and exposed to any XSS in the
app. The stored dict holds `access_token`, `refresh_token`, `expires_at`,
`scopes`, `email`.

**Refresh strategy:** `ClassroomClient` refreshes proactively when the
access token is within 60s of expiry and reactively once on a 401; the
refreshed values are written back to the Flask session on context exit. A
refresh failure raises cleanly ("connection expired — reconnect").

## 4. Scope inventory

Requested at connect (`CLASSROOM_SCOPES_MVP`, PRD §6.4):

| Scope | For |
|---|---|
| `classroom.courses.readonly` | `courses.list()` — course pickers (#10), `/classroom` live check. Not in PRD §6.4's table, but its post-MVP "classroom.courses" entry is the read-write scope; the readonly one is mandatory for any listing call |
| `classroom.addons.teacher` | Teacher iframe views, attachment creation (#10, #13) |
| `classroom.addons.student` | Student iframe views (#14) |
| `classroom.coursework.readonly` | Read assignment metadata |
| `classroom.coursework.students.readonly` | Teacher: view coursework/grades (#15) |
| `classroom.student-submissions.me.readonly` | Student: own submissions (#14) |
| `classroom.student-submissions.students.readonly` | Teacher: review submissions (#15) |
| `classroom.rosters.readonly` | Teacher roster verification |

Plus `userinfo.email` / `userinfo.profile` for the identity match.

**Deliberately NOT requested** (post-MVP, per issue #9):
`classroom.courses` (grade passback — the *readonly* variant IS requested),
`drive.readonly` (Drive shortcuts),
`classroom.push-notifications` and `classroom.coursework.students`
(feedback release — #16 adds them via the incremental path).

## 5. Routes added

| Route | Purpose |
|---|---|
| `GET /classroom` | Connection status; granted-vs-MVP scopes; live `courses.list()` check |
| `GET /classroom/authorize` | Start consent; `?next=` and optional `?scopes=` (subset for incremental grants) |
| `GET /classroom/callback` | OAuth callback: state check → code exchange → email match → store |
| `POST /classroom/disconnect` | Forget the stored Google credential |

## 6. Deployment checklist (human-in-the-loop)

1. **Google Cloud Console** (app's project, `GOOGLE_CLOUD_PROJECT_ID`):
   - Enable the **Google Classroom API**.
   - OAuth consent screen: Internal (Workspace) is sufficient for a
     private deployment; add the MVP scopes above. While the app is in
     *Testing* status, Google shows an unverified-app warning unless the
     user is a test user — Marketplace/Marketplace-sdk registration
     (#11) is what makes this a properly installed private app.
   - On the OAuth client (`GOOGLE_CLIENT_ID`), register the redirect URI
     **exactly**: `{PUBLIC_URL}/classroom/callback` — locally
     `http://localhost:5000/classroom/callback`, on Railway
     `https://campus-classroom-development.up.railway.app/classroom/callback`.
2. **Env**: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `WORKSPACE_DOMAIN` (see `.env.example`). `PUBLIC_URL` already drives
   the callback URL via `campus.common.utils.url.full_url_for`.
3. Smoke test: sign in → `/classroom` → Connect → consent → courses list.

## 7. Tests

`scripts/verify_issue_9.py` runs the whole bridge against a local stub of
Google's OAuth + Classroom endpoints (no network, no real account):
authorize-URL composition, happy-path callback + `courses.list()`, CSRF
refusal, email-mismatch refusal, cancelled consent, missing-scope UX +
incremental re-consent URL + JSON 403 shape, proactive/401-triggered token
refresh, unconfigured-GOOGLE_* errors, disconnect. 50 checks.
