#!/usr/bin/env bash
# Does the handover guide still match the API it describes?
cd "$(dirname "$0")/.." || exit 1
PORT="${PORT:-8000}"

echo "== document sizes =="
wc -l docs/FINBLADE_CLIENT_GUIDE.md docs/FINBLADE_API_REQUIREMENTS.md \
      integrations/finblade_ai/README.md

echo
echo "== route count claimed in the guide =="
grep -n 'routes\*\*' docs/FINBLADE_CLIENT_GUIDE.md || echo "  (no claim found)"

echo
echo "== routes served vs documented =="
curl -s -m 15 "localhost:$PORT/openapi.json" -o /tmp/spec.json || {
  echo "  API not running on :$PORT"; exit 1; }
.venv/bin/python -c "
import json, re
spec = json.load(open('/tmp/spec.json'))
served = sorted(spec['paths'])
guide = open('docs/FINBLADE_CLIENT_GUIDE.md').read()

# Normalise every path parameter to {} on BOTH sides. The spec says
# {camera_id} where the guide says {id}; comparing them literally, or
# stripping the braces and leaving a // behind, reports routes as missing
# when they are documented. A check that cries wolf gets ignored.
norm = lambda s: re.sub(r'\{[^}]*\}', '{}', s)
doc = norm(guide)


def documented(path):
    if norm(path) in doc:
        return True
    # The guide groups related routes in shorthand — '/start' · '/stop',
    # 'resolve' · 'release' · 'merge' — so the full path never appears for the
    # second and later members. Fall back to the last literal segment, which is
    # loose but still catches the case that matters: a NEW route whose name
    # appears nowhere in the document.
    tail = [seg for seg in path.split('/') if seg and not seg.startswith('{')]
    return bool(tail) and tail[-1] in guide


print('%d paths served' % len(served))
missing = [p for p in served if not documented(p)]
print()
print('NOT mentioned anywhere in the client guide:')
for p in missing:
    print('  ', p)
if not missing:
    print('   (none — every served route is documented)')
" </dev/null
