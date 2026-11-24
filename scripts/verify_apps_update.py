"""Verification for the apphost app-update surface (#142 follow-up:
edit an app's name / slug / content + drag-and-drop uploads).

The update surface:

1. GET /apps/edit/<slug> renders for the owning publisher and 404s for
   everyone else (existence is not revealed), on the apphost deploy
   only.
2. PATCH /api/v1/apps/<slug> — owner-gated, per-field updates:
   name-only, content-only, slug move (copy to an UNUSED slug, old
   copy stays unless remove_old_slug), and their combinations; every
   failure shape (foreign 403, unknown 404, move-target 409, bad html,
   oversize, nothing-to-update, missing CSRF header) and the #98
   dead-grant seam answering 401 campus_session_expired.
3. /apps/myapps carries the per-app Edit link and the ?updated= banner.

No network and no real Campus are involved — the hostedapps registry
is faked in memory at the campus_session.user_session() seam (verify
issue #98 harness pattern: env before imports, fake push_context,
test_client with seeded sessions and the X-Requested-With header).

Usage: .venv/Scripts/python.exe scripts/verify_apps_update.py
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace

# Test environment must be in place before apps.classroom imports (its
# load_dotenv() does not override variables that are already set).
os.environ["PUBLIC_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "verify-apps-update-secret"
os.environ["EMBED_MODE"] = "1"

import campus.flask_campus.context as flask_context  # noqa: E402
import campus_python  # noqa: E402
import flask  # noqa: E402
from campus_python import errors as campus_errors  # noqa: E402

from apps.classroom import create_app  # noqa: E402

OWNER = "uid-user-publisher"
OTHER = "uid-user-other"

V1_HTML = b"<html><body><h1>v1</h1></body></html>"
V2_HTML = "<html><body><h1>v2</h1></body></html>"
NOW = "2026-11-24T12:00:00"


class NotFound(Exception):
    """The fake registry's miss error ('not found' matches the seam)."""


class FakeRegistry:
    """client.api.hostedapps stand-in: an in-memory by-slug store."""

    def __init__(self):
        self.apps = {}

    def seed(self, slug, user_id, content=V1_HTML, name=None):
        self.apps[slug] = {
            "slug": slug,
            "name": name,
            "content": content,
            "content_type": "text/html",
            "size": len(content),
            "uploaded_by_user_id": user_id,
            "updated_at": NOW,
        }

    def _require(self, slug):
        record = self.apps.get(slug)
        if record is None:
            raise NotFound("Hosted app not found")
        return record

    def list(self, prefix=None):
        return [
            SimpleNamespace(**{**rec, "content": None})
            for slug, rec in sorted(self.apps.items())
            if not prefix or slug.startswith(prefix)
        ]

    def get_meta(self, slug):
        record = self._require(slug)
        return SimpleNamespace(**{
            key: value for key, value in record.items()
            if key != "content"
        })

    def get_content(self, slug):
        return self._require(slug)["content"]

    def put_by_slug(self, slug, data, content_type=None, name=None):
        record = self.apps.get(slug)
        if record is None:
            record = {
                "slug": slug,
                "name": None,
                "content": None,
                "content_type": content_type or "text/html",
                "size": 0,
                "uploaded_by_user_id": OWNER,
                "updated_at": NOW,
            }
            self.apps[slug] = record
        if isinstance(data, str):
            data = data.encode("utf-8")
        record["content"] = bytes(data)
        record["size"] = len(record["content"])
        record["content_type"] = content_type or record["content_type"]
        record["updated_at"] = "2026-11-24T13:00:00"
        if name:
            record["name"] = name
        return self.get_meta(slug)

    def delete_by_slug(self, slug):
        self._require(slug)
        del self.apps[slug]


class FakeCampus:
    """campus_python.Campus stand-in: user sessions hit the fake
    registry; dead_grant=True mints fail invalid_grant like a dead
    campus grant (#98)."""

    def __init__(self, registry):
        self.registry = registry
        self.dead_grant = False

    @contextmanager
    def with_user_session(self):
        if self.dead_grant:
            raise campus_errors.BadRequestError(
                400,
                "No campus grant for this user and client; "
                "re-authentication is required",
                details={"oauth_error": "invalid_grant"},
            )
        yield SimpleNamespace(
            api=SimpleNamespace(hostedapps=self.registry))


