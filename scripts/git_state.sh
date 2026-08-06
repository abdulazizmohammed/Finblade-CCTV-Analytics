#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
echo "== remotes =="
git remote -v || echo "  (none configured)"
echo
echo "== branch =="
git rev-parse --abbrev-ref HEAD
echo
echo "== upstream =="
git rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo "  (no upstream set)"
echo
echo "== commits not on the remote =="
if git rev-parse '@{u}' >/dev/null 2>&1; then
  git log --oneline '@{u}..HEAD'
else
  echo "  no upstream — local commits:"
  git log --oneline -14
fi
echo
echo "== working tree =="
git status --porcelain | head -30
echo
echo "== size of what would be pushed =="
git count-objects -vH | grep -E 'size-pack|count'
