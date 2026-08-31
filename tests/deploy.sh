#!/bin/sh
# Push, but not on top of somebody's review.
#
# A deploy replaces the instance, and a review runs for minutes in a background
# thread of the old one — so it dies. The checkpoint means the work is not lost
# (the same manuscript resumes where it stopped), but somebody is watching a
# progress bar that suddenly says the server stopped, and that is worth waiting
# a few minutes to avoid.
#
# Render's shutdown grace period is far shorter than a review, so draining is
# not an option. Asking is.
set -e
cd "$(dirname "$0")/.."

URL=${PEERNA_URL:-https://peerna.onrender.com}
RUNNING=$(curl -fsS -m 20 "$URL/healthz" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("running", 0))' 2>/dev/null || echo "?")

if [ "$RUNNING" = "?" ]; then
  echo "Could not reach $URL — deploying anyway."
elif [ "$RUNNING" != "0" ]; then
  echo "$RUNNING review(s) running on $URL."
  echo "A deploy would interrupt them. They would resume on re-upload, but wait if you can."
  printf "Push anyway? [y/N] "
  read -r answer
  case "$answer" in [yY]*) ;; *) echo "Stopped."; exit 1 ;; esac
fi

./tests/run.sh
git push origin main
