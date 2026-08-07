#!/usr/bin/env bash
# Which conformance tests fail, and on which backends.
#
# A failure on all three is a bug in the test; a failure on one is a bug in
# that store. Grouping by test name rather than reading the log top to bottom
# makes the difference obvious immediately.
set -u
cd "$(dirname "$0")/.."
source .venv/bin/activate
mkdir -p scripts/logs
timeout 540 python -m unittest tests.test_store_conformance \
  > scripts/logs/conf.log 2>&1
echo "--- totals"
tail -3 scripts/logs/conf.log
echo
echo "--- failures grouped by test (backends affected)"
grep -E '^(FAIL|ERROR):' scripts/logs/conf.log \
  | sed -E 's/^(FAIL|ERROR): ([a-z_]+) .*\.(Test[A-Za-z]+)\)?/\2 \3/' \
  | sort | awk '{ t[$1] = t[$1] " " $2 } END { for (k in t) print k ":" t[k] }' \
  | sort
