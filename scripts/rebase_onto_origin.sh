#!/usr/bin/env bash
# Fetch, then replay local commits on top of origin/main. Stops on conflict.
# Does NOT push. Nothing here rewrites published history.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

echo "== fetch =="
git fetch origin

echo
echo "== before =="
echo "  ahead : $(git log --oneline HEAD --not origin/main | wc -l)"
echo "  behind: $(git log --oneline origin/main --not HEAD | wc -l)"
git log --oneline origin/main --not HEAD | sed 's/^/    remote-only: /'

if [ "$(git log --oneline origin/main --not HEAD | wc -l)" -eq 0 ]; then
  echo
  echo "already up to date with origin — nothing to rebase"
  exit 0
fi

echo
echo "== rebasing local commits onto origin/main =="
if git rebase origin/main; then
  echo
  echo "== after =="
  echo "  ahead : $(git log --oneline HEAD --not origin/main | wc -l)"
  echo "  behind: $(git log --oneline origin/main --not HEAD | wc -l)"
  echo
  echo "== the remote's change survived the rebase =="
  grep '^model_path' config/cameras.template.yaml
else
  echo
  echo "[BLOCKER] rebase hit a conflict and has been left in progress."
  echo "  git status            # see the conflicted files"
  echo "  git rebase --abort    # to back out entirely"
  exit 1
fi
