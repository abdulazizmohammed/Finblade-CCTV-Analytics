"""Every time on screen is the SITE's time (web/sitetime.js).

The pages formatted with toLocaleTimeString() and no timezone, which renders
in the VIEWER's zone. Two failures, and the second is worse than the first:

  * the same incident read 14:02 in Riyadh and 11:02 in London, with nothing
    on screen saying which;
  * a date picker turned "2026-10-11" into the viewer's midnight, so a report
    run from abroad covered a window shifted by hours — wrong records, not a
    wrong label.

The behaviour tests run the shipped module under node, with TZ set to a
DIFFERENT zone than the site's, which is the whole point: a helper that reads
the host clock passes in Riyadh and fails in London. Skipped without node; the
static guards below need no runtime, so the rule stays enforced anywhere.
"""

import datetime as dt
import json
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO, "web")
SITETIME = os.path.join(WEB, "sitetime.js")

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# Every page that puts a timestamp on screen.
TIME_PAGES = ("dashboard.html", "ops.html", "map.html", "history.html",
              "report.html", "webhooks.html")

# 2026-10-11 09:00:00 UTC — 12:00 in Riyadh, 10:00 in London (still BST).
TS = dt.datetime(2026, 10, 11, 9, 0, tzinfo=dt.timezone.utc).timestamp()


def _page(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as fh:
        return fh.read()


def run_js(expr, tz="Asia/Riyadh", host_tz="America/New_York"):
    """Evaluate against the shipped module, with node's own clock elsewhere."""
    src = open(SITETIME, encoding="utf-8").read()
    script = f"{src}\nsetSiteTz({json.dumps(tz)});\nconsole.log(String({expr}));\n"
    env = dict(os.environ, TZ=host_tz)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


# --- behaviour --------------------------------------------------------------

@node
def test_a_time_renders_in_the_site_zone_not_the_hosts():
    assert run_js(f"fbTime({TS})") == "12:00:00"
    assert run_js(f"fbTime({TS})", tz="Europe/London") == "10:00:00"
    assert run_js(f"fbHM({TS})") == "12:00"
    assert run_js(f"fbDateTime({TS})") == "2026-10-11 12:00:00"
    assert run_js(f"fbDateTime({TS}, false)") == "2026-10-11 12:00"


@node
def test_the_date_is_the_sites_date_across_the_midnight_boundary():
    # 2026-10-11 22:30 UTC is already the 12th in Riyadh, still the 11th in
    # London — the case that splits a night's records across two days.
    late = dt.datetime(2026, 10, 11, 22, 30, tzinfo=dt.timezone.utc).timestamp()
    assert run_js(f"fbDate({late})") == "2026-10-12"
    assert run_js(f"fbDate({late})", tz="Europe/London") == "2026-10-11"


@node
def test_a_picked_date_is_site_local_midnight_to_midnight():
    start = float(run_js('fbDayStart("2026-10-11")'))
    end = float(run_js('fbDayEnd("2026-10-11")'))
    # Riyadh midnight is 21:00 UTC the previous day.
    assert dt.datetime.fromtimestamp(start, dt.timezone.utc).strftime("%Y-%m-%d %H:%M") \
        == "2026-10-10 21:00"
    assert round(end - start) == 86400, "a whole day"
    # ...and the same date in another zone is a different instant, which is
    # exactly what the old browser-local conversion got wrong.
    uk = float(run_js('fbDayStart("2026-10-11")', tz="Europe/London"))
    assert uk - start == 2 * 3600, "Riyadh's day starts two hours before London's that week"
    assert run_js('String(fbDayStart("not-a-date"))') == "null"


@node
def test_an_absent_timestamp_renders_as_a_dash_not_nineteen_seventy():
    """A missing time is unknown. Formatting it as the epoch would print 1970
    into an operator's incident log as though it were a reading. 0 counts as
    missing: every formatter this replaced wrote `ts ? format(ts) : '—'`."""
    for expr in ("fbTime(null)", "fbTime('')", "fbTime(0)", "fbDateTime(null)",
                 "fbDateTime(0)", "fbHM(null)", "fbHM(0)"):
        assert run_js(expr) == "—", expr


@node
def test_the_zone_label_says_whose_time_it_is():
    assert run_js("fbTzLabel()") == "GMT+3"
    assert run_js("fbTzLabel()", tz="UTC") in ("GMT", "GMT+0")


# --- the day-boundary algorithm, without a JS engine ------------------------
# fbDayStart converts a site-local date to an epoch by measuring the zone's
# offset and subtracting it, twice — the second pass is what gets a daylight
# -saving boundary right. The dev box has no JS runtime, so the ALGORITHM is
# re-stated here against zoneinfo: if the two-pass trick is wrong, this fails
# whether or not node is installed. (It does not prove the JS parses; the node
# tests above do that wherever a runtime exists.)

def _py_day_start(date_str, tz, passes=2, correct=True):
    """fbDayStart, transliterated. Mirrors the JS line for line."""
    from zoneinfo import ZoneInfo
    zone = ZoneInfo(tz)

    def offset_ms(at_ms):
        d = dt.datetime.fromtimestamp(at_ms / 1000, dt.timezone.utc)
        as_site = d.astimezone(zone).replace(tzinfo=None)
        as_utc = d.replace(tzinfo=None)
        return (as_site - as_utc).total_seconds() * 1000

    def date_in(ms):
        return dt.datetime.fromtimestamp(ms / 1000, zone).strftime("%Y-%m-%d")

    utc_midnight = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(
        tzinfo=dt.timezone.utc).timestamp() * 1000
    t = utc_midnight
    for _ in range(passes):
        t = utc_midnight - offset_ms(t)
    if correct:
        i = 0
        while i < 8 and date_in(t) < date_str:
            t += 15 * 60 * 1000
            i += 1
    return t / 1000


@pytest.mark.parametrize("tz,date", [
    ("Asia/Riyadh", "2026-10-11"),        # no DST at all — the Wareed case
    ("Europe/London", "2026-10-11"),      # BST
    ("Europe/London", "2026-03-29"),      # clocks forward
    ("Europe/London", "2026-10-25"),      # clocks back
    ("America/New_York", "2026-11-01"),   # clocks back
    ("Asia/Beirut", "2026-03-29"),        # one pass is an hour out here
    ("Pacific/Chatham", "2026-09-27"),    # ...and here
    ("Australia/Lord_Howe", "2026-10-04"),  # half-hour DST
    ("America/Santiago", "2026-09-06"),   # clocks forward AT midnight
    ("America/Havana", "2026-03-08"),     # ditto
])
def test_a_picked_date_starts_where_the_tz_database_says_it_does(tz, date):
    from zoneinfo import ZoneInfo
    want = dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz)).timestamp()
    assert _py_day_start(date, tz) == want, f"{tz} {date}"


