#!/bin/sh
# Everything that can be checked without spending money or waiting on a model.
set -e
cd "$(dirname "$0")/.."
python3 -m compileall -q ai_peer_reviewer
python3 -m tests.test_names
python3 -m tests.test_routes
