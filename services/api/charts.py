"""FinBlade live-feed chart tags (docs: live-feed-chart-tags.md, schema 1).

A `finblade` key is added alongside the existing payload so a dashboard tile
knows how to draw this endpoint without a FinBlade user mapping fields by hand.
Purely additive: every existing key keeps its meaning, and a consumer that
ignores the tag is unaffected.

THREE RULES FROM THIS SYSTEM THAT THE TAG MUST NOT BREAK.

1. A number that cannot be computed is omitted, never sent as 0. Occupancy with
   no zone polygons drawn is not "zero people", it is "unknown", and a tile
   reading 0 shows an empty building. The spec agrees — `null` for a genuine
   gap, never a substituted 0 — and for a `metric`, whose value is required, the
   honest move is to drop the chart rather than invent a number for it.

2. Per-camera counts are never summed. `people_in_view` double-counts anyone
   two cameras can see. A bar chart with one bar per camera is the correct way
   to show it; a total is not, so no chart here produces one.

3. `zone_id` is unique per camera, not globally. Two cameras both have a
   ZONE-01, so labels carry the camera prefix as soon as more than one camera
   is present — otherwise the chart shows two identically named bars.

Caps come from the spec (20 charts, 8 datasets, 1000 points). We enforce them
here rather than letting FinBlade truncate, so what we send is what renders.
"""

from typing import List, Optional

SCHEMA = 1
MAX_CHARTS = 20
MAX_DATASETS = 8
MAX_POINTS = 1000

_CHART_TYPES = {"line", "bar", "pie", "doughnut", "metric", "table"}


def _num(value, default=None):
    """A real number, or `default`. Booleans are not numbers here."""
    if isinstance(value, bool) or value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):   # NaN / inf
        return default
    return int(out) if out.is_integer() else out


def _valid(chart: dict) -> bool:
    """Drop anything FinBlade would silently discard, at the source.

    Sending a chart that the far side throws away is worse than sending none:
    it looks configured and renders nothing, and the tile owner has no way to
    see why.
    """
    if not isinstance(chart, dict):
        return False
    if not chart.get("id") or chart.get("type") not in _CHART_TYPES:
        return False
    if chart["type"] == "metric":
        return _num(chart.get("value")) is not None or isinstance(
            chart.get("value"), str)
    if chart["type"] == "table":
        return bool(chart.get("columns"))
    labels = chart.get("labels")
    datasets = chart.get("datasets")
    if not isinstance(labels, list) or not labels:
        return False
    if not isinstance(datasets, list) or not datasets:
        return False
    return all(isinstance(d, dict) and isinstance(d.get("data"), list)
               and len(d["data"]) == len(labels) for d in datasets)


def _capped(chart: dict) -> dict:
    """Trim to the documented limits, keeping labels and data in step."""
    chart = dict(chart)
    if isinstance(chart.get("datasets"), list):
        chart["datasets"] = chart["datasets"][:MAX_DATASETS]
    if isinstance(chart.get("labels"), list) and len(chart["labels"]) > MAX_POINTS:
        chart["labels"] = chart["labels"][:MAX_POINTS]
        chart["datasets"] = [dict(d, data=d["data"][:MAX_POINTS])
                             for d in chart.get("datasets", [])]
    return chart


def tag(charts: List[dict]) -> Optional[dict]:
    """The `finblade` block, or None when there is nothing worth drawing.

    Returning None (and the caller omitting the key) is deliberate: an empty
    charts array would offer a FinBlade user a picker with nothing in it.
    """
    kept = [_capped(c) for c in charts if _valid(c)]
    if not kept:
        return None
    return {"schema": SCHEMA, "charts": kept[:MAX_CHARTS]}


def attach(body: dict, charts: List[dict]) -> dict:
    """Add the tag to a response body if there is anything to draw."""
    block = tag(charts)
    if block:
        body = dict(body)
        body["finblade"] = block
    return body


# --------------------------------------------------------------- builders ---
def _zone_label(zone: dict, multi_camera: bool) -> str:
    name = zone.get("zone_name") or zone.get("zone_id") or "?"
    camera = zone.get("camera_id")
    return f"{camera} / {name}" if multi_camera and camera else str(name)


