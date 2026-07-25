"""Occupancy report renderer (UC-50 / R-08).

Pure Python -> HTML string, so it is unit-testable and needs no browser. Uses
the FinBlade theme via a linked stylesheet + CSS variables; NEVER hard-codes a
colour and keeps all numerals tabular (.fb-num). Status maps by rule:
NORMAL -> --fb-ok (grey, no green), AMBER -> --fb-warning, RED -> --fb-critical.
"""

import html
import time
from typing import List

_STATUS_PILL = {
    "NORMAL": "fb-pill--normal",
    "WARNING": "fb-pill--warning",
    "CRITICAL": "fb-pill--critical",
}


def render_report_html(zone_states: List[dict], generated_at: float,
                       theme_href: str = "/web/finblade-theme.css") -> str:
    ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(generated_at))
    total = sum(int(z.get("occupancy", 0)) for z in zone_states)

    rows = []
    for z in sorted(zone_states, key=lambda z: z.get("zone_id", "")):
        status = str(z.get("status", "NORMAL")).upper()
        pill = _STATUS_PILL.get(status, "fb-pill--normal")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(z.get('zone_id','')))}</td>"
            f"<td class='fb-num'>{int(z.get('occupancy',0))}</td>"
            f"<td class='fb-num'>{float(z.get('density',0.0)):.2f}</td>"
            f"<td class='fb-num'>{float(z.get('capacity_pct',0.0)):.0f}%</td>"
            f"<td class='fb-num'>{float(z.get('inflow_per_min',0.0)):.1f}</td>"
            f"<td class='fb-num'>{float(z.get('outflow_per_min',0.0)):.1f}</td>"
            f"<td><span class='fb-pill {pill}'>{html.escape(status)}</span></td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FinBlade — Occupancy Report</title>
<link rel="stylesheet" href="{html.escape(theme_href)}">
<style>
  .rpt {{ max-width: 900px; margin: 24px auto; padding: 0 16px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--fb-line); }}
  th {{ color: var(--fb-text-muted); font-family: var(--fb-font-data);
       font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }}
  td {{ color: var(--fb-text); }}
</style></head>
<body><div class="rpt fb-panel fb-bracket">
  <p class="fb-eyebrow">FinBlade · Occupancy Report</p>
  <p class="fb-timecode">{ts_str} UTC</p>
  <p>Total occupancy across zones: <span class="fb-num">{total}</span></p>
  <table>
    <thead><tr>
      <th>Zone</th><th>Occ</th><th>Density /m²</th><th>Capacity</th>
      <th>In/min</th><th>Out/min</th><th>Status</th>
    </tr></thead>
    <tbody>
      {''.join(rows) if rows else '<tr><td colspan="7">No zone data yet.</td></tr>'}
    </tbody>
  </table>
</div></body></html>"""
