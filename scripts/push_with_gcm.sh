#!/usr/bin/env bash
# Push using the Windows Git Credential Manager from inside WSL.
#
# WSL git has no credential helper of its own here, so a push over HTTPS has
# nothing to authenticate with. GCM is already installed on the Windows side
# and already holds this repo's credentials, so the fix is to point at it —
# repo-locally, not globally, so nothing outside this checkout changes.
#
# The path contains a space, and git splits the helper string on whitespace
# before running it ("/mnt/c/Program get: not found"). The embedded quotes
# below are what stop that.
#
# RUN THIS FROM A TERMINAL YOU ARE SITTING AT. GCM authenticates by opening a
# window — a browser sign-in or a device-code prompt — so in an unattended
# session it hangs forever with no output rather than failing. Verified: it
# produced nothing for five minutes here and the remote never moved.
#
# Leaves the helper configured on success, so subsequent pushes are silent.
set -u
cd "$(dirname "$0")/.."

GCM='/mnt/c/Program Files/Git/mingw64/bin/git-credential-manager.exe'
if [ ! -x "${GCM}" ]; then
  echo "GCM not found at ${GCM}"
  exit 1
fi

git config --local credential.helper "\"${GCM}\""
echo "helper set to: $(git config --local --get credential.helper)"

echo "pushing $(git rev-parse --short HEAD) -> origin/main"
timeout 180 git push origin main
rc=$?
echo "exit=${rc}"
exit "${rc}"
