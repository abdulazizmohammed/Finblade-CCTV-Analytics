#!/usr/bin/env bash
# What credential material does a push have available, without using any of it?
#
# Reports presence and host only. Never prints a token, a password, or the
# userinfo half of a stored URL.
set -u
cd "$(dirname "$0")/.."

echo "helper (local)  : $(git config --local  --get credential.helper || echo '-')"
echo "helper (global) : $(git config --global --get credential.helper || echo '-')"
echo "helper (system) : $(git config --system --get credential.helper 2>/dev/null || echo '-')"

f="${HOME}/.git-credentials"
if [ -f "${f}" ]; then
  echo "store file      : present, $(wc -l < "${f}" | tr -d ' ') entry/entries"
  # Host only. The whole point of the file is the part before the @.
  sed -E 's#^[a-z]+://[^@]*@#  host: #' "${f}" | sed -E 's#/.*$##'
else
  echo "store file      : absent  <- this is why the push cannot authenticate"
fi

echo "GIT_ASKPASS     : ${GIT_ASKPASS:-unset}"
echo "GH_TOKEN        : $([ -n "${GH_TOKEN:-}" ] && echo set || echo unset)"
echo "GITHUB_TOKEN    : $([ -n "${GITHUB_TOKEN:-}" ] && echo set || echo unset)"
echo "gh cli          : $(command -v gh >/dev/null && echo present || echo absent)"
if command -v gh >/dev/null; then
  gh auth status 2>&1 | sed 's/^/  /' | head -8
fi
echo "remote          : $(git remote get-url origin | sed -E 's#//[^@]*@#//#')"