def zone_charts(zones: List[dict]) -> List[dict]:
    """Occupancy, density and share, from live zone state."""
    zones = [z for z in (zones or []) if isinstance(z, dict)]
    if not zones:
        return []
    multi = len({z.get("camera_id") for z in zones}) > 1
    labels = [_zone_label(z, multi) for z in zones]
    occupancy = [_num(z.get("occupancy"), 0) for z in zones]
    density = [_num(z.get("density"), 0) for z in zones]
    inflow = [_num(z.get("inflow_per_min"), 0) for z in zones]
    outflow = [_num(z.get("outflow_per_min"), 0) for z in zones]

    charts = [
        {"id": "zone_occupancy", "type": "bar", "title": "Occupancy by zone",
         "unit": "people", "labels": labels,
         "datasets": [{"label": "Occupancy", "data": occupancy}]},
        {"id": "zone_density", "type": "bar", "title": "Density by zone",
         "unit": "per m2", "precision": 2, "labels": labels,
         "datasets": [{"label": "Density", "data": [round(d, 2) for d in density]}]},
        {"id": "zone_flow", "type": "bar", "title": "Flow by zone",
         "unit": "per min", "precision": 1, "labels": labels,
         "datasets": [{"label": "In", "data": [round(v, 1) for v in inflow]},
                      {"label": "Out", "data": [round(v, 1) for v in outflow]}]},
    ]
    # A pie of all zeros renders three slices of nothing and reads as a fault.
    if sum(occupancy) > 0:
        charts.append(
            {"id": "occupancy_share", "type": "pie", "title": "Where people are",
             "unit": "people", "labels": labels,
             "datasets": [{"label": "Occupancy", "data": occupancy}]})
    return charts


def summary_charts(body: dict) -> List[dict]:
    """Headline metrics plus a per-camera bar, from GET /api/v1/summary."""
    summary = body.get("summary") or {}
    cameras = body.get("cameras") or []
    charts: List[dict] = []

    people = summary.get("people_in_zones")
    if people is not None:
        # Only when zones exist. None means "cannot be computed", and a 0 here
        # would show an empty site while people are standing in it.
        charts.append({"id": "people_on_site", "type": "metric",
                       "title": "People on the floor", "unit": "people",
                       "value": _num(people, 0)})

    live = (body.get("counts") or {}).get("live")
    if live is not None:
        charts.append({"id": "people_live", "type": "metric",
                       "title": "Distinct people on site", "unit": "people",
                       "value": _num(live, 0)})

    alerts = summary.get("alerts") or {}
    if alerts:
        charts.append({"id": "open_alerts", "type": "metric",
                       "title": "Open alerts", "value": _num(alerts.get("open_total"), 0)})
        severities = [("Red", alerts.get("red")), ("Amber", alerts.get("amber")),
                      ("Critical", alerts.get("critical")), ("Info", alerts.get("info"))]
        present = [(k, _num(v, 0)) for k, v in severities if _num(v, 0)]
        if present:
            charts.append(
                {"id": "alerts_by_severity", "type": "doughnut",
                 "title": "Open alerts by severity",
                 "labels": [k for k, _ in present],
                 "datasets": [{"label": "Alerts", "data": [v for _, v in present]}]})

    cams = summary.get("cameras") or {}
    states = [(k.title(), _num(v, 0)) for k, v in cams.items() if _num(v, 0)]
    if states:
        charts.append({"id": "camera_health", "type": "doughnut",
                       "title": "Camera health",
                       "labels": [k for k, _ in states],
                       "datasets": [{"label": "Cameras", "data": [v for _, v in states]}]})

    # One bar PER CAMERA and never a total: people_in_view double-counts anyone
    # visible to two cameras at once.
    online = [c for c in cameras if c.get("effective_state") == "ONLINE"]
    if online:
        charts.append(
            {"id": "camera_people", "type": "bar",
             "title": "People in view, per camera (not a site total)",
             "unit": "people",
             "labels": [str(c.get("camera_id")) for c in online],
             "datasets": [{"label": "In view",
                           "data": [_num(c.get("people_in_view"), 0) for c in online]}]})
    return charts


