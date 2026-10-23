#!/bin/sh
# Deploy to the development server: push dev, which peerna-dev builds on every
# push. The real service is not touched; that is tests/release.sh.
set -e
cd "$(dirname "$0")/.."

[ "$(git rev-parse --abbrev-ref HEAD)" = "dev" ] || { echo "Deploy from dev."; exit 1; }
./tests/run.sh
git push origin dev
echo "Pushed dev; peerna-dev rebuilds in a few minutes."
