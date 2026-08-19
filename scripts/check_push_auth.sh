#!/usr/bin/env bash
# Can this machine authenticate to the remote? Pushes nothing.
cd "$(dirname "$0")/.." || exit 1

echo "== remote =="
git remote get-url origin

echo
echo "== credential helper configured =="
git config --get credential.helper || echo "  (none)"

echo
echo "== stored credentials =="
[ -f ~/.git-credentials ] && echo "  ~/.git-credentials exists" \
                          || echo "  no ~/.git-credentials"
[ -n "${GITHUB_TOKEN:-}" ] && echo "  GITHUB_TOKEN is set" \
                           || echo "  GITHUB_TOKEN not set"
command -v gh >/dev/null 2>&1 && echo "  gh CLI present" || echo "  no gh CLI"

echo
echo "== can we reach the remote read-only (10s timeout) =="
GIT_TERMINAL_PROMPT=0 timeout 20 git ls-remote --heads origin >/dev/null 2>&1 \
  && echo "  YES — anonymous read works" \
  || echo "  no (private repo, no network, or auth required)"

echo
echo "== dry-run push (never prompts; fails fast if auth is missing) =="
GIT_TERMINAL_PROMPT=0 timeout 40 git push --dry-run origin main 2>&1 | tail -6

echo
echo "== local commit subject sanity =="
git log --oneline -3
