#!/usr/bin/env python3
"""Re-decide past matches under different settings, without walking the building.

    .venv/bin/python scripts/reid_replay.py evidence/reid_decisions.jsonl
    .venv/bin/python scripts/reid_replay.py <journal> --threshold 0.65
    .venv/bin/python scripts/reid_replay.py <journal> --lone-threshold 0.55
    .venv/bin/python scripts/reid_replay.py <journal> --sweep

WHAT THIS IS FOR. Embeddings are never persisted, so the matcher cannot be
re-run offline — "would 0.65 have matched that hop?" normally costs a walk
through the building. The journal records the score of EVERY candidate at each
decision, which is enough to replay the threshold and margin rules exactly,
because those rules only ever compare scores to each other and to a constant.

WHAT IT CANNOT TELL YOU. Replay reasons about the candidates that were actually
scored. It cannot invent candidates the physics gate rejected, and it cannot say
whether a match would have been CORRECT — only what the rules would have
decided. Pair it with a walk whose true route you know; that is what turns
"more matches" into "more RIGHT matches", and a change that only raises the
match count may simply be merging strangers.

THE NUMBER TO WATCH IS NEW MERGES. Loosening any bound produces more matches by
construction. The question is always which ones are new, and whether the runner
up was close behind — a match won by 0.002 over the next candidate is a coin
toss wearing a decision's clothes.
"""

import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def load(path):
    entries = []
    bad = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                bad += 1
    return entries, bad


def decide(entry, threshold, margin, lone_threshold):
    """Re-apply the matching rules to one recorded decision.

    Mirrors GlobalIdentityRegistry.resolve()'s gate 3 exactly. Gates 1 and 2
    are NOT re-run: the journal records what survived them, and their verdicts
    do not depend on any of the parameters being swept here.
    """
    scored = entry.get("scored") or []
    if not scored:
        return ("no_candidates", None, 0.0, 0.0)

    ranked = sorted(scored, key=lambda c: -float(c.get("score", 0.0)))
    best = ranked[0]
    best_score = float(best.get("score", 0.0))
    runner_up = float(ranked[1].get("score", 0.0)) if len(ranked) > 1 else 0.0
    lone = len(ranked) == 1

    # A single survivor of the physics gate is a different question from a
    # winner among several: nothing else could have been this person, so the
    # appearance score is corroboration rather than the deciding evidence.
    floor = lone_threshold if lone else threshold
    if best_score < floor:
        return ("below_threshold", best.get("ref"), best_score, runner_up)
    if not lone and (best_score - runner_up) < margin:
        return ("ambiguous_margin", best.get("ref"), best_score, runner_up)
    return ("appearance_match", best.get("ref"), best_score, runner_up)


def summarise(entries, threshold, margin, lone_threshold):
    out = {"appearance_match": 0, "ambiguous_margin": 0,
           "below_threshold": 0, "no_candidates": 0}
    picks = {}
    for e in entries:
        # Sticky bindings were never a choice; replaying them would inflate
        # every count with decisions no parameter can change.
        if e.get("decision") == "existing_binding":
            continue
        verdict, ref, best, runner = decide(e, threshold, margin, lone_threshold)
        out[verdict] += 1
        picks[id(e)] = (verdict, ref, best, runner)
    return out, picks


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("journal")
    ap.add_argument("--threshold", type=float, default=0.70)
    ap.add_argument("--margin", type=float, default=0.06)
    ap.add_argument("--lone-threshold", type=float, default=None,
                    help="floor when physics left exactly ONE candidate. "
                         "Defaults to --threshold, i.e. today's behaviour.")
    ap.add_argument("--sweep", action="store_true",
                    help="table of thresholds instead of one comparison")
    ap.add_argument("--show", type=int, default=8,
                    help="how many changed decisions to print")
    args = ap.parse_args()

    if not os.path.exists(args.journal):
        print("no journal at %s" % args.journal)
        print("Record one with:  FINBLADE_REID_JOURNAL=evidence/reid_decisions.jsonl")
        return 2

    entries, bad = load(args.journal)
    decisions = [e for e in entries if e.get("decision") != "existing_binding"]
    if not decisions:
        print("journal has %d entries but no real decisions in it "
              "(all sticky bindings)." % len(entries))
        return 1

    lone = args.lone_threshold if args.lone_threshold is not None else args.threshold

    print("journal   %s" % args.journal)
    print("entries   %d  (%d decisions, %d sticky, %d unparseable)"
          % (len(entries), len(decisions), len(entries) - len(decisions), bad))
    only_one = sum(1 for e in decisions if len(e.get("scored") or []) == 1)
    print("of those, physics left exactly one candidate: %d (%.0f%%)"
          % (only_one, 100.0 * only_one / len(decisions)))

    if args.sweep:
        print()
        print("  threshold  lone   matched  ambiguous  below   new-vs-live")
        base, _ = summarise(decisions, 0.70, args.margin, 0.70)
        for th in (0.80, 0.75, 0.70, 0.65, 0.60, 0.55):
            for lo in (th, max(0.40, th - 0.15)):
                got, _ = summarise(decisions, th, args.margin, lo)
                delta = got["appearance_match"] - base["appearance_match"]
                print("  %9.2f  %.2f  %7d  %9d  %5d   %+d"
                      % (th, lo, got["appearance_match"],
                         got["ambiguous_margin"], got["below_threshold"], delta))
        print()
        print("'lone' is the floor applied when physics left ONE candidate.")
        print("Baseline is threshold 0.70 with no leniency — today's behaviour.")
        return 0

    live, _ = summarise(decisions, 0.70, 0.06, 0.70)
    trial, picks = summarise(decisions, args.threshold, args.margin, lone)

    print()
    print("                    live(0.70/0.06)   trial(%.2f/%.2f, lone %.2f)"
          % (args.threshold, args.margin, lone))
    for k in ("appearance_match", "ambiguous_margin", "below_threshold"):
        print("  %-18s %10d %20d" % (k, live[k], trial[k]))

    # The decisions that flip, which is the whole point. A match the live
    # settings refused is a candidate merge, and merges are the harmful error.
    flipped = []
    for e in decisions:
        was, _r1, _b1, _u1 = decide(e, 0.70, 0.06, 0.70)
        now, ref, best, runner = decide(e, args.threshold, args.margin, lone)
        if was != now:
            flipped.append((e, was, now, ref, best, runner))

    print()
    print("decisions that change: %d" % len(flipped))
    gained = [f for f in flipped if f[2] == "appearance_match"]
    lost = [f for f in flipped if f[1] == "appearance_match"]
    print("  newly matched %d   no longer matched %d" % (len(gained), len(lost)))

    if gained:
        print()
        print("NEW MATCHES — each one is a merge that does not happen today.")
        print("A thin gap to the runner-up is a coin toss, not a decision.")
        for e, _was, _now, ref, best, runner in gained[:args.show]:
            n = len(e.get("scored") or [])
            print("  %s track %-5s  best %.3f  runner-up %.3f  gap %.3f  "
                  "candidates %d" % (e.get("camera"), e.get("track"), best,
                                     runner, best - runner, n))
        if len(gained) > args.show:
            print("  ... and %d more" % (len(gained) - args.show))

    print()
    print("Replay says what the RULES would decide, never whether the decision")
    print("is right. Walk a known route and check these against it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
