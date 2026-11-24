#!/bin/sh
# Deploy to the development server: push dev, which GitHub Actions deploys to
# dev.peerna.haileylab.com (and Render's peerna-dev builds, while it runs). The real service is not touched; that is tests/release.sh.
set -e
cd "$(dirname "$0")/.."

[ "$(git rev-parse --abbrev-ref HEAD)" = "dev" ] || { echo "Deploy from dev."; exit 1; }
./tests/run.sh
git push origin dev
echo "Pushed dev; GitHub Actions deploys it to dev.peerna.haileylab.com (and Render peerna-dev rebuilds)."
