#!/usr/bin/env bash
# After a git pull: what actually needs doing on this box?
cd "$(dirname "$0")/.." || exit 1
RANGE="${1:-origin/main..HEAD}"

echo "== do dependencies change in this range? =="
if git diff --name-only "$RANGE" | grep -qE '^(requirements|constraints)\.txt$'; then
  echo "  YES — re-run: .venv/bin/pip install -r requirements.txt -c constraints.txt"
  git diff "$RANGE" -- requirements.txt constraints.txt | head -20
else
  echo "  no — requirements.txt and constraints.txt untouched, no pip install needed"
fi

echo
echo "== is there a compile/build step at all? =="
ls setup.py pyproject.toml Makefile package.json 2>/dev/null || \
  echo "  none — pure Python and static HTML, nothing to build"

echo
echo "== database migration needed? =="
# Watches ddl_pg.sql, because that is where the schema lives. It used to watch
# services/api/sqlite_store.py; when SQLite was removed this check went on
# reporting "no schema change" for every range, which is the worst way for a
# check to fail. Guard against that happening again.
SCHEMA_FILE=services/api/ddl_pg.sql
if [ ! -f "$SCHEMA_FILE" ]; then
  echo "  *** $SCHEMA_FILE is gone — this check is watching nothing ***"
elif git diff "$RANGE" -- "$SCHEMA_FILE" | grep -q 'ADD COLUMN\|CREATE TABLE'; then
  echo "  schema changed — applied automatically by _apply_schema() when the API starts"
  git diff "$RANGE" -- "$SCHEMA_FILE" | grep -E '^\+.*(ADD COLUMN|CREATE TABLE)' | head
else
  echo "  no schema change"
fi

echo
echo "== which running processes hold changed code? =="
git diff --name-only "$RANGE" | grep -q '^services/api/'       && echo "  API      — restart required"
git diff --name-only "$RANGE" | grep -q '^services/inference/' && echo "  camera workers — restart required"
git diff --name-only "$RANGE" | grep -qE '^(web|tools)/'       && echo "  browser  — hard refresh required (Ctrl-Shift-R)"

echo
echo "== model the camera template points at =="
MODEL=$(grep '^model_path' config/cameras.template.yaml | awk '{print $2}')
echo "  config wants : $MODEL"
if [ -f "$MODEL" ]; then
  echo "  on disk      : yes ($(du -h "$MODEL" | cut -f1))"
else
  echo "  on disk      : *** MISSING — cameras will not start ***"
fi
echo "  get_weights.py fetches:"
grep -oE '"[a-z0-9_.]+\.pt"' scripts/get_weights.py | sort -u | sed 's/^/    /'
