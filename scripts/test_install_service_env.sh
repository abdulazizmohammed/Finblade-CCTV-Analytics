#!/usr/bin/env bash
# Exercise the .env handling in install_service.sh without root or systemd.
# Runs against a throwaway copy of the repo layout.
set -u
cd "$(dirname "$0")/.." || exit 1
SRC="$(pwd -P)"

TMP=$(mktemp -d /tmp/fbenv_XXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/scripts" "$TMP/deploy" "$TMP/.venv/bin"
cp "$SRC/scripts/install_service.sh" "$TMP/scripts/"
cp "$SRC/deploy/finblade-api.service" "$TMP/deploy/"
# Stand-in for the venv python: only used to generate keys.
cat > "$TMP/.venv/bin/python" <<'PY'
#!/usr/bin/env bash
echo "GENERATED-$RANDOM"
PY
chmod +x "$TMP/.venv/bin/python"

run() { (cd "$TMP" && FINBLADE_ENV_ONLY=1 "$@" bash scripts/install_service.sh >/dev/null 2>&1); }
val() { grep "^$1=" "$TMP/.env" | head -1 | cut -d= -f2-; }
ok=0; fail=0
check() {  # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok=$((ok+1)); printf '  ok   %s\n' "$1"
  else fail=$((fail+1)); printf '  FAIL %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; fi
}

echo "== fresh install generates keys =="
run env
G1=$(val FINBLADE_API_KEY)
[ -n "$G1" ] && check "api key generated" "yes" "yes" || check "api key generated" "yes" "no"
check "autostart on by default" "1" "$(val FINBLADE_AUTOSTART_CAMERAS)"

echo
echo "== a supplied key is written verbatim =="
KEY='fVBkSkKx2sugzyXpg-AkVVoynyvLOEI2Ig8VPDck8X0'
run env FINBLADE_INTEGRATION_KEY="$KEY"
check "integration key set" "$KEY" "$(val FINBLADE_INTEGRATION_KEY)"
check "operator key untouched" "$G1" "$(val FINBLADE_API_KEY)"

echo
echo "== re-running replaces, never duplicates =="
run env FINBLADE_INTEGRATION_KEY="$KEY"
check "single line only" "1" "$(grep -c '^FINBLADE_INTEGRATION_KEY=' "$TMP/.env")"

echo
echo "== rotation overwrites the old value =="
run env FINBLADE_INTEGRATION_KEY="second-key-value"
check "rotated" "second-key-value" "$(val FINBLADE_INTEGRATION_KEY)"
check "still one line" "1" "$(grep -c '^FINBLADE_INTEGRATION_KEY=' "$TMP/.env")"

echo
echo "== keys containing sed metacharacters survive =="
run env FINBLADE_INTEGRATION_KEY='a/b&c|d\e.f'
check "metacharacters intact" 'a/b&c|d\e.f' "$(val FINBLADE_INTEGRATION_KEY)"

echo
echo "== other settings =="
run env FINBLADE_SITE_ID=SITE-99 FINBLADE_PORT=9000
check "site id" "SITE-99" "$(val FINBLADE_SITE_ID)"
check "port" "9000" "$(val FINBLADE_PORT)"

echo
echo "== permissions =="
check "chmod 600" "600" "$(stat -c '%a' "$TMP/.env")"

echo
echo "$ok passed, $fail failed"
[ "$fail" -eq 0 ]
