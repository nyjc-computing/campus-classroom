"""Gateway entry: apphost sign-in allowlist (#169) and the pre-cutover
forward to the PUBLIC_URL origin (#171), plus the cutover-readiness
contracts of #170/#157: root static mount, /health, and the inert
canonical-host flag.

Contracts:

  A. campus-apphost (EMBED_MODE=1) lets a publisher sign in on its own
     origin again. Since #165 the auth blueprint mounts at the root and
     request.path is namespace-stripped, so the containment allowlist
     must name /login, /finalize_login and /logout — while still
     refusing everything outside the /apps surface.
  B. Until PUBLIC_URL names the gateway host, a request that arrives
     through campus-gateway (X-Forwarded-Prefix -> SCRIPT_NAME) is sent
     to the same resource on the PUBLIC_URL origin, where sign-in can
     complete. It stops once the two hosts agree, and origin traffic
     (no prefix header) is never redirected.
  C. Static mounts at Flask's root: /static/... at the origin, single
     /classroom/static/... behind the gateway (#170 item 3 — the #162
     literal mount double-prefixed). /health answers a plain 200 on
     both deployments, including behind the gateway and inside the
     apphost containment (#157).
  D. CANONICALIZE_ORIGIN_HOST (issue #170 item 5): inert when unset;
     when set, a direct hit on a non-canonical host (no prefix header —
     i.e. the old origin domain after the PUBLIC_URL flip) bounces to
     its namespaced gateway shape, so distributed /a/<id> coursework
     links keep working. Gateway-hosted traffic keeps the #173 forward.

Run:  python tests/test_gateway_entry.py   (exit 1 on violation)
"""

import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Same dummy environment as the CI smoke job; PUBLIC_URL is forced so the
# pre/post-cutover comparison below is exact. Nothing leaves the process.
for key, value in {
    "SECRET_KEY": "test",
    "CLIENT_ID": "ci-dummy",
    "CLIENT_SECRET": "ci-dummy",
    "BASE_URL": "https://campus.test",
}.items():
    os.environ.setdefault(key, value)
ORIGIN = "https://origin.example"
os.environ["PUBLIC_URL"] = ORIGIN
os.environ.pop("EMBED_MODE", None)

import flask  # noqa: E402
from campus.flask_campus import context as auth_context  # noqa: E402

from apps.classroom import create_app  # noqa: E402

# /finalize_login without code/state raises inside the auth blueprint; that
# is the expected shape of a bare probe, so keep its traceback out of output.
logging.getLogger("apps.classroom").setLevel(logging.CRITICAL)

checks = []


def check(name, got, want):
    ok = want(got) if callable(want) else got == want
    checks.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got: {got!r}"))


def via_gateway(client, path, prefix, host="gateway.example"):
    """A request the way campus-gateway forwards it."""
    return client.get(
        path,
        headers={"X-Forwarded-Prefix": prefix, "Host": host},
    )


def location(response):
    return response.headers.get("Location", "")


# /login asks campus.auth to mint the login session before redirecting; the
# test only needs to see that the view runs and which callback it registers.
login_targets = []


def _stub_start_login(_campus, *, target, scopes=None):  # noqa: ARG001
    login_targets.append(target)
    return flask.redirect("https://auth.test/authorize")


auth_context.start_login = _stub_start_login


# --- B. classroom deployment, before the PUBLIC_URL flip ---------------------

client = create_app().test_client()

r = via_gateway(client, "/classroom/sign-in", "/classroom")
check("gateway-hosted request is redirected", r.status_code, 302)
check("… to the PUBLIC_URL origin", location(r), f"{ORIGIN}/sign-in")

r = via_gateway(client, "/classroom/dashboard?tab=1", "/classroom")
check("… ahead of the anonymous login redirect, query intact",
      location(r), f"{ORIGIN}/dashboard?tab=1")

r = via_gateway(client, "/classroom/", "/classroom", host="origin.example")
check("no redirect once PUBLIC_URL names the gateway host", r.status_code, 200)

check("origin request is never redirected",
      client.get("/").status_code, 200)
r = client.get("/dashboard")
check("origin anonymous page keeps its own login redirect",
      location(r), "/login?next=/dashboard")

# --- A. campus-apphost deployment -------------------------------------------

os.environ["EMBED_MODE"] = "1"
client = create_app().test_client()

r = via_gateway(client, "/apps/add", "/apps")
check("apphost: gateway-hosted request keeps the /apps prefix",
      location(r), f"{ORIGIN}/apps/add")

r = client.get("/apps/add")
check("apphost origin: anonymous publisher page redirects to login",
      r.status_code, 302)
login = location(r)
check("… at the root-mounted login", login.startswith("/login?next="), True)

r = client.get(login)
check("apphost origin: /login reaches the auth blueprint (was a containment 404, #169)",
      r.status_code, 302)
