"""The reporting day a headline count belongs to: 06:00-18:00, site-local.

Every number on the front of the dashboard answers a question about a DAY —
"how many people came in today" — and none of them answer "how many since this
process last restarted". Those two agreed only by accident, on the day the
service happened to start at breakfast.

THE RULE, and the one case that makes it worth writing down.

The window is the site's opening hours on the day being reported. Before
opening, that day has not started yet, so the day being reported is YESTERDAY:
at 01:00 the honest answer to "how many visitors today" is last business day's
figure, not a count of the seven hours of darkness since midnight. A window
running 00:00-01:00 would show a near-empty building to a night operator and
read as an outage.

Inside opening hours the window is clipped to now. It ends at 18:00 rather than
running to the end of the day, because a figure labelled "today" that keeps
climbing after closing is a figure nobody can reconcile against a door count.

TIMEZONE. Site-local, not UTC and not the server's. A site in Riyadh reporting
on a UTC day splits its afternoon across two "days", and the 06:00 boundary
lands at 09:00 local. Saudi Arabia has no daylight saving, so the fixed +03:00
fallback below is exact there; it is a fallback only for a host with no tzdata
installed, where zoneinfo raises rather than returning a wrong offset silently.
"""

import datetime as _dt
from typing import Optional

# Asia/Riyadh. Named, not hard-coded as +03:00, so a site in another timezone is
# a config change rather than an arithmetic one.
DEFAULT_TZ = "Asia/Riyadh"
DEFAULT_START_HOUR = 6
DEFAULT_END_HOUR = 18

# Used only when the host has no timezone database. Correct for KSA, which has
# never observed DST; wrong for anywhere that does, which is why it is not the
# primary path.
_FALLBACK_OFFSET_HOURS = 3.0


def _zone(tz: str):
    """A tzinfo for `tz`, or the fixed KSA offset if tzdata is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:                                        # noqa: BLE001
        return _dt.timezone(_dt.timedelta(hours=_FALLBACK_OFFSET_HOURS))


def business_window(now: float, tz: str = DEFAULT_TZ,
                    start_hour: int = DEFAULT_START_HOUR,
                    end_hour: int = DEFAULT_END_HOUR) -> dict:
    """The window a count taken at `now` should cover.

    Returns from/to as epoch seconds plus the local date they describe, so a
    caller can label the figure with the day it belongs to instead of guessing
    from the timestamps.

    `complete` says whether the window has closed. A partial window is not a
    smaller day, it is a day still in progress, and a report that does not
    distinguish them invites comparing this morning against last Tuesday.
    """
    if not 0 <= start_hour < end_hour <= 24:
        raise ValueError("need 0 <= start_hour < end_hour <= 24")

    zone = _zone(tz)
    local = _dt.datetime.fromtimestamp(now, zone)

    # Before opening, the day being reported is the previous one — see the
    # module docstring. This is the whole reason the helper exists.
    day = local.date()
    if local.hour < start_hour:
        day = day - _dt.timedelta(days=1)

    opened = _dt.datetime.combine(day, _dt.time(hour=start_hour), tzinfo=zone)
    # hour=24 is not a valid time; midnight the following day is the same instant.
    if end_hour == 24:
        closes = _dt.datetime.combine(day + _dt.timedelta(days=1),
                                      _dt.time(hour=0), tzinfo=zone)
    else:
        closes = _dt.datetime.combine(day, _dt.time(hour=end_hour), tzinfo=zone)

    t0 = opened.timestamp()
    close_ts = closes.timestamp()
    # Clipped to now while the day is still running: the window describes time
    # that has actually happened.
    t1 = min(close_ts, now)
    return {
        "from": t0,
        "to": t1,
        "date": day.isoformat(),
        "tz": tz,
        "start_hour": start_hour,
        "end_hour": end_hour,
        "complete": now >= close_ts,
        "closes_at": close_ts,
    }


def resolve_window(now: float, frm: Optional[float] = None,
                   to: Optional[float] = None, tz: str = DEFAULT_TZ,
                   start_hour: int = DEFAULT_START_HOUR,
                   end_hour: int = DEFAULT_END_HOUR) -> dict:
    """`business_window`, unless the caller named an explicit range.

    An explicit from/to wins so "how many people last Tuesday" stays askable.
    Such a window carries `date: None` — an arbitrary range does not describe
    one business day and labelling it with one would be a guess.
    """
    if frm is None and to is None:
        return business_window(now, tz=tz, start_hour=start_hour,
                               end_hour=end_hour)
    t0 = float(frm) if frm is not None else 0.0
    t1 = float(to) if to is not None else now
    return {"from": t0, "to": t1, "date": None, "tz": tz,
            "start_hour": None, "end_hour": None,
            "complete": now >= t1, "closes_at": t1}
