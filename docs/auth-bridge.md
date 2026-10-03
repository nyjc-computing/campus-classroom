# Google Classroom Auth Bridge (issue #9; broker swap + legacy retirement #30)

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
`/addon/` paths) or a flash + redirect (browser paths). None of them
surface as 500.

---

## 1. How it works (since issue #30, 2026-10-03)

**campus.auth is the sole custodian of Google Classroom credentials** (the
namespaced `google.classroom` integration; design campus#730 §2.4–2.5,
tracker campus#733). This app holds no Google tokens of any kind.

- **Connecting (one-time, per user):** the user clicks Connect on the
  Classroom card of the **campus-profile integrations page**
  (`/profile/integrations`). campus.auth runs the Google consent (forced
  `prompt=consent`), enforces the identity mapping there (consenting
  Google email must equal the campus session user), and stores the
  credential in its vault. This app has no connect UX of its own.
- **Releasing (every request that needs Classroom):**
  `with_classroom_session()` POSTs
  `{campus.auth}/auth/v1/broker/google/classroom/` with the user's **campus**
  bearer token — the one the app already holds from its campus login
  session (SDK `auth.get_token()`, refreshed when expired via the
  refresh-token grant). The response carries `access_token`, `expires_in`
  and `scope`; **no refresh token ever leaves campus.auth**.
- **Token lifetime:** in-memory only, until `expires_in` (60 s skew), then
  the app simply asks the broker again — campus refreshes its stored
  credential silently server-side. A Classroom 401 triggers one
  re-release + retry. Nothing is persisted anywhere.
- **Scope gating:** the broker is asked only for the conservative floor
  (`_BROKER_ASKABLE` = MVP + identity scopes; asking beyond a deployment's
  vault SCOPES cap is a 400 `AUTH_INVALID_SCOPE`, i.e. a configuration
  bug). Caller requirements beyond the floor (the send flow's
  `classroom.coursework.students`) are enforced **locally** against the
  returned grant. On dev the vault grant is 11 scopes — everything this
  app needs, verified live 2026-10-03.

## 2. Error mapping (preserved semantics for callers)

| Broker / seam condition | Raised as | JSON code | Browser path |
|---|---|---|---|
| 404 no connected credential | `NotConnectedError` | `classroom_not_connected` (403) | flash + connection page (profile CTA) |
| 403 + `details.missing_scopes` | `MissingClassroomScopesError` | `classroom_missing_scopes` (403) + `missing_scopes` + `reconnect_url` | flash + connection page |
| 401 (campus bearer unusable) | `CampusSessionExpiredError` | `campus_session_expired` (401) | redirect into campus re-login flow |
| 400 `AUTH_INVALID_SCOPE` / bridge-guard 403 | `BrokerConfigError` (+ ERROR log) | `classroom_broker_config` (502) | flash + connection page |
| broker unreachable / malformed | `OAuthFlowError` | `classroom_oauth_flow_error` (502) | flash + connection page |

Remediation for connect/reconnect is always the campus-profile
integrations page (`reconnect_url` in JSON; CTA button in the UI).

## 3. Scope inventory

Requested via the broker (`CLASSROOM_SCOPES_MVP`, PRD §6.4):

| Scope | For |
|---|---|
| `classroom.courses.readonly` | `courses.list()` — course pickers (#10), `/classroom` live check. Not in PRD §6.4's table, but its post-MVP "classroom.courses" entry is the read-write scope; the readonly one is mandatory for any listing call |
| `classroom.addons.teacher` | Teacher iframe views, attachment creation (#10, #13) |
| `classroom.addons.student` | Student iframe views (#14) |
| `classroom.course-work.readonly` | Read assignment metadata (PRD's "classroom.coursework.readonly" — the unhyphenated name doesn't exist; found at first real consent) |
| `classroom.student-submissions.me.readonly` | Student: own submissions (#14) |
| `classroom.student-submissions.students.readonly` | Teacher: review submissions (#15). PRD's "classroom.coursework.students.readonly" is a noncanonical alias of this — request the canonical name only |
| `classroom.rosters.readonly` | Teacher roster verification |

Feature scope, demanded locally by the send flow
(`CLASSROOM_SCOPES_SEND`):

| Scope | For |
|---|---|
| `classroom.coursework.students` | `courseWork.create`/`.patch` — Send-to-Classroom drafts + Link Material (#10). In the dev vault grant (verified 2026-10-03), so the send flow works through the broker |

Scope names come from the GCP **Data access** list (canonical), not the
PRD table — Google rejects unknown/noncanonical scope strings outright.

**Deliberately NOT requested** (post-MVP, per issue #9):
`classroom.courses` (course-level management / grade passback — the
*readonly* variant IS requested),
`drive.readonly` (Drive shortcuts),
`classroom.push-notifications` (feedback release — #16 adds it).

## 4. Routes

| Route | Purpose |
|---|---|
| `GET /classroom` | Connection status (broker-derived: Connected / Not connected / error states, granted scopes, live `courses.list()` check) |

The legacy in-app OAuth flow (`GET /classroom/authorize`,
`GET /classroom/callback`, `POST /classroom/disconnect`, Flask-session
token storage, app-owned `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`) was
**removed on 2026-10-03** once the broker path was proven live (issue #30
Retire lane).

## 5. Deployment checklist

1. **Campus side** (owned by campus.auth, not this repo): the
   `google.classroom` vault label seeded (CLIENT_ID/SECRET/SCOPES/
   CONNECT_TARGETS); this app's campus client flagged `token_bridge=true`
   with a non-empty `upstream_scopes["google.classroom"]` allowlist.
2. **Env for this app**: the usual `SECRET_KEY`/`CLIENT_ID`/`CLIENT_SECRET`/
   `ENV`/`PUBLIC_URL` — **no Google vars**. Optional
   `CAMPUS_PROFILE_URL` (ENV-derived otherwise) for the reconnect links.
3. Smoke test: sign in → connect via the campus-profile integrations page
   → `/classroom` shows Connected + live courses.

## 6. Tests

`scripts/verify_issue_30.py` runs the seam against a local stub of
campus.auth's broker + the Classroom API (no network, no real account):
bearer/min_scopes on the wire, nothing-persisted, campus-credential
refresh, proactive + 401-triggered re-release, the full error mapping,
the send-scope local gate, and the `/classroom` page states.
`scripts/verify_issue_10.py` covers the send flow on the same harness.
(The pre-broker `verify_issue_9.py` was deleted with the legacy flow it
verified; its history lives in issue #9 / PR #17/#20.)
