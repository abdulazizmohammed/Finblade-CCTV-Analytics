"""Measure the ACTUAL detection confidence of everything a camera sees.

Why this exists: a false positive on furniture cannot be judged from the UI. The
dashboard shows counts, and the annotated feed shows boxes, but neither shows the
confidence behind a box — so "should I raise conf_threshold?" is unanswerable by
looking. This prints the number.

It deliberately runs BELOW the production threshold (--conf 0.15 by default) so
you can see what a detection scores even when it sometimes falls under the
production cut. A box that oscillates around 0.4 appears and disappears in the
UI; here you see it every frame with its real score.

Detections are grouped into CLUSTERS by position across frames. Furniture holds
still, so a cluster whose centre barely moves is a static-object candidate; a
person walking produces a cluster with large displacement. That is a heuristic
about MOVEMENT, not about what the object is:

  ** THIS SCRIPT CANNOT TELL A CHAIR FROM A PERSON SITTING STILL. **

It writes clusters.jpg with every cluster boxed and numbered so YOU can look once
and say which id is the chair. Then read that id's confidence off the table.

Read-only: opens the RTSP stream as an extra client and touches nothing the
running pipeline owns.

  .venv/bin/python scripts/probe_confidence.py --source rtsp://... --seconds 30

Find the source URL on /web/cameras.html, or in the worker log:
  grep -m1 -o 'rtsp://[^ ]*' scripts/cam_CAM-04.log
"""

import argparse
import json
import math
import os
import statistics
import sys
import time

import cv2
import yaml
from ultralytics import YOLO


def _resolve_device(want):
    """Mirror run_cpu.py: map a config device onto an Ultralytics device string."""
    want = str(want or "cpu").lower()
    if want in ("cuda", "gpu", "0", "cuda:0"):
        try:
            import torch
            if torch.cuda.is_available():
                return 0
        except Exception:
            pass
        print("[warn] CUDA unavailable; probing on CPU (slower, same numbers)",
              file=sys.stderr)
    return "cpu"


