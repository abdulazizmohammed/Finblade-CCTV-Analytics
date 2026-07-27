"""Measure cross-camera identity accuracy on real footage with known ground truth.

THE PROBLEM THIS SOLVES. There is no footage of one person leaving camera A and
entering camera B, so there is nothing to check cross-camera matching against.
Without ground truth, "it produced some matches" is not evidence of anything.

THE TRICK. Take one clip and derive a synthetic second camera from it by a known
transform (horizontal flip + brightness + scale). Run detection and tracking
independently on each stream, so each has its own ByteTrack ids. Because the
transform is known, a box on camera B can be mapped back to camera A's frame,
and boxes that overlap are — by construction — the same person. That gives exact
ground-truth correspondences on real people in real footage.

Then ask the matcher to resolve both streams into one registry and score it:

  match rate    of the people visible on both cameras, how many got one shared
                global_ref (higher is better; misses are splits)
  false merges  pairs of genuinely DIFFERENT people who ended up sharing a ref
                (must be zero; this is the dangerous failure)
  identity count how many identities were created vs how many people there are

HONEST LIMITS — read before quoting these numbers.
  * Camera B is a transformed copy of camera A, so the two views share clothing,
    pose and lighting. Real cameras differ in viewpoint, illumination and scale
    far more than this. These figures therefore measure the PLUMBING and the
    THRESHOLD BEHAVIOUR end to end on real crops. They do NOT predict accuracy
    on a genuine second camera, and must not be quoted as if they do.
  * A high match rate here is the floor, not the ceiling of difficulty.
  * Ground truth comes from IoU of mapped boxes, so it inherits the detector's
    own errors. Pairs below the IoU threshold are simply not scored.

Run:
    .venv/bin/python scripts/eval_cross_camera.py --source media/clip.mp4 --frames 300
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from finblade.appearance import (CropQualityGate, EmbeddingSampler,  # noqa: E402
                                 OSNetEmbedder, TrackFeatureBank)
from finblade.globalid import GlobalIdentityRegistry  # noqa: E402
from finblade.topology import CameraTopology  # noqa: E402


def make_camera_b(frame, brightness=0.82, scale=0.92):
    """Derive a second 'camera' from a frame by a known, invertible transform."""
    h, w = frame.shape[:2]
    out = cv2.flip(frame, 1)                       # mirror
    out = cv2.convertScaleAbs(out, alpha=brightness, beta=-8)   # dimmer, cooler
    sw, sh = int(w * scale), int(h * scale)
    out = cv2.resize(out, (sw, sh))
    canvas = np.zeros((h, w, 3), dtype=np.uint8)   # pad back to frame size
    y0, x0 = (h - sh) // 2, (w - sw) // 2
    canvas[y0:y0 + sh, x0:x0 + sw] = out
    return canvas, (scale, x0, y0)


def map_b_box_to_a(box, frame_w, frame_h, params):
    """Map a camera-B box back into camera-A coordinates."""
    scale, x0, y0 = params
    x1, y1, x2, y2 = box
    # undo padding + scaling
    x1, x2 = (x1 - x0) / scale, (x2 - x0) / scale
    y1, y2 = (y1 - y0) / scale, (y2 - y0) / scale
    # undo the horizontal flip
    return (frame_w - x2, y1, frame_w - x1, y2)


def iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua if ua > 0 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="media/clip.mp4")
    ap.add_argument("--frames", type=int, default=300)
    ap.add_argument("--model", default="models/yolov8n.pt")
    ap.add_argument("--reid-weights", default="models/osnet_x0_25_msmt17.pt")
    ap.add_argument("--device", default="0")
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--threshold", type=float, default=0.62)
    ap.add_argument("--margin", type=float, default=0.06)
    ap.add_argument("--gt-iou", type=float, default=0.5)
    ap.add_argument("--min-samples", type=int, default=2)
    ap.add_argument("--out", default="evidence/cross_camera_eval.json")
    args = ap.parse_args()

    if not os.path.exists(args.source):
        sys.exit(f"[BLOCKER] source not found: {args.source}")
    if not os.path.exists(args.reid_weights):
        sys.exit(f"[BLOCKER] ReID weights not found: {args.reid_weights} "
                 "— run scripts/get_weights.py")

    from ultralytics import YOLO
    # Two model instances so each stream keeps its own ByteTrack state; sharing
    # one would leak track identity between the cameras and invalidate the test.
    model_a, model_b = YOLO(args.model), YOLO(args.model)

    embedder = OSNetEmbedder(weights=args.reid_weights, device=args.device)
    embedder.load()
    gate = CropQualityGate()
    samplers = {"A": EmbeddingSampler(interval_s=0.5, max_samples=6, budget_per_frame=12),
                "B": EmbeddingSampler(interval_s=0.5, max_samples=6, budget_per_frame=12)}
    banks = {"A": defaultdict(lambda: TrackFeatureBank(capacity=6)),
             "B": defaultdict(lambda: TrackFeatureBank(capacity=6))}

    # Ground truth: how often each (A track, B track) pair was spatially the
    # same person, and how often each track was seen at all.
    pair_hits = defaultdict(int)
    seen = {"A": defaultdict(int), "B": defaultdict(int)}

    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        sys.exit(f"[BLOCKER] cannot open {args.source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0

    t0 = time.time()
    n = 0
    while n < args.frames:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        vnow = n / fps
        h, w = frame.shape[:2]
        frame_b, params = make_camera_b(frame)

        dets = {}
        for cam, model, img in (("A", model_a, frame), ("B", model_b, frame_b)):
            res = model.track(img, persist=True, classes=[0], conf=args.conf,
                              imgsz=args.imgsz, tracker="bytetrack.yaml",
                              device=args.device, verbose=False)[0]
            rows = []
            if res.boxes is not None and res.boxes.id is not None:
                xyxy = res.boxes.xyxy.cpu().numpy()
                ids = res.boxes.id.cpu().numpy().astype(int)
                confs = res.boxes.conf.cpu().numpy()
                rows = [(int(t), tuple(map(float, bb)), float(c))
                        for bb, t, c in zip(xyxy, ids, confs)]
            dets[cam] = rows
            for tid, _bb, _c in rows:
                seen[cam][tid] += 1

        # --- ground truth for this frame: map B boxes into A space -----------
        for btid, bbox, _bc in dets["B"]:
            mapped = map_b_box_to_a(bbox, w, h, params)
            best, best_iou = None, 0.0
            for atid, abox, _ac in dets["A"]:
                v = iou(mapped, abox)
                if v > best_iou:
                    best, best_iou = atid, v
            if best is not None and best_iou >= args.gt_iou:
                pair_hits[(best, btid)] += 1

        # --- embed, exactly as the runtime does ------------------------------
        for cam, img in (("A", frame), ("B", frame_b)):
            rows = dets[cam]
            if not rows:
                continue
            boxes_by_tid = {t: b for t, b, _c in rows}
            conf_by_tid = {t: c for t, _b, c in rows}
            all_boxes = list(boxes_by_tid.values())
            chosen, chosen_boxes = [], []
            for tid in samplers[cam].select(list(boxes_by_tid), vnow):
                box = boxes_by_tid[tid]
                ok_crop, _reason = gate.check(box, conf_by_tid[tid], w, h)
                if not ok_crop or gate.occluded_by(box, all_boxes):
                    continue
                chosen.append(tid)
                chosen_boxes.append(box)
            if chosen:
                for tid, vec in zip(chosen, embedder.embed(img, chosen_boxes)):
                    banks[cam][tid].add(vec)
                    samplers[cam].record(tid, vnow)

        if n % 50 == 0:
            print(f"  frame {n}/{args.frames}", flush=True)

    cap.release()
    elapsed = time.time() - t0

    # --- resolve both streams into one registry ---------------------------
    # Overlapping pair: the two cameras see the same floor at the same instant.
    topo = CameraTopology(overlapping=[("CAM-A", "CAM-B")])
    reg = GlobalIdentityRegistry(topology=topo, threshold=args.threshold,
                                 margin=args.margin, ttl_seconds=10_000.0)
    refs = {"A": {}, "B": {}}
    # Interleave by first appearance so the order resembles a live run.
    order = ([("A", t) for t in banks["A"]] + [("B", t) for t in banks["B"]])
    for cam, tid in order:
        bank = banks[cam][tid]
        if bank.n < args.min_samples:
            continue
        res = reg.resolve(f"CAM-{cam}", tid, bank, now=1000.0)
        refs[cam][tid] = res.global_ref

    # --- score -------------------------------------------------------------
    # A ground-truth pair counts only if BOTH tracks were resolvable.
    gt_pairs = [(a, b) for (a, b), hits in pair_hits.items()
                if hits >= 3 and a in refs["A"] and b in refs["B"]]
    matched = [(a, b) for a, b in gt_pairs if refs["A"][a] == refs["B"][b]]

    # False merges: two tracks sharing a ref that ground truth never paired.
    truth = {(a, b) for a, b in pair_hits if pair_hits[(a, b)] >= 3}
    false_merges = []
    for a, aref in refs["A"].items():
        for b, bref in refs["B"].items():
            if aref == bref and (a, b) not in truth:
                false_merges.append((a, b))

    # --- separability: what the matcher actually has to work with ----------
    # The threshold can only do useful work if true-pair similarities sit well
    # above false-pair ones. If the two distributions overlap, no threshold
    # choice will fix it, and a sweep over thresholds will look flat and
    # meaningless. Reporting the gap makes that visible instead of implied.
    true_sims, false_sims = [], []
    for a in refs["A"]:
        for b in refs["B"]:
            sim = banks["A"][a].similarity(banks["B"][b])
            (true_sims if (a, b) in truth else false_sims).append(sim)

    def stat(vals):
        if not vals:
            return None
        s = sorted(vals)
        return {"n": len(s), "min": round(s[0], 4),
                "median": round(s[len(s) // 2], 4), "max": round(s[-1], 4)}

    separability = {
        "true_pairs": stat(true_sims),
        "false_pairs": stat(false_sims),
        "gap_min_true_minus_max_false": (
            round(min(true_sims) - max(false_sims), 4)
            if true_sims and false_sims else None),
    }

    total_refs = len({*refs["A"].values(), *refs["B"].values()})
    report = {
        "separability": separability,
        "source": args.source,
        "frames": n,
        "seconds": round(elapsed, 1),
        "fps": round(n / elapsed, 2) if elapsed else 0.0,
        "params": {"threshold": args.threshold, "margin": args.margin,
                   "gt_iou": args.gt_iou, "min_samples": args.min_samples},
        "tracks": {"cam_a": len(refs["A"]), "cam_b": len(refs["B"])},
        "ground_truth_pairs": len(gt_pairs),
        "matched_pairs": len(matched),
        "match_rate": round(len(matched) / len(gt_pairs), 3) if gt_pairs else None,
        "false_merges": len(false_merges),
        "identities_created": total_refs,
        "identities_ideal": len(gt_pairs) + (len(refs["A"]) - len(gt_pairs)) +
                            (len(refs["B"]) - len(gt_pairs)),
        "registry_stats": reg.snapshot()["stats"],
        "caveat": ("Camera B is a transformed copy of camera A, so the two views "
                   "share clothing, pose and lighting. These numbers validate the "
                   "pipeline and threshold behaviour end to end; they do NOT "
                   "predict accuracy on a genuine second camera."),
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)

    print("\n=== cross-camera identity evaluation ===")
    for k in ("frames", "fps", "tracks", "ground_truth_pairs", "matched_pairs",
              "match_rate", "false_merges", "identities_created"):
        print(f"  {k:22s} {report[k]}")
    print(f"  registry_stats         {report['registry_stats']}")
    print(f"  separability           true={separability['true_pairs']}")
    print(f"                         false={separability['false_pairs']}")
    print(f"                         gap={separability['gap_min_true_minus_max_false']}")
    print(f"\n  wrote {args.out}")
    print("  NOTE:", report["caveat"])


if __name__ == "__main__":
    main()