check("… and starts the OAuth flow", location(r), "https://auth.test/authorize")
check("… with the origin's registered root callback",
      login_targets[-1], f"{ORIGIN}/finalize_login")

for path in ("/finalize_login", "/logout"):
    check(f"apphost origin: {path} is not a containment 404",
          client.get(path).status_code, lambda s: s != 404)

for path in ("/dashboard", "/a/uid-assignment-x", "/assignments"):
    check(f"apphost containment still refuses {path}",
          client.get(path).status_code, 404)

# --- C. root static mount + /health (#170 item 3, #157) ----------------------

os.environ.pop("EMBED_MODE", None)
client = create_app().test_client()

r = client.get("/", headers={"Host": "origin.example"})
html = r.get_data(as_text=True)
check("origin page emits the root static URL",
      'href="/static/css/style.css"' in html, True)
check("… and degrades the URL globals to root shapes",
      'window.CAMPUS_API_BASE = "/api/v1"' in html, True)

r = via_gateway(client, "/classroom/", "/classroom", host="origin.example")
html = r.get_data(as_text=True)
check("gateway page emits the single-prefix static URL",
      'href="/classroom/static/css/style.css"' in html, True)
check("… with no double prefix",
      "/classroom/classroom/static" not in html, True)
check("… and namespaced URL globals",
      'window.CAMPUS_API_BASE = "/classroom/api/v1"' in html, True)

check("origin serves /static",
      client.get("/static/css/style.css",
                 headers={"Host": "origin.example"}).status_code, 200)
check("gateway-emulated static resolves at the single prefix",
      via_gateway(client, "/classroom/static/css/style.css", "/classroom",
                  host="origin.example").status_code, 200)

check("origin /health answers 200 ok",
      client.get("/health", headers={"Host": "origin.example"}).data, b"ok")
check("gateway-emulated /health answers 200",
      via_gateway(client, "/classroom/health", "/classroom",
                  host="origin.example").status_code, 200)

os.environ["EMBED_MODE"] = "1"
client = create_app().test_client()
check("apphost: /health is exempt from the containment 404",
      client.get("/health").status_code, 200)
check("apphost: gateway-emulated /health answers 200",
      via_gateway(client, "/apps/health", "/apps",
                  host="origin.example").status_code, 200)
check("apphost: origin serves /static",
      client.get("/static/css/style.css").status_code, 200)

# --- D. canonical-host flag (issue #170 item 5) ------------------------------
# Post-flip simulation: PUBLIC_URL names the gateway host; the origin
# domain keeps answering directly. Flag on: distributed /a/<id> links on
# the origin domain bounce to their namespaced gateway shape.

GATEWAY = "https://gateway.example"
os.environ.pop("EMBED_MODE", None)
os.environ["CANONICALIZE_ORIGIN_HOST"] = "1"
os.environ["PUBLIC_URL"] = GATEWAY
client = create_app().test_client()

# Flag-off inertness was proven above: with CANONICALIZE_ORIGIN_HOST
# unset, origin-hosted requests whose host differs from PUBLIC_URL
# ("localhost" vs origin.example there) were never redirected.

r = client.get("/a/uid-assignment-x", headers={"Host": "origin.example"})
check("flag on: direct hit on the old origin redirects", r.status_code, 302)
check("… to the namespaced gateway shape",
      location(r), f"{GATEWAY}/classroom/a/uid-assignment-x")

r = client.get("/a/uid-assignment-x?course=c1",
               headers={"Host": "origin.example"})
check("… with the query intact",
      location(r), f"{GATEWAY}/classroom/a/uid-assignment-x?course=c1")

r = client.get("/a/uid-assignment-x", headers={"Host": "gateway.example"})
check("the canonical host itself is never redirected",
      location(r).startswith("/"), True)

r = via_gateway(client, "/classroom/dashboard", "/classroom",
                host="origin.example")
check("flag on: gateway-hosted requests keep the #173 forward",
      location(r), f"{GATEWAY}/dashboard")

r = client.get("/health", headers={"Host": "origin.example"})
check("flag on: origin /health answers, never redirects",
      (r.status_code, r.data), (200, b"ok"))
r = via_gateway(client, "/classroom/health", "/classroom",
                host="origin.example")
check("flag on: gateway-emulated /health answers",
      (r.status_code, r.data), (200, b"ok"))

os.environ["EMBED_MODE"] = "1"
client = create_app().test_client()
r = client.get("/apps/add", headers={"Host": "origin.example"})
check("flag on: apphost origin hit adds the namespace once",
      location(r), f"{GATEWAY}/apps/apps/add")

r = client.get("/health", headers={"Host": "origin.example"})
check("flag on: apphost origin /health answers, never redirects",
      (r.status_code, r.data), (200, b"ok"))

# --- result -----------------------------------------------------------------

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
