"""Propose a camera topology from observed sightings.

Writing config/topology.yaml by hand does not scale past one site. The numbers
in it — which cameras share floor, and how long the walk is between the ones
that do not — are site knowledge, but they are not knowledge that has to come
from a tape measure: the system has already watched people move between these
cameras, and that history is in the events table.

For each person, sort their sightings by time and look at consecutive pairs on
DIFFERENT cameras. The gap between them says what kind of pair it is:

    CAM-03 -> CAM-04   dt: 0.0 0.1 0.0 0.3 0.0 0.2   -> they see the same floor
    CAM-01 -> CAM-07   dt: 14  17  15  22  16  19    -> a walk, ~14s at a jog
    CAM-01 -> CAM-09   (never)                        -> no route, or no data

THIS PRODUCES A DRAFT, NOT AN ANSWER. Two reasons it must be read by a human
before it is used:

  1. It is circular. The refs come from ReID, which ran under whatever topology
     was already configured — so a proposal can confirm that config's own
     mistakes. A pair that was wrongly gated shut produces no samples and looks
     like "no route"; a pair matching strangers produces noise that looks like
     a wide transit window.

  2. Absence of evidence is not evidence of absence. A pair with no samples may
     have no route, or may simply have had nobody walk it during the window.
     Those need opposite config and this cannot tell them apart.

The honest use is: run it, read it, walk the routes it is unsure about, and
paste in what you confirm. It turns a survey of every pair into a survey of the
few pairs the data cannot settle.

Pure stdlib — no database, no yaml, no camera.
"""

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Pair = Tuple[str, str]
Sighting = Tuple[str, str, float]        # (global_ref, camera_id, ts)


def _key(a: str, b: str) -> Pair:
    return (a, b) if a <= b else (b, a)


