"""Reading a sparse zone history: buckets, instants, and durations.

Part B. Step 4 made zone_state_ts write only on change, which is right for
storage and wrong for anyone who assumed a row every five seconds. Three
questions a chatbot asks constantly stopped being answerable by a plain SELECT:

    "show me the last six hours"      -> bucket_series
    "how busy was it at 3pm"          -> state_at
    "how long was the lobby full"     -> duration_where

All three come down to the same rule: A SAMPLE DESCRIBES EVERY MOMENT UNTIL THE
NEXT SAMPLE. A gap means nothing changed, not that nothing was observed. Get
that wrong and a quiet night reads as missing data, or worse, as zero people
counted once instead of zero people for eight hours.

WHAT SEPARATES "NOTHING CHANGED" FROM "WE WERE NOT WATCHING". Nothing, inside
this table — both are an absence of rows. Two things outside it resolve the
ambiguity, and both are used here:

  * CAMERA_OFFLINE / CAMERA_ONLINE in the event log mark outages the system
    noticed.
  * A killed worker announces nothing at all, so `max_hold` bounds how long a
    single sample is allowed to speak for. Past that the time is unknown. The
    keepalive write (default 300s) is what makes this measurable: with a write
    guaranteed at that interval, a materially longer gap was not a quiet zone.

Everything here reports `coverage` and `gaps` rather than quietly filling.
A confident wrong number is the failure mode that matters — an operator asking
"was anyone in the loading bay last night" must not be told "no" when the
answer is "the camera was down".
"""

from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .timeweight import merge_intervals, time_weighted

# A bucket boundary sits on a multiple of the bucket size in epoch seconds, not
# on the caller's window start. Two chatbot turns asking for "the last hour" a
# minute apart otherwise return buckets covering different spans, and the second
# answer contradicts the first for no reason the user can see.
ALIGN_TO_EPOCH = True

NO_DATA = "no_data"
CAMERA_OFFLINE = "camera_offline"