REGISTRY = FakeRegistry()
REGISTRY.seed("html-doctor", OWNER, name="HTML Doctor")
REGISTRY.seed("html-inspector", OWNER)
REGISTRY.seed("foreign-app", OTHER)
FAKE_CAMPUS = FakeCampus(REGISTRY)


def _fake_push_context(*_args):
    flask.g.user = None
    flask.g.device = None
    user = flask.session.get("verify_user")
    if user:
        flask.g.user = SimpleNamespace(id=user["id"], email=user["email"])


flask_context.push_context = _fake_push_context

app = create_app()
app.campus = FAKE_CAMPUS
# _broker_release builds its own throwaway Campus per release (#92) —
# hand it the fake too so no code path constructs a real client.
campus_python.Campus = lambda timeout=None: FAKE_CAMPUS  # noqa: ARG005

failures = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond:
        failures.append(name)


def ajaxify(client):
    """Mutations carry the X-Requested-With header (#12 CSRF guard)."""
    for method in ("post", "patch", "put", "delete"):
        original = getattr(client, method)

        def wrapper(path, *a, _orig=original, **kw):
            headers = kw.pop("headers", None) or {}
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
            kw["headers"] = headers
            return _orig(path, *a, **kw)

        setattr(client, method, wrapper)
    return client


def seed_user(client, user_id):
    with client.session_transaction() as sess:
        sess["verify_user"] = {"id": user_id, "email": f"{user_id}@nyjc.edu.sg"}


owner = ajaxify(app.test_client())
seed_user(owner, OWNER)

# --- edit page --------------------------------------------------------------
resp = owner.get("/apps/edit/html-doctor")
check("GET edit page (owner) -> 200", resp.status_code == 200)
check("edit page shows the slug + drop zone",
      b"html-doctor" in resp.data and b'app-drop' in resp.data)

resp = owner.get("/apps/edit/foreign-app")
check("GET edit page (foreign app) -> 404 (existence hidden)",
      resp.status_code == 404)

resp = owner.get("/apps/edit/never-existed")
check("GET edit page (unknown slug) -> 404", resp.status_code == 404)

resp = owner.get("/apps/myapps")
check("myapps lists the app with an Edit link",
      resp.status_code == 200
      and b"/apps/edit/html-doctor" in resp.data
      and b"/apps/edit/foreign-app" not in resp.data)

# --- PATCH: name-only ---------------------------------------------------------
resp = owner.patch("/api/v1/apps/html-doctor", json={"name": "Doc v2"})
check("PATCH name-only -> 200", resp.status_code == 200,
      resp.get_data(as_text=True))
body = resp.get_json()
check("name-only response is not a move",
      body.get("moved") is False and body.get("content_replaced") is False)
record = REGISTRY.apps["html-doctor"]
check("name-only persists the new name, keeps content",
      record["name"] == "Doc v2" and record["content"] == V1_HTML)

# --- PATCH: content-only -------------------------------------------------------
resp = owner.patch("/api/v1/apps/html-doctor", json={"html": V2_HTML})
check("PATCH html-only -> 200", resp.status_code == 200)
record = REGISTRY.apps["html-doctor"]
check("content-only replaces bytes, keeps the name",
      record["content"].decode() == V2_HTML and record["name"] == "Doc v2")

# --- PATCH: slug move, old copy kept -------------------------------------------
resp = owner.patch(
    "/api/v1/apps/html-doctor",
    json={"slug": "html-doctor-v2"},
)
check("PATCH slug move -> 200", resp.status_code == 200,
      resp.get_data(as_text=True))
check("move creates the new slug with same content + name",
      REGISTRY.apps["html-doctor-v2"]["content"].decode() == V2_HTML
      and REGISTRY.apps["html-doctor-v2"]["name"] == "Doc v2")
check("move without remove_old_slug keeps the old copy",
      "html-doctor" in REGISTRY.apps)
