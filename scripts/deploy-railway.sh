#!/usr/bin/env bash
# Deploy campus-classroom to the Railway dev deployment
# (campus project, development environment -> campus-classroom-development.up.railway.app).
#
# Why the staging copy: `railway up` (CLI 5.63 on Windows) packs the local
# .venv into the upload (~270MB, 413/502 from the edge) even though .gitignore
# and .dockerignore list it. Deploying from a clean copy of the build inputs
# (~3MB) sidesteps it. Revisit if the CLI's ignore handling gets fixed.
set -euo pipefail

STAGING="$(mktemp -d)"
PROJECT_ID="76e0b6d0-0832-41dc-841f-e3bc758d3eca"
SERVICE="campus-classroom"
ENVIRONMENT="development"

trap 'rm -rf "$STAGING"' EXIT

cp pyproject.toml poetry.lock main.py Procfile .python-version README.md "$STAGING/"
cp -r apps "$STAGING/apps"
find "$STAGING" -type d -name __pycache__ -exec rm -rf {} +

railway up "$STAGING" --path-as-root \
    -p "$PROJECT_ID" -s "$SERVICE" -e "$ENVIRONMENT" \
    -m "campus-classroom CLI deploy"
