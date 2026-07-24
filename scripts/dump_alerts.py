import sqlite3
c = sqlite3.connect("data/finblade.db")
print("by rule / frame-present:")
for row in c.execute("SELECT rule_id, (frame IS NOT NULL) AS hasframe, COUNT(*) "
                     "FROM alerts GROUP BY rule_id, hasframe ORDER BY rule_id"):
    print("  ", row)
print("distinct camera_id in alerts:",
      [r[0] for r in c.execute("SELECT DISTINCT camera_id FROM alerts")])
print("sample 6 alerts (rule, ts, frame, kind):")
for row in c.execute("SELECT rule_id, ts, frame, kind FROM alerts ORDER BY ts DESC LIMIT 6"):
    print("  ", row)
print("total alerts:", c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