def align_down(ts: float, bucket_s: float) -> float:
    return (ts // bucket_s) * bucket_s if ALIGN_TO_EPOCH and bucket_s > 0 else ts


def bucket_bounds(t0: float, t1: float, bucket_s: float) -> List[Tuple[float, float]]:
    """Bucket edges spanning [t0, t1], aligned to the epoch.

    The first and last buckets are NOT clipped to the window: a bucket is a
    fixed span of clock time, and clipping makes the edge buckets cover less
    time than the middle ones while looking identical in the output. A caller
    plotting them would see a dip at each end that is an artifact of the query.
    """
    if bucket_s <= 0 or t1 <= t0:
        return []
    start = align_down(t0, bucket_s)
    edges = []
    edge = start
    while edge < t1:
        edges.append((edge, edge + bucket_s))
        edge += bucket_s
    return edges


def find_gaps(samples: Sequence[dict], t0: float, t1: float,
              offline: Iterable[Tuple[float, float]] = (),
              max_hold: Optional[float] = None,
              prior: Optional[dict] = None) -> List[dict]:
    """Stretches of the window with no trustworthy observation.

    Two kinds, reported separately because they mean different things to
    whoever is asking. `camera_offline` is a known outage — the system saw the
    camera go and said so. `no_data` is silence longer than a sample is allowed
    to speak for, which usually means a worker died without announcing it.

    An offline period wins where the two overlap: it is the more specific
    explanation, and reporting the same seconds twice would make the gap totals
    exceed the window.
    """
    known = merge_intervals(offline)
    unknown: List[Tuple[float, float]] = []

    if max_hold and max_hold > 0:
        ordered = sorted(samples, key=lambda r: float(r.get("ts") or 0))
        if prior is not None:
            ordered = [prior] + ordered
        if not ordered:
            unknown.append((t0, t1))
        else:
            first = max(float(ordered[0].get("ts") or 0), t0)
            if first > t0:
                unknown.append((t0, first))
            for i, row in enumerate(ordered):
                start = max(float(row.get("ts") or 0), t0)
                end = min(float(ordered[i + 1].get("ts") or 0)
                          if i + 1 < len(ordered) else t1, t1)
                if end - start > max_hold:
                    unknown.append((start + max_hold, end))
    elif not samples and prior is None:
        # With no max_hold there is nothing to measure silence against, so the
        # only detectable gap is a window with no data whatsoever.
        unknown.append((t0, t1))

    out = [{"from": a, "to": b, "seconds": round(b - a, 3),
            "reason": CAMERA_OFFLINE} for a, b in known if b > a]
    for a, b in merge_intervals(unknown):
        for piece in _subtract(a, b, known):
            out.append({"from": piece[0], "to": piece[1],
                        "seconds": round(piece[1] - piece[0], 3),
                        "reason": NO_DATA})
    return sorted(out, key=lambda g: g["from"])


def _subtract(a0: float, a1: float,
              intervals: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """[a0, a1) minus a sorted, merged interval list."""
    pieces = []
    cursor = a0
    for b0, b1 in intervals:
        if b1 <= cursor:
            continue
        if b0 >= a1:
            break
        if b0 > cursor:
            pieces.append((cursor, min(b0, a1)))
        cursor = max(cursor, b1)
        if cursor >= a1:
            break
    if cursor < a1:
        pieces.append((cursor, a1))
    return [(x, y) for x, y in pieces if y > x]


def bucket_series(samples: Sequence[dict], t0: float, t1: float, bucket_s: float,
                  fields: Sequence[str], prior: Optional[dict] = None,
                  offline: Iterable[Tuple[float, float]] = (),
                  max_hold: Optional[float] = None) -> List[dict]:
    """One entry per bucket, each time-weighted within its own span.

    A bucket that observed nothing reports None for every field and coverage
    0.0 — NOT zero. Zero is a measurement, and a chart that draws an outage as
    an empty room is worse than a chart with a hole in it.

    A sample from before a bucket carries into it, which is the whole point:
    under write-on-change most buckets contain no row at all and are described
    entirely by the last row before them.
    """
    bounds = bucket_bounds(t0, t1, bucket_s)
    if not bounds:
        return []

    ordered = sorted(samples, key=lambda r: float(r.get("ts") or 0))

    # Staleness is resolved ONCE across the whole span, not per bucket.
    #
    # Handing max_hold to each bucket separately re-grants the allowance at
    # every boundary: the carried-forward sample is clamped to the bucket's own
    # start, so a reading from a worker that died hours ago looks max_hold
    # seconds old in every bucket forever. Caught by the test that kills a
    # worker and expects holes — it produced a flat line of 9s instead.
    #
    # Doing it here also makes bucket_series and find_gaps agree by
    # construction, which matters because a response carries both.
    span0, span1 = bounds[0][0], bounds[-1][1]
    with_prior = ([prior] if prior is not None else []) + ordered
    known = merge_intervals(
        list(offline) + _stale_intervals(with_prior, span0, span1, max_hold))

    out = []
    # Walk the samples once, carrying the last row before each bucket forward,
    # rather than re-scanning the list per bucket. A day of 1-minute buckets is
    # 1,440 of them, and the quadratic version is felt.
    idx = 0
    carried = prior
    for b0, b1 in bounds:
        while idx < len(ordered) and float(ordered[idx].get("ts") or 0) < b0:
            carried = ordered[idx]
            idx += 1
        inside = []
        j = idx
        while j < len(ordered) and float(ordered[j].get("ts") or 0) < b1:
            inside.append(ordered[j])
            j += 1
        # max_hold is deliberately NOT passed down: `known` already carries the
        # stale intervals, computed across the whole span above.
        stats = time_weighted(inside, b0, b1, fields, unknown=known,
                              prior=carried)
        entry = {"from": b0, "to": b1, "coverage": stats["coverage"],
                 "samples": len(inside)}
        for field in fields:
            f = stats["fields"][field]
            entry[field] = f["mean"]
            if field == "occupancy":
                entry["peak_occupancy"] = f["peak"]
        out.append(entry)
    return out


def state_at(samples: Sequence[dict], ts: float, prior: Optional[dict] = None,
             max_hold: Optional[float] = None) -> Optional[dict]:
    """The reading in force at an instant: the last sample at or before it.

    Returns None when nothing describes that moment, rather than the nearest
    row in either direction. Interpolating, or reaching forward to the next
    sample, would invent a reading for a time nobody observed — and under
    write-on-change the next sample can be hours later.

    The returned dict carries `age_seconds` (how old the reading was at that
    instant) and `stale` (whether it exceeded max_hold), so a caller can say
    "4 people, as of a reading 3 hours old" instead of just "4 people".
    """
    best = prior
    for row in sorted(samples, key=lambda r: float(r.get("ts") or 0)):
        if float(row.get("ts") or 0) <= ts:
            best = row
        else:
            break
    if best is None:
        return None
    age = ts - float(best.get("ts") or 0)
    stale = bool(max_hold and max_hold > 0 and age > max_hold)
    return dict(best, age_seconds=round(age, 3), stale=stale, at=ts)


def duration_where(samples: Sequence[dict], t0: float, t1: float,
                   predicate: Callable[[dict], bool],
                   prior: Optional[dict] = None,
                   offline: Iterable[Tuple[float, float]] = (),
                   max_hold: Optional[float] = None) -> dict:
    """How long the predicate held, plus the individual episodes.

    "How long was the lobby over capacity" is a duration question, and answering
    it by counting matching rows is meaningless once rows no longer represent
    equal time — one row can stand for four hours.

    Time the camera could not observe is excluded from the total AND from the
    episodes, so an outage cannot be reported as a four-hour breach. It is
    counted in `unobserved_seconds` instead, which is the honest way to say
    "at least this long, and I could not see 40 minutes of it".
    """
    ordered = sorted(samples, key=lambda r: float(r.get("ts") or 0))
    if prior is not None:
        ordered = [prior] + ordered

    blind = merge_intervals(list(offline) + _stale_intervals(ordered, t0, t1, max_hold))

    matched: List[Tuple[float, float]] = []
    for i, row in enumerate(ordered):
        start = max(float(row.get("ts") or 0), t0)
        end = min(float(ordered[i + 1].get("ts") or 0)
                  if i + 1 < len(ordered) else t1, t1)
        if end <= start or not predicate(row):
            continue
        matched.extend(_subtract(start, end, blind))

    episodes = merge_intervals(matched)
    total = sum(b - a for a, b in episodes)
    window = max(t1 - t0, 0.0)
    unobserved = sum(min(b, t1) - max(a, t0) for a, b in blind
                     if min(b, t1) > max(a, t0))
    return {
        "from": t0, "to": t1,
        "total_seconds": round(total, 3),
        "episodes": [{"from": a, "to": b, "seconds": round(b - a, 3)}
                     for a, b in episodes],
        "episode_count": len(episodes),
        "longest_seconds": round(max((b - a for a, b in episodes), default=0.0), 3),
        "unobserved_seconds": round(min(unobserved, window), 3),
        "coverage": round(max(window - unobserved, 0.0) / window, 4) if window else None,
    }


def _stale_intervals(ordered: Sequence[dict], t0: float, t1: float,
                     max_hold: Optional[float]) -> List[Tuple[float, float]]:
    if not max_hold or max_hold <= 0:
        return []
    if not ordered:
        return [(t0, t1)]
    out = []
    first = max(float(ordered[0].get("ts") or 0), t0)
    if first > t0:
        out.append((t0, first))
    for i, row in enumerate(ordered):
        start = max(float(row.get("ts") or 0), t0)
        end = min(float(ordered[i + 1].get("ts") or 0)
                  if i + 1 < len(ordered) else t1, t1)
        if end - start > max_hold:
            out.append((start + max_hold, end))
    return out


# Comparisons a caller may ask for by name. Kept to a closed set rather than
# accepting an expression: this is reachable from an LLM deciding its own
# arguments, and "evaluate whatever string arrives" is not a thing to build
# into an API that also serves a security system.
OPERATORS: Dict[str, Callable[[float, float], bool]] = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}

COMPARABLE_FIELDS = ("occupancy", "density", "capacity_pct")


def field_predicate(field: str, op: str, value: float) -> Callable[[dict], bool]:
    """A predicate over one numeric field. Raises ValueError on anything
    outside the closed sets above — a typo must fail loudly, not match nothing
    and report zero seconds, which is indistinguishable from a real answer."""
    if field not in COMPARABLE_FIELDS:
        raise ValueError(f"field must be one of {list(COMPARABLE_FIELDS)}")
    if op not in OPERATORS:
        raise ValueError(f"op must be one of {sorted(OPERATORS)}")
    compare = OPERATORS[op]

    def check(row: dict) -> bool:
        raw = row.get(field)
        if raw is None:
            return False
        try:
            return compare(float(raw), float(value))
        except (TypeError, ValueError):
            return False
    return check


def status_predicate(status: str) -> Callable[[dict], bool]:
    wanted = str(status).upper()
    return lambda row: str(row.get("status") or "").upper() == wanted