class Cluster:
    """One spatial group of detections seen across frames."""

    def __init__(self, cid, box, conf, frame_idx):
        self.cid = cid
        self.confs = [conf]
        self.centres = [self._centre(box)]
        self.boxes = [box]
        self.first = self.last = frame_idx

    @staticmethod
    def _centre(box):
        x1, y1, x2, y2 = box
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def centre(self):
        xs = [c[0] for c in self.centres]
        ys = [c[1] for c in self.centres]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def add(self, box, conf, frame_idx):
        self.confs.append(conf)
        self.centres.append(self._centre(box))
        self.boxes.append(box)
        self.last = frame_idx

    def displacement(self):
        """Max distance any observation sits from the cluster's mean centre, x2.

        A cheap stand-in for "how far did this thing travel" that does not need
        the full pairwise matrix.
        """
        cx, cy = self.centre()
        return 2.0 * max(math.hypot(x - cx, y - cy) for x, y in self.centres)

    def summary(self, diag, prod_conf, frames_total):
        disp = self.displacement()
        confs = sorted(self.confs)
        return {
            "cluster_id": self.cid,
            "frames_seen": len(self.confs),
            "seen_pct": round(100.0 * len(self.confs) / max(frames_total, 1), 1),
            "conf_min": round(confs[0], 3),
            "conf_median": round(statistics.median(confs), 3),
            "conf_max": round(confs[-1], 3),
            # What matters for a threshold decision: how often this thing would
            # clear the production cut.
            "pct_above_prod_threshold": round(
                100.0 * sum(1 for c in confs if c >= prod_conf) / len(confs), 1),
            "displacement_px": round(disp, 1),
            "displacement_pct_of_diag": round(100.0 * disp / diag, 2),
            "movement": "STATIC" if disp < 0.03 * diag else "MOVING",
            "centre": [round(v, 1) for v in self.centre()],
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="RTSP URL or video file")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--conf", type=float, default=0.15,
                    help="probe threshold; keep BELOW production to see the whole picture")
    ap.add_argument("--config", default="config/cameras.template.yaml")
    ap.add_argument("--out", default="evidence/confidence_probe")
    ap.add_argument("--match-px-pct", type=float, default=8.0,
                    help="centre distance (%% of frame diagonal) to join a cluster")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    model_path = cfg.get("model_path", "models/yolov8n.pt")
    imgsz = int(cfg.get("imgsz", 1280))
    person_cls = int(cfg.get("person_class_id", 0))
    prod_conf = float(cfg.get("conf_threshold", 0.4))
    device = _resolve_device(cfg.get("device"))

    if args.conf >= prod_conf:
        print(f"[warn] --conf {args.conf} is not below the production threshold "
              f"{prod_conf}; you will not see sub-threshold detections",
              file=sys.stderr)

    os.makedirs(args.out, exist_ok=True)
    print(f"model={model_path} imgsz={imgsz} device={device} "
          f"production conf_threshold={prod_conf} probe conf={args.conf}")

    model = YOLO(model_path)
    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        raise SystemExit(f"[BLOCKER] cannot open source: {args.source}")

    clusters = []
    next_cid = 1
    frames = 0
    last_frame = None
    diag = None
    t0 = time.time()

    while time.time() - t0 < args.seconds:
        ok, frame = cap.read()
        if not ok:
            print("[warn] stream read failed; stopping early", file=sys.stderr)
            break
        frames += 1
        last_frame = frame
        if diag is None:
            h, w = frame.shape[:2]
            diag = math.hypot(w, h)
            print(f"source resolution: {w}x{h}")
        join_px = (args.match_px_pct / 100.0) * diag

        res = model.predict(frame, conf=args.conf, classes=[person_cls],
                            imgsz=imgsz, device=device, verbose=False)[0]
        for b in res.boxes:
            box = [float(v) for v in b.xyxy[0].tolist()]
            conf = float(b.conf[0])
            cx, cy = Cluster._centre(box)
            best, best_d = None, None
            for c in clusters:
                ccx, ccy = c.centre()
                d = math.hypot(cx - ccx, cy - ccy)
                if d <= join_px and (best_d is None or d < best_d):
                    best, best_d = c, d
            if best is None:
                clusters.append(Cluster(next_cid, box, conf, frames))
                next_cid += 1
            else:
                best.add(box, conf, frames)

    cap.release()
    if frames == 0:
        raise SystemExit("[BLOCKER] no frames read; nothing measured")

    # Drop one-off blips: a cluster seen once in 300 frames tells you nothing and
    # crowds out the signal.
    kept = [c for c in clusters if len(c.confs) >= max(2, frames // 100)]
    kept.sort(key=lambda c: -len(c.confs))
    rows = [c.summary(diag, prod_conf, frames) for c in kept]

    # The artifact that makes this usable: every cluster boxed and numbered on a
    # real frame, so a human can map id -> object in one look. I cannot do that.
    if last_frame is not None:
        canvas = last_frame.copy()
        for c, row in zip(kept, rows):
            x1, y1, x2, y2 = [int(v) for v in c.boxes[-1]]
            cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 255, 255), 2)
            label = f"#{row['cluster_id']} {row['conf_median']:.2f} {row['movement']}"
            cv2.putText(canvas, label, (x1, max(14, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1,
                        cv2.LINE_AA)
        path = os.path.join(args.out, "clusters.jpg")
        cv2.imwrite(path, canvas)

    out = {
        "source": args.source,
        "frames_probed": frames,
        "seconds": round(time.time() - t0, 1),
        "model_path": model_path,
        "imgsz": imgsz,
        "production_conf_threshold": prod_conf,
        "probe_conf": args.conf,
        "clusters": rows,
    }
    with open(os.path.join(args.out, "confidence_probe.json"), "w",
              encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"\nframes probed: {frames}")
    print(f"{'id':>3} {'seen':>6} {'conf min':>9} {'median':>7} {'max':>6} "
          f"{'>=prod':>7} {'move px':>8}  verdict")
    for r in rows:
        print(f"{r['cluster_id']:>3} {r['seen_pct']:>5.0f}% {r['conf_min']:>9.3f} "
              f"{r['conf_median']:>7.3f} {r['conf_max']:>6.3f} "
              f"{r['pct_above_prod_threshold']:>6.0f}% {r['displacement_px']:>8.1f}  "
              f"{r['movement']}")

    print(f"\nwrote {args.out}/confidence_probe.json")
    print(f"wrote {args.out}/clusters.jpg   <-- OPEN THIS")
    print("\nNEEDS YOUR EYES: match a cluster id in clusters.jpg to the chair.")
    print("Then read its row above:")
    print(f"  conf_max well below {prod_conf}  -> already excluded; not your false positive")
    print(f"  median just above {prod_conf}    -> a small threshold rise would drop it")
    print("  median well above real people     -> threshold cannot separate them;")
    print("                                       raise source resolution or model size")
    print("\nCompare against a MOVING cluster (a real person) in the same run —")
    print("the gap between those two medians is the only thing that decides")
    print("whether a threshold change is safe on THIS camera.")


if __name__ == "__main__":
    main()
