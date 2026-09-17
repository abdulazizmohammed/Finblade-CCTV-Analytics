#!/usr/bin/env python3
"""Re-apply the attribute confidence floors to rows already in person_sightings.

Every sighting stores its per-attribute confidences beside the labels, so a
floor raised after the fact (mask: 0.85, 2026-09-17) can be applied to what
is already there WITHOUT re-running the model: any label whose stored
confidence is below its floor becomes "unknown" and the description is
rewritten. It is exactly what the worker would have stored had the floor
been in place — nothing is re-scored, nothing is guessed.

    .venv/bin/python scripts/reapply_attribute_floors.py            # dry run: counts only
    .venv/bin/python scripts/reapply_attribute_floors.py --apply    # write the changes
    .venv/bin/python scripts/reapply_attribute_floors.py --config config/cameras.template.yaml

Floors come from the `attributes.min_confidence` block of the camera config
(a number, or {default: x, <attr>: y}); DATABASE_URL from .env. Rows are
never deleted and a label is never raised from "unknown" — this only ever
lowers confidence-in-the-answer, so it is safe to run more than once.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from finblade.attributes import Vocabulary, confidence_floors, describe  # noqa: E402

ATTRS = ("upper_colour", "lower_colour", "headwear", "mask", "bag", "outerwear")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default="config/cameras.template.yaml")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args(argv)

    import yaml
    with open(args.config) as fh:
        cfg = yaml.safe_load(fh) or {}
    floors = confidence_floors((cfg.get("attributes") or {}).get("min_confidence", 0.45), ATTRS)
    print("floors:", json.dumps(floors))
    # Labels the current vocabulary no longer has (an experiment's leftover
    # class, a term the site struck out) are retired to "unknown" as well:
    # they can never be searched for and read as garbage in a description.
    vocab = Vocabulary.from_config(cfg.get("attributes") or {})
    allowed = {a: set(vocab.labels(a)) for a in ATTRS if a in vocab.names()}

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
        if os.path.exists(env):
            for line in open(env):
                if line.startswith("DATABASE_URL="):
                    dsn = line.split("=", 1)[1].strip()
    if not dsn:
        print("DATABASE_URL not set (env or .env)", file=sys.stderr)
        return 2

    from services.api.postgres_store import PostgresStore
    store = PostgresStore(dsn, apply_schema=False)
    rows = store._q("SELECT id, upper_colour, lower_colour, headwear, mask, bag, outerwear, "
                    "extra, confidences, description FROM person_sightings")
    changed = 0
    per_attr = {a: 0 for a in ATTRS}
    retired_labels: dict = {}
    for r in rows:
        try:
            conf = json.loads(r.get("confidences") or "{}")
        except ValueError:
            conf = {}
        labels = {a: r.get(a) for a in ATTRS}
        new = dict(labels)
        for a in ATTRS:
            lab = labels.get(a)
            if not lab or lab == "unknown":
                continue
            below = float(conf.get(a, 1.0)) < floors[a]
            retired = a in allowed and lab not in allowed[a]
            if below or retired:
                new[a] = "unknown"
                per_attr[a] += 1
                if retired:
                    retired_labels[f"{a}={lab}"] = retired_labels.get(f"{a}={lab}", 0) + 1
        if new == labels:
            continue
        changed += 1
        try:
            extra = json.loads(r.get("extra") or "{}")
        except ValueError:
            extra = {}
        desc = describe(dict(extra, **{a: v for a, v in new.items() if v}))
        if args.apply:
            store._x("UPDATE person_sightings SET upper_colour=%s, lower_colour=%s, headwear=%s, "
                     "mask=%s, bag=%s, outerwear=%s, description=%s WHERE id=%s",
                     (new["upper_colour"], new["lower_colour"], new["headwear"], new["mask"],
                      new["bag"], new["outerwear"], desc, r["id"]))
    print(f"rows: {len(rows)}  would change: {changed}" if not args.apply
          else f"rows: {len(rows)}  changed: {changed}")
    print("set to unknown per attribute:", json.dumps({a: n for a, n in per_attr.items() if n}))
    if retired_labels:
        print("labels no longer in the vocabulary:", json.dumps(retired_labels))
    if not args.apply and changed:
        print("dry run — add --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