def _want(date, tz):
    from zoneinfo import ZoneInfo
    return dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz)).timestamp()


def test_each_refinement_earns_its_place():
    """Measured, not assumed. Sweeping all 599 zones over every day of 2026:
    one pass plus the correction is wrong 33 times, two passes without the
    correction 5 times, and the two together are wrong nowhere. So neither
    refinement is cargo cult — these are the days each one fixes."""
    # The second pass is what these need: the offset moves between UTC
    # midnight and the candidate instant, and the correction cannot reach it
    # because the first guess lands INSIDE the right date, just not at its start.
    for tz, date in (("America/Nuuk", "2026-03-29"), ("America/Scoresbysund", "2026-03-29"),
                     ("Antarctica/Macquarie", "2026-04-05"), ("Asia/Gaza", "2026-10-24")):
        assert _py_day_start(date, tz, passes=1) != _want(date, tz), \
            f"{tz} {date}: one pass would have done, drop the second"
        assert _py_day_start(date, tz) == _want(date, tz)

    # The correction is what these need: the clocks go forward AT midnight, so
    # that midnight never happens and the arithmetic lands just BEFORE the day
    # starts — a day query would otherwise swallow an hour of the previous day.
    for tz, date in (("America/Havana", "2026-03-08"), ("America/Santiago", "2026-09-06"),
                     ("Atlantic/Azores", "2026-03-29")):
        assert _py_day_start(date, tz, correct=False) < _want(date, tz)
        assert _py_day_start(date, tz) == _want(date, tz)


def test_the_day_boundary_is_exact_in_every_zone_through_a_whole_year():
    """The sweep itself, trimmed to the days that can go wrong (every zone's
    transition days plus the turn of the year) so it stays quick."""
    from zoneinfo import ZoneInfo, available_timezones
    checked = 0
    for tz in sorted(available_timezones()):
        try:
            zone = ZoneInfo(tz)
        except Exception:                       # noqa: BLE001  (an incomplete tzdata)
            continue
        day = dt.date(2026, 1, 1)
        prev = None
        while day < dt.date(2027, 1, 1):
            off = dt.datetime.combine(day, dt.time(12), tzinfo=zone).utcoffset()
            if off != prev:                     # a transition on or near this day
                for d in (day - dt.timedelta(days=1), day, day + dt.timedelta(days=1)):
                    iso = d.isoformat()
                    assert _py_day_start(iso, tz) == _want(iso, tz), f"{tz} {iso}"
                    checked += 1
                prev = off
            day += dt.timedelta(days=1)
    assert checked > 500, f"the sweep covered only {checked} days — did tzdata load?"


# --- static guards ----------------------------------------------------------

@pytest.mark.parametrize("page", TIME_PAGES)
def test_pages_load_the_shared_module(page):
    assert 'src="sitetime.js"' in _page(page), f"{page} shows times but does not load sitetime.js"


@pytest.mark.parametrize("page", TIME_PAGES)
def test_no_page_formats_a_time_in_the_viewers_zone(page):
    """REGRESSION GUARD. toLocale*String() without a timeZone renders in the
    browser's zone — the bug this module exists to fix. If a page needs one,
    it passes timeZone explicitly (fbDate does, via Intl)."""
    src = _page(page)
    for call in ("toLocaleTimeString(", "toLocaleString("):
        for idx in _occurrences(src, call):
            frag = src[idx:idx + 160]
            assert "timeZone" in frag, f"{page}: {call} with no timeZone — {frag[:90]!r}"


def _occurrences(src, needle):
    i = src.find(needle)
    while i != -1:
        yield i
        i = src.find(needle, i + 1)


def test_the_clock_pages_show_the_zone_beside_the_time():
    """A bare clock on a wall screen invites the reader's own timezone."""
    for page in ("dashboard.html", "ops.html"):
        src = _page(page)
        assert "fbTzLabel()" in src, f"{page} paints a clock with no zone label"


def test_the_api_tells_the_pages_which_zone_the_site_is_in():
    """The default in the module is only a default; the server is the source."""
    with open(os.path.join(REPO, "services", "api", "app.py"), encoding="utf-8") as fh:
        src = fh.read()
    body = src[src.index("async def org_index("):]
    body = body[:body.index("@app.post")]
    assert "timezone=BUSINESS_TZ" in body
