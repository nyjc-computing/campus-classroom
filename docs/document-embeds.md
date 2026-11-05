# Document-type assignments (#78)

A **document question** embeds a single-file HTML5 app into the `/a/`
answer sheet. The student works inside the embedded app; at save/submit
the app's live DOM is exported as a **script-free HTML snapshot**, and
that snapshot *is* the answer (`response_data: {html}`). The teacher
grades by viewing the snapshot inertly. Design + security model:
[issue #78](https://github.com/nyjc-computing/campus-classroom/issues/78).

## Roles of the two deployments

| | Main deployment | Docs deployment |
|---|---|---|
| Serves | everything except `/embed` | `/embed/<assignment>/<question>?t=<token>` (everything else unused) |
| Env | `EMBED_ORIGIN=<docs origin>` | `EMBED_MODE=1`, `EMBED_PARENT_ORIGIN=<main origin>`, same `EMBED_TOKEN_SECRET` |
| Trust | never serves executable teacher HTML | sandboxed frames (`allow-scripts allow-same-origin`, safe because origin ≠ main), CSP `frame-ancestors <main>` |

`EMBED_TOKEN_SECRET` (falls back to `SECRET_KEY`) is the HMAC key both
deployments share: the main app mints embed tokens and export nonces,
the docs deployment verifies tokens. Set a dedicated value when the
deployments are separate services.

## Flow

1. Teacher authors a `document` question (type picker → upload/paste
   `.html`, ≤ 2 MB); campus validates `data.html` against the registry
   schema (campus#533 entry, campus PR #1061).
2. `/a/{id}` renders document questions as sandboxed iframes pointing
   at tokened `/embed` URLs (doc HTML is never inlined into the page).
3. At save/submit the page mints an **export challenge nonce**
   (`POST /api/v1/assignments/{id}/export-challenge`, enrollment-gated),
   asks the frame to serialize via postMessage, and PATCHes
   `{question_id, question_type: "document", response_data: {html}}`.
4. The submissions routes enforce snapshots before forwarding to campus:
   fresh nonce stamped as `meta[name=campus-export-nonce]`, size cap,
   and a scrub of `<script>`/`on*`/`javascript:` URLs. Snapshots are
   attestations, not verified evidence (a client can fabricate them, as
   it can `response_text` — accepted, out of scope).
5. Review renders the snapshot in a fully script-disabled sandboxed
   iframe (`sandbox=""`), on demand, per question.

## Railway ops (activating on dev)

1. Duplicate the `campus-classroom` service as e.g.
   `campus-classroom-docs` in the campus project, development
   environment (same repo, connected to `main`, same build).
2. On the docs service set variables: `EMBED_MODE=1`,
   `EMBED_PARENT_ORIGIN=https://campus-classroom-development.up.railway.app`,
   `EMBED_TOKEN_SECRET=<new random value>`, `PUBLIC_URL=<docs domain>`,
   plus the standard `CLIENT_ID`/`CLIENT_SECRET`/`SECRET_KEY`/`ENV`.
3. Generate a docs domain (`railway domain` or dashboard), e.g.
   `campus-classroom-docs-development.up.railway.app`.
4. On the main service set: `EMBED_ORIGIN=https://<docs domain>`,
   `EMBED_TOKEN_SECRET=<same value>`.
5. Redeploy both. Verify: `GET <docs>/embed/x/y` → 404 without a token,
   403 with a bad one; `/a/` of an assignment with a document question
   shows the frame.

Local dev works with one instance served twice: run `main.py` on port
5000 (main role) and a second process with `EMBED_MODE=1`,
`EMBED_ORIGIN=http://localhost:5001`, `EMBED_PARENT_ORIGIN=http://localhost:5000`,
`PORT=5001`. (`EMBED_TOKEN_SECRET` falls back to `SECRET_KEY`.)

## Harness

`.venv/Scripts/python.exe scripts/verify_issue_62.py` — offline
(fake Campus + faked login, #28 pattern): token/nonce semantics,
sanitiser, enforcement on create/patch, byte-compat for text questions,
share-page iframe/degrade rendering, embed route gating + runtime
injection + CSP.
