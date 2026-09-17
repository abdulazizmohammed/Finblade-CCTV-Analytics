#!/usr/bin/env python3
"""Contact sheet of appearance tags: the crop next to what the model said.

    .venv/bin/python scripts/attributes_sheet.py                       # evidence/events.jsonl
    .venv/bin/python scripts/attributes_sheet.py --camera ATTR-TEST --out evidence/attributes_sheet.jpg

THIS IS THE EVIDENCE FOR A CAPABILITY NOBODY HERE CAN SEE. The tagger says
"blue top, cap"; only a person looking at the crop can say whether that is
true. This tiles every PERSON_ATTRIBUTES event's crop with its tags and
confidences, ~5 across, so the human can scan a hundred in a minute and read
off the error rate — which is the number that decides whether search results
can be trusted on this camera. Tags reported "unknown" are shown as such; a
high unknown share is the second thing to read off.
"""

import argparse
import json
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main():
    import cv2
    import numpy as np
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--events", default=os.path.join(REPO, "evidence", "events.jsonl"))
    ap.add_argument("--camera", default=None, help="only this camera's tags")
    ap.add_argument("--out", default=os.path.join(REPO, "evidence", "attributes_sheet.jpg"))
    ap.add_argument("--cols", type=int, default=5)
    ap.add_argument("--max", type=int, default=100)
    args = ap.parse_args()

    tags = []
    with open(args.events, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event_type") != "PERSON_ATTRIBUTES":
                continue
            if args.camera and e.get("camera_id") != args.camera:
                continue
            tags.append(e)
    tags = tags[-args.max:]
    if not tags:
        print("no PERSON_ATTRIBUTES events in", args.events)
        return 1

    W, H, TXT = 200, 300, 118
    tiles = []
    unknown = total = 0
    for e in tags:
        tile = np.full((H + TXT, W, 3), 14, dtype=np.uint8)
        frame = e.get("frame")
        path = os.path.join(REPO, "evidence", frame.lstrip("/")) if frame else None
        img = cv2.imread(path) if path and os.path.exists(path) else None
        if img is not None:
            h, w = img.shape[:2]
            s = min(W / w, H / h)
            img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))))
            y0, x0 = (H - img.shape[0]) // 2, (W - img.shape[1]) // 2
            tile[y0:y0 + img.shape[0], x0:x0 + img.shape[1]] = img
        else:
            cv2.putText(tile, "no crop", (60, H // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (90, 110, 120), 1)
        attrs = e.get("attributes") or {}
        confs = e.get("confidences") or {}
        y = H + 16
        for k in ("upper_colour", "lower_colour", "headwear", "mask", "bag", "outerwear"):
            v = attrs.get(k, "-")
            total += 1
            if v == "unknown":
                unknown += 1
            c = confs.get(k)
            col = (100, 120, 130) if v == "unknown" else (220, 236, 240)
            cv2.putText(tile, f"{k[:5]}: {v} {'' if c is None else format(c, '.2f')}", (6, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, col, 1)
            y += 16
        cv2.putText(tile, f"t{e.get('track_id')} n={e.get('samples')} {e.get('camera_id')}", (6, H + TXT - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (24, 189, 194), 1)
        tiles.append(tile)
    cols = args.cols
    rows = (len(tiles) + cols - 1) // cols
    blank = np.full((H + TXT, W, 3), 14, dtype=np.uint8)
    grid = np.vstack([np.hstack(tiles[r * cols:(r + 1) * cols] + [blank] * (cols - len(tiles[r * cols:(r + 1) * cols])))
                      for r in range(rows)])
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    cv2.imwrite(args.out, grid, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"{args.out}: {len(tiles)} tags, {unknown}/{total} attributes unknown "
          f"({100.0 * unknown / max(1, total):.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
