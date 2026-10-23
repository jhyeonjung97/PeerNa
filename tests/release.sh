#!/bin/sh
# Release: put what is on dev in front of everyone. Only when the operator says.
#
# The real service has autoDeploy off, so pushing main does not deploy it; this
# pushes main and then asks Render to deploy, through the deploy hook if one is
# configured, or tells you which button to press.
#
# A deploy replaces the instance, and a review runs for minutes in a background
# thread of the old one, so it dies. The checkpoint means the work is not lost,
# but somebody is watching a progress bar, so a running review is asked about.
set -e
cd "$(dirname "$0")/.."

[ "$(git rev-parse --abbrev-ref HEAD)" = "dev" ] || { echo "Release from dev."; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "Commit or set aside the changes first."; exit 1; }
git fetch -q origin
git merge-base --is-ancestor origin/main HEAD || { echo "main has commits dev lacks; merge them first."; exit 1; }

./tests/run.sh
# The container runs Python 3.12. Everything that broke the real service on
# 2026-10-22 passed on 3.10, so 3.12 is checked too when it is available.
PY312="$HOME/PeerNa-data/py312/bin/python"
if [ -x "$PY312" ]; then
  for t in test_names test_routes test_jobs test_access; do "$PY312" -m "tests.$t" | tail -1; done
  "$PY312" -W error::DeprecationWarning -c "
import importlib, pkgutil, ai_peer_reviewer
for m in pkgutil.iter_modules(ai_peer_reviewer.__path__): importlib.import_module('ai_peer_reviewer.' + m.name)
print('  imports cleanly on 3.12')"
  find ai_peer_reviewer -name "*.cpython-312.pyc" -delete
fi

URL=${PEERNA_URL:-https://peerna.onrender.com}
RUNNING=$(curl -fsS -m 20 "$URL/healthz" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("running", 0))' 2>/dev/null || echo "?")
if [ "$RUNNING" != "0" ] && [ "$RUNNING" != "?" ]; then
  echo "$RUNNING review(s) running on $URL. A release would interrupt them."
  printf "Release anyway? [y/N] "; read -r answer
  case "$answer" in [yY]*) ;; *) echo "Stopped."; exit 1 ;; esac
fi

git push origin HEAD:main
HOOK=$(sed -n 's/^RENDER_DEPLOY_HOOK=//p' "$HOME/.config/ai-peer-reviewer/.env" 2>/dev/null | tail -1)
if [ -n "$HOOK" ]; then
  curl -fsS -X POST "$HOOK" >/dev/null && echo "Release deploy started on $URL."
else
  echo "main is pushed. Deploy it from the Render dashboard: peerna -> Manual Deploy -> Deploy latest commit."
fi
