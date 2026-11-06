# Campus suite dependency tree and relock order

Every campus package pins its campus dependencies by **wheel URL** (a GitHub
Release asset). Poetry treats a URL dependency as an **exact pin** — not a
range — so when a package anywhere in the tree releases a new version, every
package above it must re-pin and re-release before downstream consumers can
lock it. This document records the tree, the pin styles, and the order to
bump/lock packages in, so a relock is a checklist instead of a solver-error
archaeology dig (the SDK 0.7.1 case that motivated this doc is the worked
example at the bottom).

As of **2026-11-06**.

## The tree (runtime deps only)

```
depth 0   campus-common v0.3.1            ← leaf: no campus deps (bcrypt only)
             │
depth 1   campus-model v0.1.4            → campus-common
          campus-deploy v0.1.0           → campus-common
             │
depth 2   campus-core v0.1.1             → campus-common, campus-model
          campus-api-python (SDK)        → campus-common, campus-model
             │                            latest: v0.7.1
depth 3   flask-campus v0.1.4            → flask, werkzeug, requests,
             │                            campus-common, campus-model,
             │                            campus-api-python (URL == 0.7.0)
depth 4   campus (framework monolith)    → campus-api-python 0.7.0,
             │                            campus-common 0.3.1, campus-model 0.1.4,
             │                            campus-deploy 0.1.0, flask-campus 0.1.4,
             │                            campus-core 0.1.1 (deploys from git
             │                            `weekly`, relocks like an app)
depth 5   apps (leaf consumers — chain ends here):
          campus-classroom               → campus-api-python 0.7.0,
             │                            flask-campus 0.1.4, campus-common 0.3.1
          campus-admin                   → campus-api-python @ git main,
             │                            flask-campus 0.1.2, campus-common 0.3.1
          campus-profile                 → campus-api-python @ git main,
                                           flask-campus 0.1.2, campus-common 0.3.1,
                                           campus-model 0.1.3
```

Campus-classroom does **not** pin campus-model/core/deploy directly — they
arrive transitively through flask-campus's wheel metadata. Its own direct
pins are the three in `pyproject.toml`: campus-api-python, flask-campus,
campus-common.

## Pin styles

| Style | Example | Semantics |
|---|---|---|
| Wheel URL | `campus-api-python = {url = "…/releases/download/v0.7.0/campus_api_python-0.7.0-py3-none-any.whl"}` | **Exact pin.** The solver requires that identical URL; a different version anywhere in the tree is a hard conflict. Used by classroom, flask-campus, campus, campus-core, campus-model, campus-deploy, campus-api-python. |
| Git branch | `campus-api-python = {git = "…", branch = "main"}` | **Floats at re-lock time**: the lockfile snapshots whatever main was when `poetry lock` last ran (profile's snapshot is 0.5.1 — old). Used by campus-admin and campus-profile for the SDK. |

**The trap that motivates the order rule:** a released wheel's dependency
pins are frozen into its metadata at build time. Editing flask-campus's
`pyproject.toml` today does not change what the released v0.1.4 wheel
requires — it will advertise `campus_api_python @ <0.7.0 URL>` forever. The
only way to re-point a released package at a new dependency version is to
**cut a new release**.

## Release mechanics

Releases are cut by **pushing an annotated `v*` tag** — CI builds the wheel
and publishes the GitHub Release (the tag message becomes the release body;
see `ci.yml`'s `release` job in the package repos, e.g. campus-api-python).
The wheel asset filename is what the URL pins point at:

```
https://github.com/nyjc-computing/<repo>/releases/download/vX.Y.Z/<pkg>-X.Y.Z-py3-none-any.whl
```

CI also runs a fresh-venv wheel smoke that installs the built wheel from
exactly the URLs baked into its metadata — so a released wheel is guaranteed
self-consistent. The flip side: those baked URLs are what create relock
cascades.

## Relock order (the rule)

**Bump leaf-first, one release per hop.** A change at depth N is adoptable
by depth N-1 only after depth N has released, and every package on the path
must re-pin and re-release before the apps can lock:

1. **Deepest changed package**: bump version, push the annotated `v*` tag,
   CI publishes the release.
2. **Each direct dependent, walking up the tree**: update the URL pin in its
   `pyproject.toml` → `poetry lock` → verify (CI / local harnesses) → push
   the annotated tag to release its own new wheel.
3. **Apps last**: update URL pins → `poetry lock` → `poetry install` → run
   the offline harnesses → PR → merge (autodeploy).

Skip nothing on the path: the solver enforces it, but finding out via the
error message (below) is slower than following the order.

### Checking for drift

Latest release per repo vs what everyone pins:

```bash
for r in campus-common campus-model campus-deploy campus-core \
         campus-api-python flask-campus; do
  printf "%s: " $r
  gh api repos/nyjc-computing/$r/releases --jq '.[0].tag_name'
done
```

## Drift as of 2026-11-06

| Package | Latest release | Pinned at | Status |
|---|---|---|---|
| campus-common | v0.3.1 | 0.3.1 everywhere | current |
| campus-model | v0.1.4 | 0.1.4 (core, flask-campus, campus); **0.1.3 (profile)** | profile drifted |
| campus-deploy | v0.1.0 | 0.1.0 (campus) | current |
| campus-core | v0.1.1 | 0.1.1 (campus) | current |
| campus-api-python | **v0.7.1** | **0.7.0** (flask-campus 0.1.4, campus, classroom); git-main float (admin, profile) | **drifted — the live relock chain below** |
| flask-campus | v0.1.4 | 0.1.4 (campus, classroom); **0.1.2 (admin, profile)** | admin/profile drifted |

## Worked example: the SDK 0.7.1 relock (open as of this writing)

campus-api-python released **v0.7.1** (#160, nesting-safe
`with_user_session`, fix for #159 — found via classroom #81's live E2E,
classroom #92). Dev behaviour is already correct via classroom PR #93's
throwaway-instance mitigation, so this cascade is hygiene, not urgency —
but it is the canonical walkthrough:

1. ✅ campus-api-python v0.7.1 released (depth 2).
2. ⬜ **flask-campus** (depth 3): pyproject URL → v0.7.1 wheel, `poetry
   lock`, CI green, push annotated tag **v0.1.5**. Until this release
   exists, nothing URL-pinning flask-campus can move — v0.1.4's metadata
   hard-requires the 0.7.0 URL. *(This is the blocker.)*
3. ⬜ **campus-classroom** (app): pyproject URLs → SDK v0.7.1 + flask-campus
   v0.1.5, `poetry lock`, `poetry install`, run the offline harnesses, PR.
4. ⬜ Same for the **campus framework** relock and, when convenient, the
   admin/profile drift (flask-campus 0.1.2 → 0.1.5; their git-main SDK locks
   are stale snapshots worth refreshing at the same time).

## Diagnostic: the solver-conflict signature

When two URL pins for the same package disagree, `poetry lock` fails with a
message like:

```
Because flask-campus (0.1.4) … depends on both campus_api_python (*) @
…/v0.7.0/campus_api_python-0.7.0-py3-none-any.whl and campus_api_python (*)
@ …/v0.7.0/campus_api_python-0.7.0-py3-none-any.whl, campus-api-python is
required. So, because campus-classroom depends on campus-api-python (0.7.1)
@ …/v0.7.1/…, version solving failed.
```

Translation: **some other package's released wheel pins the old URL**. The
two "both" URLs look identical because only the version digit in the path
differs — read them character by character. The fix is never to loosen the
app's pin; it is to walk up the tree and re-release the pinning package
(section above).