def _trimmed_min(values: Sequence[float]) -> float:
    """Smallest gap after discarding the fastest few, which are usually wrong.

    A plain minimum is set by the single worst match in the sample: one
    mismatched pair at dt=0 drops min_seconds to zero and disables the very
    gate the window exists to provide. A nearest-rank p05 does not help at
    realistic sample sizes either — with eight observations it rounds to index
    0, the outlier itself.

    So drop at least one observation once there are enough to afford it. This
    biases the minimum UP, which is the safe direction here: too high misses a
    real handover and counts one person twice, too low links two strangers and
    puts a stranger's movements under someone else's identity. The project
    takes the split over the merge everywhere else; this matches.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) < 5:
        return ordered[0]
    return ordered[max(1, int(0.05 * len(ordered)))]


def _pct(values: Sequence[float], p: float) -> float:
    """Percentile by nearest rank. Small samples are the norm here, and
    interpolating between two observations invents a transit time nobody made."""
    if not values:
        return 0.0
    ordered = sorted(values)
    i = max(0, min(len(ordered) - 1, int(round(p * (len(ordered) - 1)))))
    return ordered[i]


def pair_gaps(sightings: Iterable[Sighting]) -> Dict[Pair, List[float]]:
    """Gaps between consecutive sightings of one person on different cameras.

    Only ADJACENT sightings count. Taking every combination would treat a
    person's first and last appearance of the day as a transit and swamp the
    real walks with hours-long gaps.
    """
    by_ref: Dict[str, List[Tuple[float, str]]] = {}
    for ref, cam, ts in sightings:
        if not ref or not cam:
            continue
        by_ref.setdefault(str(ref), []).append((float(ts), str(cam)))

    gaps: Dict[Pair, List[float]] = {}
    for track in by_ref.values():
        track.sort()
        for (t0, c0), (t1, c1) in zip(track, track[1:]):
            if c0 == c1:
                continue
            gaps.setdefault(_key(c0, c1), []).append(abs(t1 - t0))
    return gaps


def classify_pair(dts: Sequence[float], overlap_max_dt: float = 2.0,
                  min_samples: int = 5) -> dict:
    """Decide whether a pair overlaps, is a walk, or cannot be called yet."""
    n = len(dts)
    if n == 0:
        return {"kind": "no_data", "samples": 0}

    p05, p50, p95 = _pct(dts, 0.05), _pct(dts, 0.50), _pct(dts, 0.95)
    near_zero = sum(1 for d in dts if d <= overlap_max_dt) / float(n)
    out = {"samples": n, "p05": round(p05, 2), "p50": round(p50, 2),
           "p95": round(p95, 2), "near_zero_fraction": round(near_zero, 2)}

    if n < min_samples:
        # Reporting a window from three observations would look like a
        # measurement. It is an anecdote.
        out["kind"] = "insufficient"
        return out

    if p50 <= overlap_max_dt:
        # Half of all handovers took under a couple of seconds. A person cannot
        # walk between two separate places that fast, so the views share floor.
        out["kind"] = "overlapping"
        return out

    out["kind"] = "transit"
    out["min_seconds"] = max(0.0, round(_trimmed_min(dts), 1))
    # p95 rather than the maximum, for the same reason in the other direction —
    # and never below the observed median, which would reject typical walks.
    out["max_seconds"] = round(max(p95, p50 * 1.5), 1)
    return out


def propose(sightings: Iterable[Sighting], overlap_max_dt: float = 2.0,
            min_samples: int = 5,
            cameras: Optional[Iterable[str]] = None) -> dict:
    """Build a topology proposal. Returns a dict shaped like topology.yaml.

    ``cameras`` is the full camera list, so pairs that produced NO evidence can
    be listed explicitly as unsurveyed rather than silently omitted — an
    omitted pair falls back to the permissive default, which is exactly the
    case that fails quietly.
    """
    gaps = pair_gaps(sightings)
    verdicts = {pair: classify_pair(d, overlap_max_dt, min_samples)
                for pair, d in gaps.items()}

    known = set(cameras or ())
    for a, b in gaps:
        known.update((a, b))
    all_pairs = {_key(a, b) for a in known for b in known if a != b}
    for pair in all_pairs:
        verdicts.setdefault(pair, {"kind": "no_data", "samples": 0})

    overlapping, transits, unsurveyed = [], [], []
    for pair in sorted(verdicts):
        v = verdicts[pair]
        a, b = pair
        if v["kind"] == "overlapping":
            overlapping.append({"a": a, "b": b, "evidence": v})
        elif v["kind"] == "transit":
            transits.append({"a": a, "b": b,
                             "min_seconds": v["min_seconds"],
                             "max_seconds": v["max_seconds"],
                             "evidence": v})
        else:
            unsurveyed.append({"a": a, "b": b, "reason": v["kind"],
                               "evidence": v})

    return {"overlapping_pairs": overlapping, "transits": transits,
            "unsurveyed": unsurveyed,
            "cameras": sorted(known)}


def to_yaml(proposal: dict, site: Optional[str] = None) -> str:
    """Render the proposal as a topology.yaml a human can review and edit.

    Hand-written rather than yaml.dump so every entry carries the evidence that
    produced it as a comment. A transit window with no sample count behind it
    is indistinguishable from a guess, and this file's whole problem is that
    guesses in it fail silently.
    """
    L: List[str] = []
    add = L.append
    add("# Camera topology — PROPOSED FROM OBSERVED DATA. Review before use.")
    if site:
        add(f"# Site: {site}")
    add("#")
    add("# Generated by scripts/propose_topology.py from the events table.")
    add("# Each entry carries the evidence behind it. Numbers with few samples,")
    add("# and every pair under `unsurveyed`, still need a human.")
    add("#")
    add("# Circular by construction: these refs came from ReID running under")
    add("# the PREVIOUS topology, so this can reproduce that config's mistakes.")
    add("# Walk the routes listed as unsurveyed rather than trusting silence.")
    add("")

    add("overlapping_pairs:")
    if not proposal["overlapping_pairs"]:
        add("  []   # none observed — if two cameras share floor, add them here")
    for e in proposal["overlapping_pairs"]:
        ev = e["evidence"]
        add(f"  # {ev['samples']} handovers, median {ev['p50']}s, "
            f"{int(ev['near_zero_fraction'] * 100)}% within the overlap window")
        add(f"  - a: {e['a']}")
        add(f"    b: {e['b']}")
    add("")

    add("transits:")
    if not proposal["transits"]:
        add("  []")
    for e in proposal["transits"]:
        ev = e["evidence"]
        add(f"  # {ev['samples']} walks observed — p05 {ev['p05']}s, "
            f"median {ev['p50']}s, p95 {ev['p95']}s")
        add(f"  - a: {e['a']}")
        add(f"    b: {e['b']}")
        add(f"    min_seconds: {e['min_seconds']}")
        add(f"    max_seconds: {e['max_seconds']}")
    add("")

    add("# Pairs the data could not settle. Each needs a decision:")
    add("#   share floor        -> move to overlapping_pairs")
    add("#   a walk between     -> add to transits with a paced time")
    add("#   no route at all    -> leave out AND set allow_unknown_pairs: false")
    for e in proposal["unsurveyed"]:
        add(f"#   {e['a']} <-> {e['b']}  ({e['reason']}, "
            f"{e['evidence']['samples']} samples)")
    if not proposal["unsurveyed"]:
        add("#   (none — every pair had enough evidence to classify)")
    add("")

    add("default_transit:")
    add("  min_seconds: 0.0     # keep 0.0 until every overlapping pair is declared")
    add("  max_seconds: 120.0")
    add("")
    add("overlap_tolerance_seconds: 5.0")
    add("")
    add("# Set false once `unsurveyed` above is empty. Until then a missing pair")
    add("# falls back to the permissive default rather than being refused.")
    add("allow_unknown_pairs: true")
    return "\n".join(L) + "\n"
