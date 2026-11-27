"""Gateway entry: apphost sign-in allowlist (#169) and the pre-cutover
forward to the PUBLIC_URL origin (#171).

Two contracts:

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

# --- result -----------------------------------------------------------------

failed = [name for name, ok in checks if not ok]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
if failed:
    print("FAILED:")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
