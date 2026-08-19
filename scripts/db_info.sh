#!/usr/bin/env bash
# What is actually in the database, and how fast is it growing?
cd "$(dirname "$0")/.." || exit 1
DB="${FINBLADE_DB:-data/finblade.db}"

if [ ! -f "$DB" ]; then
  echo "no database at $DB (created on first API start)"
  exit 0
fi

echo "== file =="
ls -lh "$DB" | awk '{print "  path:", $9, "\n  size:", $5}'

.venv/bin/python - "$DB" <<'PY'
import os, sqlite3, sys, time
db = sys.argv[1]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

print("\n== engine ==")
for pragma in ("journal_mode", "synchronous", "page_size"):
    print(f"  {pragma:14} {conn.execute(f'PRAGMA {pragma}').fetchone()[0]}")

print("\n== tables ==")
rows = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
now = time.time()
for (name,) in rows:
    if name.startswith("sqlite_"):
        continue
    n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    span = ""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
    tscol = "ts" if "ts" in cols else ("generated_at" if "generated_at" in cols else None)
    if tscol and n:
        lo, hi = conn.execute(
            f"SELECT MIN({tscol}), MAX({tscol}) FROM {name}").fetchone()
        if lo and hi:
            days = (hi - lo) / 86400.0
            span = f"  spanning {days:.1f} days, newest {(now - hi)/3600:.1f}h ago"
    print(f"  {name:20} {n:>10,} rows{span}")

print("\n== growth drivers ==")
for name, note in (("zone_state_ts", "one row per zone every 5s"),
                   ("events", "one row per zone entry/exit/transition")):
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        lo, hi = conn.execute(f"SELECT MIN(ts), MAX(ts) FROM {name}").fetchone()
        if n and lo and hi and hi > lo:
            per_day = n / max((hi - lo) / 86400.0, 1e-9)
            print(f"  {name:16} {per_day:>12,.0f} rows/day   ({note})")
    except sqlite3.Error:
        pass

size = os.path.getsize(db)
print(f"\n  {size/1e6:.1f} MB total. No retention job runs by default —")
print("  DEPLOY.md records two cameras reaching 380 MB / 569k events in a day.")
PY
