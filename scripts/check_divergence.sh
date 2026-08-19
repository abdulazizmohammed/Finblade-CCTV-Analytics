#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
echo "== local main is AHEAD by =="
git log --oneline HEAD --not origin/main | wc -l
echo
echo "== origin/main is AHEAD by (commits I do NOT have) =="
git log --oneline origin/main --not HEAD | wc -l
git log --oneline origin/main --not HEAD | head -20
echo
echo "== merge base =="
git merge-base HEAD origin/main | head -1
git log --oneline -1 "$(git merge-base HEAD origin/main)"
echo
echo "== last fetch of origin =="
if [ -f .git/FETCH_HEAD ]; then
  stat -c '%y' .git/FETCH_HEAD
else
  echo "  never fetched in this clone"
fi
echo
echo "== would a plain push fast-forward? =="
if [ "$(git log --oneline origin/main --not HEAD | wc -l)" -eq 0 ]; then
  echo "  YES — safe fast-forward"
else
  echo "  NO — remote has commits this branch lacks. Push would be REJECTED."
fi
