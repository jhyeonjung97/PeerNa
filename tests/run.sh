#!/bin/sh
# Everything that can be checked without spending money or waiting on a model.
set -e
cd "$(dirname "$0")/.."
python3 -m compileall -q ai_peer_reviewer
python3 -m tests.test_names
python3 -m tests.test_routes
python3 -m tests.test_jobs
python3 -m tests.test_access
python3 -m tests.test_corpus_sync
python3 -m tests.test_parse
# Import every module with our own deprecation warnings as errors. A pattern
# with an inline flag mid-expression only warns on the Python here and is an
# error on the 3.12 in the container, which took every PDF review down with it.
python3 -W error::DeprecationWarning -c "
import importlib, pkgutil, ai_peer_reviewer
for m in pkgutil.iter_modules(ai_peer_reviewer.__path__):
    importlib.import_module('ai_peer_reviewer.' + m.name)
print('  every module imports cleanly')"
