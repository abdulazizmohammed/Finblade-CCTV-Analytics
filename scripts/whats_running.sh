#!/usr/bin/env bash
# What is actually listening, and what keeps it alive between commands?
set -u
echo "== listening sockets of interest"
ss -ltnp 2>/dev/null | grep -E ':(8554|8000|5432)' || echo "  none of 8554 / 8000 / 5432"

echo
echo "== processes"
for p in mediamtx uvicorn ffmpeg postgres; do
  out=$(pgrep -a "$p" 2>/dev/null | head -3)
  echo "  ${p}: ${out:-none}"
done

echo
echo "== init system (does anything supervise these?)"
if command -v systemctl >/dev/null && systemctl is-system-running >/dev/null 2>&1; then
  echo "  systemd is running"
  systemctl --no-pager --plain list-units 'finblade*' 2>/dev/null | head -5
else
  echo "  no systemd — nothing supervises a background process here,"
  echo "  and WSL stops the distro when the last command exits."
fi

echo
echo "== WSL boot config (systemd can be turned on)"
cat /etc/wsl.conf 2>/dev/null | sed 's/^/  /' || echo "  /etc/wsl.conf absent"
