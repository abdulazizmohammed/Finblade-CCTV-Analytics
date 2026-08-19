#!/usr/bin/env bash
# Are there REAL secrets in tracked files, or only placeholders?
#
# A scanner that flags rtsp://user:password@ in a docstring gets ignored, and
# an ignored scanner is worse than none. This separates the two: real values
# are things that would actually work, placeholders are the words we document
# the masking with.
set -u
cd "$(dirname "$0")/.."

echo "== the site's real camera IPs (from config, not invented)"
git grep -nI -E '192\.168\.200\.[0-9]+' -- . ':!scripts/deploy_exclusions.py' \
  ':!scripts/secret_scan.sh' 2>/dev/null || echo "  none tracked"

echo
echo "== live API keys"
git grep -nI -E '(FINBLADE_(API|INTEGRATION)_KEY\s*=\s*[A-Za-z0-9_\-]{24,})' -- . \
  2>/dev/null || echo "  none tracked"

echo
echo "== AWS keys / private keys"
git grep -nI -E '(AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY)' -- . 2>/dev/null \
  || echo "  none tracked"

echo
echo "== rtsp credentials, EXCLUDING documented placeholders"
# user:pass, user:password, admin:${VAR} and regex fragments are what we use to
# describe masking; a real one has neither a shell variable nor a regex class.
git grep -nI -E 'rtsp://[^:/[:space:]]+:[^@/[:space:]]+@' -- . 2>/dev/null \
  | grep -vE 'user:(pass|password)@' \
  | grep -vE '\$\{|\[\^|\(\?|\*\*\*' \
  | grep -vE '^(tests|docs)/' \
  || echo "  none beyond documented placeholders"

echo
echo "== files git would refuse to let you forget"
for f in .local_key .local_integration_key .env auto.key auto.crt; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "  TRACKED (bad): $f"
  else
    echo "  untracked ok: $f"
  fi
done
