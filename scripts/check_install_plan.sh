#!/usr/bin/env bash
# Dry run of scripts/install_ubuntu.sh decisions. Installs nothing.
cd "$(dirname "$0")/.." || exit 1

echo "== CPU-path requirements (torch lines rewritten) =="
sed 's/+cu128//' requirements.txt | grep -E '^(torch|torchvision)'
echo
echo "== CPU-path constraints =="
sed 's/+cu128//' constraints.txt | grep -E '^(torch|torchvision|numpy)'
echo
echo "== which path would this machine take =="
if [ "${FORCE_CPU:-0}" != "1" ] && command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  echo "  GPU  (+cu128 wheels)"
else
  echo "  CPU  (plain wheels from the pytorch cpu index)"
fi
echo
echo "== package names on this release =="
. /etc/os-release 2>/dev/null && echo "  $PRETTY_NAME"
if apt-cache show libglib2.0-0t64 >/dev/null 2>&1; then
  echo "  glib: libglib2.0-0t64"
else
  echo "  glib: libglib2.0-0"
fi
if apt-cache show python3.10-venv >/dev/null 2>&1; then
  echo "  venv: python3.10-venv"
else
  echo "  venv: python3-venv"
fi
echo
echo "== libGL present =="
ldconfig -p 2>/dev/null | grep -q 'libGL\.so\.1' && echo "  yes" || echo "  NO — apt install libgl1"
