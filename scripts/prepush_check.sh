#!/usr/bin/env bash
# Safety scan before pushing to a PUBLIC remote. Reads only; pushes nothing.
cd "$(dirname "$0")/.." || exit 1

# Default to the tracked upstream. NOT written inline as @{u}..HEAD — bash
# brace-expands that into @{u..HEAD}, every git call fails, and the credential
# scan below then greps an empty revision list and reports "ok" having checked
# nothing. A safety check that cannot fail is worse than no check.
UPSTREAM=$(git rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo origin/main)
RANGE="${1:-$UPSTREAM..HEAD}"
git rev-list "$RANGE" >/dev/null 2>&1 || {
  echo "[BLOCKER] bad range: $RANGE" >&2; exit 2; }
FAIL=0

echo "== commits in range =="
git log --oneline "$RANGE"
echo

echo "== files touched =="
git diff --name-only "$RANGE" | sort
echo

echo "== real local keys must NOT appear anywhere in the range =="
for f in .local_key .local_integration_key; do
  [ -f "$f" ] || continue
  KEY=$(cat "$f")
  [ -z "$KEY" ] && continue
  REVS=$(git rev-list "$RANGE")
  if [ -z "$REVS" ]; then
    echo "  SKIP: no commits in range to search"
  elif git grep -q -- "$KEY" $REVS 2>/dev/null; then
    echo "  FAIL: contents of $f appear in a commit"
    FAIL=1
  else
    echo "  ok: $f not in any of $(echo "$REVS" | wc -l) commit(s)"
  fi
done

echo
echo "== key files must be untracked =="
for f in .local_key .local_integration_key auto.key auto.crt; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "  FAIL: $f is TRACKED"
    FAIL=1
  else
    echo "  ok: $f untracked"
  fi
done

echo
echo "== credential-shaped strings in the diff =="
# scheme://user:pass@host, excluding the mask and obvious test values
HITS=$(git diff "$RANGE" -- . ':!scripts/audit_option1*.py' ':!tests/*' \
        | grep -E '^\+' \
        | grep -Ei '[a-z]+://[^/@:[:space:]]+:[^/@[:space:]]+@' \
        | grep -v '\*\*\*' || true)
if [ -n "$HITS" ]; then
  echo "$HITS"
  echo "  ^ review these"
  FAIL=1
else
  echo "  none (test fixtures and redaction tests excluded by design)"
fi

echo
echo "== large blobs being added =="
git diff --stat "$RANGE" | tail -1
BIG=$(git diff --numstat "$RANGE" | awk '$1 > 3000 {print "  " $3 " (+" $1 " lines)"}')
[ -n "$BIG" ] && echo "$BIG" || echo "  none over 3000 lines"

echo
echo "== untracked files NOT ignored (would be missed by the push) =="
git status --porcelain | grep '^??' | grep -vE 'evidence/|media/' || echo "  none"

echo
[ "$FAIL" = 0 ] && echo "SAFE TO PUSH" || echo "DO NOT PUSH — see FAIL lines above"
exit "$FAIL"
