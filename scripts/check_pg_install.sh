#!/usr/bin/env bash
# Can we install a Postgres server here, without a human at the keyboard?
set -u

echo "== sudo without a password?"
if timeout 10 sudo -n true 2>/dev/null; then
  echo "  yes"
  SUDO=1
else
  echo "  no (sudo needs a password, and there is nobody to type it)"
  SUDO=0
fi

echo
echo "== apt candidate for postgresql"
timeout 30 apt-cache policy postgresql 2>/dev/null | head -4 || echo "  apt-cache unavailable"

echo
echo "== disk"
df -h /home 2>/dev/null | tail -1

if [ "${SUDO}" -eq 1 ]; then
  echo
  echo "  sudo is available: 'sudo apt-get install -y postgresql' would give a"
  echo "  real server to test against."
else
  echo
  echo "  Without sudo, a server can still be run entirely as this user IF the"
  echo "  postgres binaries exist (initdb + pg_ctl into a directory we own)."
  echo "  They do not — /usr/lib/postgresql is absent — so installing the"
  echo "  package is the only route, and that needs root."
fi
