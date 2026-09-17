#!/usr/bin/env python3
"""Human review of stored appearance tags: a numbered sheet, then reject by number.

The tagger cannot be asked to re-judge its own crops — it is the same model
looking at the same pixels and it gives the same answer. A person can. This
pulls every sighting matching one attribute value from the database, tiles
the crops with a number, the label and its confidence, and writes one sheet
to look at. Read off the wrong ones, then feed the numbers back and those
tags become "unknown" through the API's correction route — audited, with
your name on it, and the description rewritten.

    # 1. sheet of everyone tagged mask=yes in the last 24 h
    .venv/bin/python scripts/attributes_review.py --attr mask --value yes --hours 24
        -> evidence/review_mask_yes.jpg  +  evidence/review_mask_yes.json (number -> sighting_id)

    # 2. look at the sheet; tiles 3, 7 and 12 are wrong
    .venv/bin/python scripts/attributes_review.py --attr mask --reject 3,7,12 --by "A. Aziz"

Reads DATABASE_URL from .env; the rejection step talks to the API
(FINBLADE_API_KEY from .env, FINBLADE_SELF_URL or http://127.0.0.1:8000).
"""
import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)

ATTRS = ("upper_colour", "lower_colour", "headwear", "mask", "bag", "outerwear")


def _env():
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def build_sheet(args) -> int:
    import time
    import cv2
    import numpy as np
    from services.api.postgres_store import PostgresStore
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2
    store = PostgresStore(dsn, apply_schema=False)
    t0 = time.time() - args.hours * 3600.0
    where, params = ["ts >= %s"], [t0]
    if args.value:
        where.append(f"{args.attr}=%s"); params.append(args.value)
    if args.camera:
        where.append("camera_id=%s"); params.append(args.camera)
    params.append(args.max)
    rows = store._ps_out(store._q(
        f"SELECT {store._PS_COLS} FROM person_sightings WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC LIMIT %s", params))
    if not rows:
        print("nothing matches")
        return 1

    W, H, TXT = 200, 300, 64
    tiles, index = [], {}
    for n, r in enumerate(rows, start=1):
        tile = np.full((H + TXT, W, 3), 14, dtype=np.uint8)
        frame = r.get("frame")
        path = os.path.join(REPO, "evidence", frame.lstrip("/")) if frame else None
        img = cv2.imread(path) if path and os.path.exists(path) else None
        if img is not None:
            h, w = img.shape[:2]
            s = min(W / w, H / h)
            img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))))
            y0, x0 = (H - img.shape[0]) // 2, (W - img.shape[1]) // 2
            tile[y0:y0 + img.shape[0], x0:x0 + img.shape[1]] = img
        else:
            cv2.putText(tile, "crop gone", (50, H // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (90, 110, 120), 1)
        # the number is the handle for --reject; big and unmissable
        cv2.rectangle(tile, (0, 0), (58, 26), (24, 189, 194), -1)
        cv2.putText(tile, f"#{n}", (4, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (10, 10, 10), 2)
        conf = (r.get("confidences") or {}).get(args.attr)
        cv2.putText(tile, f"{args.attr}: {r.get(args.attr)} {'' if conf is None else format(float(conf), '.2f')}",
                    (6, H + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (220, 236, 240), 1)
        cv2.putText(tile, (r.get("description") or "")[:34], (6, H + 36), cv2.FONT_HERSHEY_SIMPLEX, 0.36, (150, 170, 180), 1)
        cv2.putText(tile, f"{r.get('camera_id')} {time.strftime('%H:%M', time.localtime(float(r['ts'])))}",
                    (6, H + TXT - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (24, 189, 194), 1)
        tiles.append(tile)
        index[str(n)] = r["event_id"]
    cols = args.cols
    nrows = (len(tiles) + cols - 1) // cols
    blank = np.full((H + TXT, W, 3), 14, dtype=np.uint8)
    grid = np.vstack([np.hstack(tiles[i * cols:(i + 1) * cols] + [blank] * (cols - len(tiles[i * cols:(i + 1) * cols])))
                      for i in range(nrows)])
    stem = f"review_{args.attr}_{args.value or 'any'}"
    out = args.out or os.path.join(REPO, "evidence", stem + ".jpg")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cv2.imwrite(out, grid, [cv2.IMWRITE_JPEG_QUALITY, 85])
    idx = os.path.splitext(out)[0] + ".json"
    with open(idx, "w") as fh:
        json.dump({"attr": args.attr, "value": args.value, "index": index}, fh, indent=1)
    print(f"{out}: {len(tiles)} tiles ({cols} across); numbers -> ids in {idx}")
    print(f"then: scripts/attributes_review.py --attr {args.attr} --value {args.value or ''} --reject <numbers> --by <you>")
    return 0


def reject(args) -> int:
    import requests
    stem = f"review_{args.attr}_{args.value or 'any'}"
    idx = os.path.splitext(args.out)[0] + ".json" if args.out else os.path.join(REPO, "evidence", stem + ".json")
    with open(idx) as fh:
        index = json.load(fh)["index"]
    base = (os.environ.get("FINBLADE_SELF_URL") or "http://127.0.0.1:8000").rstrip("/")
    key = os.environ.get("FINBLADE_API_KEY")
    h = {"Authorization": f"Bearer {key}"} if key else {}
    ok = bad = 0
    for n in [x.strip() for x in args.reject.split(",") if x.strip()]:
        sid = index.get(n)
        if not sid:
            print(f"  #{n}: not on the sheet"); bad += 1; continue
        r = requests.post(f"{base}/api/v1/search/sightings/{sid}/correct", headers=h, timeout=15,
                          json={"attribute": args.attr, "value": args.to, "by": args.by})
        if r.status_code == 200:
            d = r.json(); ok += 1
            print(f"  #{n}: {args.attr} {d['from']} -> {d['to']}   ({d['description']})")
        else:
            bad += 1
            print(f"  #{n}: HTTP {r.status_code} {r.text[:120]}")
    print(f"corrected {ok}, failed {bad}")
    return 0 if not bad else 1


def main(argv=None) -> int:
    _env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--attr", default="mask", choices=ATTRS)
    ap.add_argument("--value", default=None, help="only rows with this label (e.g. yes); default any")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--camera", default=None)
    ap.add_argument("--max", type=int, default=200)
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--out", default=None)
    ap.add_argument("--reject", default=None, help="comma-separated tile numbers whose tag is wrong")
    ap.add_argument("--to", default="unknown", help="what to set a rejected tag to (default unknown)")
    ap.add_argument("--by", default="operator", help="who reviewed (goes to search_audit)")
    args = ap.parse_args(argv)
    return reject(args) if args.reject else build_sheet(args)


if __name__ == "__main__":
    sys.exit(main())