def counts_charts(counts: dict) -> List[dict]:
    """From GET /api/v1/identity/counts."""
    counts = counts or {}
    charts: List[dict] = []
    for cid, title, unit in (("live_now", "People on site", "people"),
                             ("footfall_total", "Unique visitors", None),
                             ("cross_camera", "Seen by 2+ cameras", None)):
        key = {"live_now": "live", "footfall_total": "unique_total",
               "cross_camera": "cross_camera"}[cid]
        value = _num(counts.get(key))
        if value is not None:
            chart = {"id": cid, "type": "metric", "title": title, "value": value}
            if unit:
                chart["unit"] = unit
            charts.append(chart)

    per_camera = [c for c in (counts.get("per_camera") or []) if isinstance(c, dict)]
    if per_camera:
        charts.append(
            {"id": "live_per_camera", "type": "bar",
             "title": "Live count per camera (not a site total)", "unit": "people",
             "labels": [str(c.get("camera_id")) for c in per_camera],
             "datasets": [{"label": "Live",
                           "data": [_num(c.get("live"), 0) for c in per_camera]}]})
    return charts


def movement_charts(flows: List[dict]) -> List[dict]:
    """Zone-to-zone transitions, busiest first."""
    flows = [f for f in (flows or []) if isinstance(f, dict)]
    if not flows:
        return []
    top = sorted(flows, key=lambda f: _num(f.get("count"), 0), reverse=True)[:15]
    return [{"id": "zone_transitions", "type": "bar",
             "title": "Movement between zones", "unit": "people",
             "labels": [f"{f.get('from') or '?'} -> {f.get('to') or '?'}" for f in top],
             "datasets": [{"label": "Transitions",
                           "data": [_num(f.get("count"), 0) for f in top]}]}]


def series_charts(series: dict) -> List[dict]:
    """A zone's occupancy over time, from GET /zones/{id}/series.

    The only chart in this module whose data legitimately contains nulls, and
    the one place rule 1 has teeth: a bucket the camera could not observe is
    `null`, so the line breaks there. Substituting 0 would draw an outage as an
    empty room — the exact reading an operator must never be given.
    """
    points = [p for p in ((series or {}).get("points") or []) if isinstance(p, dict)]
    if not points:
        return []
    # Only the mean can be genuinely absent; peak is a max over observed
    # samples and is 0 or a real count.
    occupancy = [_num(p.get("occupancy")) for p in points]
    if all(v is None for v in occupancy):
        return []

    label = series.get("zone_name") or series.get("zone_id") or "zone"
    if series.get("camera_id"):
        label = f"{series['camera_id']} / {label}"
    stamps = [_num(p.get("from")) for p in points]

    charts = [{
        "id": "zone_occupancy_series", "type": "line",
        "title": f"Occupancy over time — {label}",
        "unit": "people", "precision": 2,
        "labels": stamps,
        "datasets": [{"label": "Mean occupancy",
                      "data": [None if v is None else round(v, 2)
                               for v in occupancy]}],
    }]
    peaks = [_num(p.get("peak_occupancy")) for p in points]
    if any(v for v in peaks):
        charts[0]["datasets"].append({"label": "Peak", "data": peaks})

    coverage = _num(series.get("coverage"))
    # Shown only when it is not full. A "coverage: 100%" tile on every chart is
    # noise; a "coverage: 31%" tile next to an average is the whole story.
    if coverage is not None and coverage < 0.999:
        charts.append({"id": "series_coverage", "type": "metric",
                       "title": "Window observed", "unit": "%",
                       "value": round(coverage * 100, 1)})
    return charts


def report_charts(report: dict) -> List[dict]:
    """Peak vs average occupancy per zone, from an occupancy report."""
    zones = [z for z in ((report or {}).get("zones") or []) if isinstance(z, dict)]
    if not zones:
        return []
    multi = len({z.get("camera_id") for z in zones}) > 1
    return [{"id": "occupancy_peak_avg", "type": "bar",
             "title": "Peak vs average occupancy", "unit": "people", "precision": 1,
             "labels": [_zone_label(z, multi) for z in zones],
             "datasets": [
                 {"label": "Peak", "data": [_num(z.get("peak_occupancy"), 0)
                                            for z in zones]},
                 {"label": "Average", "data": [round(_num(z.get("avg_occupancy"), 0), 1)
                                               for z in zones]}]}]