body = resp.get_json()
check("move response names the old slug",
      body.get("moved") is True and body.get("old_slug") == "html-doctor"
      and body.get("old_slug_removed") is False)

# --- PATCH: slug move with removal ---------------------------------------------
resp = owner.patch(
    "/api/v1/apps/html-doctor",
    json={"slug": "html-doctor-final", "remove_old_slug": True},
)
check("PATCH move+remove -> 200", resp.status_code == 200)
check("move+remove deletes the old copy",
      "html-doctor" not in REGISTRY.apps
      and "html-doctor-final" in REGISTRY.apps)

# --- failure shapes --------------------------------------------------------------
resp = owner.patch(
    "/api/v1/apps/html-doctor-final", json={"slug": "foreign-app"})
check("move onto an existing slug -> 409", resp.status_code == 409)
check("the 409 target is untouched",
      REGISTRY.apps["foreign-app"]["uploaded_by_user_id"] == OTHER)

resp = owner.patch("/api/v1/apps/foreign-app", json={"name": "Hijack"})
check("PATCH by a non-owner -> 403", resp.status_code == 403)
check("the foreign app is unchanged",
      REGISTRY.apps["foreign-app"]["name"] is None)

resp = owner.patch("/api/v1/apps/never-existed", json={"name": "X"})
check("PATCH unknown slug -> 404", resp.status_code == 404)

resp = owner.patch("/api/v1/apps/html-doctor-final", json={})
check("PATCH with no fields -> 400 nothing-to-update",
      resp.status_code == 400)

resp = owner.patch(
    "/api/v1/apps/html-doctor-final",
    json={"name": REGISTRY.apps["html-doctor-final"]["name"]})
check("PATCH with only the stored name -> 400 nothing-to-update",
      resp.status_code == 400)

resp = owner.patch("/api/v1/apps/html-doctor-final", json={"html": "   "})
check("PATCH blank html -> 400", resp.status_code == 400)

resp = owner.patch(
    "/api/v1/apps/html-doctor-final", json={"html": "x" * 2_000_001})
check("PATCH oversize html -> 400", resp.status_code == 400)

resp = owner.patch(
    "/api/v1/apps/html-doctor-final", json={"slug": "!!!"})
check("PATCH unsalvageable slug -> 400", resp.status_code == 400)

# Raw client (no injected header): the #12 CSRF guard.
bare = app.test_client()
seed_user(bare, OWNER)
resp = bare.patch("/api/v1/apps/html-doctor-final", json={"name": "Doc"})
check("PATCH without the CSRF header -> 403 csrf_header_missing",
      resp.status_code == 403
      and resp.get_json().get("error", {}).get("code") == "csrf_header_missing")

# --- #98 seam: dead grant mid-session ------------------------------------------
FAKE_CAMPUS.dead_grant = True
resp = owner.patch(
    "/api/v1/apps/html-doctor-final", json={"name": "Doc"})
check("dead campus grant -> 401 campus_session_expired (not 500)",
      resp.status_code == 401
      and resp.get_json().get("error", {}).get("code")
      == "campus_session_expired")
resp = owner.get("/apps/edit/html-doctor-final")
check("dead campus grant on the edit page -> 302 into /login",
      resp.status_code == 302 and "/login" in resp.headers["Location"])
FAKE_CAMPUS.dead_grant = False

# --- non-apphost deploy hides the surface ---------------------------------------
os.environ["EMBED_MODE"] = "0"
try:
    resp = owner.get("/apps/edit/html-doctor-final")
    check("EMBED_MODE=0: edit page -> 404", resp.status_code == 404)
    resp = owner.patch(
        "/api/v1/apps/html-doctor-final", json={"name": "Doc"})
    check("EMBED_MODE=0: PATCH -> 404", resp.status_code == 404)
finally:
    os.environ["EMBED_MODE"] = "1"

# --- redirect-after-save banner ---------------------------------------------------
resp = owner.get("/apps/myapps?updated=html-doctor-final")
check("myapps ?updated= renders the saved banner",
      resp.status_code == 200 and b"html-doctor-final" in resp.data)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED")
    raise SystemExit(1)
print("All checks passed.")
