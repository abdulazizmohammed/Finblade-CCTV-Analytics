#!/usr/bin/env bash
# Ask the REMOTE what it has, not the local tracking ref.
cd "$(dirname "$0")/.." || exit 1
echo "local HEAD                : $(git rev-parse HEAD)"
echo "local refs/remotes/origin : $(git rev-parse origin/main)"
echo
echo "asking github over the network..."
REMOTE=$(GIT_TERMINAL_PROMPT=0 timeout 40 git ls-remote origin refs/heads/main | awk '{print $1}')
echo "actual origin/main        : ${REMOTE:-<could not read>}"
echo
if [ -n "$REMOTE" ] && [ "$REMOTE" = "$(git rev-parse HEAD)" ]; then
  echo "PUSHED — the remote has this exact commit"
elif [ -n "$REMOTE" ]; then
  echo "NOT pushed — remote is at a different commit"
  echo "commits the remote is missing:"
  git log --oneline "$REMOTE..HEAD" | sed 's/^/  /'
else
  echo "could not reach the remote"
fi
