#!/usr/bin/env python3
"""Run PPE compliance against a local video and print the per-track timeline.

Manual validation for Phase 3 (spec section 12). Uses the SAME association
helper and the SAME state machine as the worker — finblade.geometry and
finblade.ppe — so what you watch here is what the pipeline would decide, not a
parallel reimplementation that can drift.

    .venv/bin/python scripts/ppe_check.py --source media/PPEVideo.mp4 \
        --required hardhat,safety_vest --zone ASSEMBLY

Output:

    12:01:03 CAM-01 track=17 zone=ASSEMBLY hardhat=YES vest=YES
    12:01:06 CAM-01 track=21 zone=ASSEMBLY hardhat=NO  state=CANDIDATE
    12:01:10 R-11 OPEN track=21 missing_hardhat
    12:01:22 R-11 RESOLVED track=21

WHAT THIS CANNOT TELL YOU. Whether the verdicts are RIGHT. It reports what the
model and the rules decided; judging that needs eyes on the video. Use
--save-frames to write annotated stills for the violations and check them.
"""
import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade.geometry import associate_items                  # noqa: E402
from finblade.ppe import (COMPLIANT, NONCOMPLIANT, PPEThresholds,  # noqa: E402
                          PPETracker, evidence_for)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--zone", default="ZONE-PPE")
    ap.add_argument("--required", default="hardhat,safety_vest",
                    help="comma-separated: hardhat,safety_vest,mask")
    ap.add_argument("--camera", default="CAM-01")
    ap.add_argument("--person-weights", default="models/yolo11s.pt")
    ap.add_argument("--ppe-weights", default="",
                    help="override the checkpoint's default weights path")
    # The medical profile picks its class map, default weights and
    # class-order assertion from ppe_client.MEDICAL_CHECKPOINTS — the same
    # registry the worker reads — so this script cannot drift from it.
    ap.add_argument("--profile", choices=("industrial", "medical"),
                    default="industrial")
    ap.add_argument("--checkpoint", default="",
                    help="medical only: key in MEDICAL_CHECKPOINTS "
                         "(default finblade_lab_yolo11s)")
    ap.add_argument("--conf", type=float, default=None,
                    help="visible threshold (default 0.35 industrial, 0.5 medical)")
    ap.add_argument("--iou", type=float, default=None,
                    help="NMS IoU (default: ultralytics' own; 0.5 medical)")
    ap.add_argument("--absence-weight", type=float, default=None,
                    help="how much silence counts (default 0.25 industrial, "
                         "0.0 medical — see cameras.template.yaml)")
    ap.add_argument("--raw-log", default="",
                    help="directory for the raw-detection journal "
                         "<dir>/<camera>.jsonl (every box >= --raw-log-conf)")
    ap.add_argument("--raw-log-conf", type=float, default=0.25)
    ap.add_argument("--device", default="0")
    ap.add_argument("--frames", type=int, default=1200)
    ap.add_argument("--interval", type=float, default=0.5,
                    help="seconds between PPE inferences (2 Hz default)")
    ap.add_argument("--entry-grace", type=float, default=5.0)
    ap.add_argument("--violation-confirm", type=float, default=8.0)
    ap.add_argument("--recovery-confirm", type=float, default=5.0)
    ap.add_argument("--save-frames", default="",
                    help="directory for annotated violation stills")
    args = ap.parse_args()

    required = [r.strip() for r in args.required.split(",") if r.strip()]
    if not os.path.exists(args.source):
        sys.exit("no such file: %s" % args.source)

    import cv2
    from ultralytics import YOLO
    from services.inference.ppe_client import (DEFAULT_MEDICAL_CHECKPOINT,
                                               MEDICAL_CHECKPOINTS, PPEDetector)

    person = YOLO(args.person_weights)
    medical = args.profile == "medical"
    det_kw = dict(device=args.device, interval_s=args.interval, enabled=True,
                  profile=args.profile,
                  conf_threshold=(args.conf if args.conf is not None
                                  else (0.5 if medical else 0.35)),
                  iou_threshold=(args.iou if args.iou is not None
                                 else (0.5 if medical else None)),
                  raw_log_dir=args.raw_log or None,
                  raw_log_conf=args.raw_log_conf if args.raw_log else None)
    if medical:
        name = args.checkpoint or DEFAULT_MEDICAL_CHECKPOINT
        if name not in MEDICAL_CHECKPOINTS:
            sys.exit("unknown --checkpoint %r; choose from %s"
                     % (name, sorted(MEDICAL_CHECKPOINTS)))
        ck = MEDICAL_CHECKPOINTS[name]
        det_kw.update(weights=args.ppe_weights or ck["weights"],
                      class_map=ck["class_map"],
                      ignored_classes=ck["ignored_classes"],
                      expected_names=ck["expected_names"])
    else:
        det_kw.update(weights=args.ppe_weights or "models/ppe_safetyvision_v2.pt")
    ppe = PPEDetector(args.camera, **det_kw)
    if not ppe.load():
        sys.exit("PPE detector unavailable: %s" % ppe.status)
    unserved = [r for r in required if r not in ppe.served_types]
    if unserved:
        # Same rule as the worker's ppe_served: an item the checkpoint has no
        # class for is not judged, because it would be judged on silence.
        print("NOT JUDGED (checkpoint has no class for them): %s" % unserved)
        required = [r for r in required if r in ppe.served_types]

    absence = (args.absence_weight if args.absence_weight is not None
               else (0.0 if medical else 0.25))
    tracker = PPETracker(args.camera, PPEThresholds(
        entry_grace_s=args.entry_grace,
        violation_confirm_s=args.violation_confirm,
        recovery_confirm_s=args.recovery_confirm,
        absence_weight_by_profile={args.profile: absence}))

    if args.save_frames:
        os.makedirs(args.save_frames, exist_ok=True)

    cap = cv2.VideoCapture(args.source, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        sys.exit("cannot open %s" % args.source)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0

    print("source   : %s" % args.source)
    print("model    : %s (%s)  conf %.2f  iou %s  absence_weight %.2f"
          % (ppe.weights, args.profile, ppe.conf_threshold,
             ppe.iou_threshold, absence))
    print("zone     : %s   required: %s" % (args.zone, ", ".join(required)))
    print("timers   : grace %.0fs  violate %.0fs  recover %.0fs"
          % (args.entry_grace, args.violation_confirm, args.recovery_confirm))
    if ppe.raw_log_path:
        print("raw log  : %s (>= %.2f)" % (ppe.raw_log_path, ppe.raw_log_conf))
    print("NOTE: video time, not wall clock. Verdicts are the model's; whether"
          " they are correct needs your eyes.")
    print()

    last_seen, last_line = {}, {}
    opens = defaultdict(dict)
    n = saved = 0
    t_wall0 = time.time()
    while n < args.frames:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        vnow = n / fps                       # video time
        if not ppe.due(vnow):
            continue

        res = person.track(frame, persist=True, classes=[0], conf=0.4,
                           imgsz=960, tracker="bytetrack.yaml",
                           device=args.device, verbose=False)[0]
        if res.boxes is None or res.boxes.id is None:
            continue
        people = {int(t): tuple(map(float, b))
                  for t, b in zip(res.boxes.id.tolist(), res.boxes.xyxy.tolist())}

        dets = [d for d in ppe.detect(frame, vnow, frame_id=n)
                if d["class_name"] != "person"]
        items = [(d["bbox"], d["class_name"].replace("no_", "")) for d in dets]
        owned = defaultdict(list)
        for idx, owner, _ in associate_items(items, people):
            if owner is not None:
                owned[owner].append((dets[idx]["class_name"],
                                     dets[idx]["confidence"]))

        stamp = "%02d:%02d:%02d" % (int(vnow) // 3600, int(vnow) // 60 % 60,
                                    int(vnow) % 60)
        for tid in people:
            tracker.note_in_zone(tid, args.zone, vnow)
            if tracker.in_grace(tid, args.zone, vnow):
                continue
            dt = vnow - last_seen.get(tid, vnow - args.interval)
            last_seen[tid] = vnow
            bits = []
            for req in required:
                ev, conf = evidence_for(req, owned.get(tid, []))
                new = tracker.observe(tid, req, ev, conf, vnow, dt)
                verdict = {"positive": "YES", "negative": "NO",
                           "absent": "?"}[ev]
                bits.append("%s=%-3s" % (req, verdict))
                if new == NONCOMPLIANT and req not in opens[tid]:
                    opens[tid][req] = vnow
                    print("%s R-11 OPEN     %s track=%s zone=%s missing_%s"
                          % (stamp, args.camera, tid, args.zone, req))
                    if args.save_frames:
                        img = res.plot()
                        x1, y1, x2, y2 = (int(v) for v in people[tid])
                        cv2.rectangle(img, (x1, y1), (x2, y2), (75, 75, 239), 3)
                        cv2.putText(img, "R-11 track=%s missing_%s" % (tid, req),
                                    (x1, max(20, y1 - 8)),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                    (75, 75, 239), 2)
                        p = "%s/r11_t%s_%s_%06d.jpg" % (args.save_frames, tid,
                                                        req, n)
                        cv2.imwrite(p, img)
                        saved += 1
                elif new == COMPLIANT and req in opens[tid]:
                    held = vnow - opens[tid].pop(req)
                    print("%s R-11 RESOLVED %s track=%s %s (held %.0fs)"
                          % (stamp, args.camera, tid, req, held))
            line = "track=%s zone=%s %s" % (tid, args.zone, " ".join(bits))
            states = " ".join("%s:%s" % (r, tracker.verdict(tid, r))
                              for r in required)
            full = line + "  [" + states + "]"
            if last_line.get(tid) != full:      # only print on change
                print("%s %s %s" % (stamp, args.camera, full))
                last_line[tid] = full
    cap.release()

    print()
    print("frames %d, %.1fs wall, PPE runs %d, detections %d, below-threshold %d, "
          "ignored %d, errors %d, journalled %d"
          % (n, time.time() - t_wall0, ppe.stats["runs"], ppe.stats["detections"],
             ppe.stats["below_threshold"], ppe.stats["ignored"],
             ppe.stats["errors"], ppe.stats["logged"]))
    if args.save_frames:
        print("wrote %d violation stills to %s/" % (saved, args.save_frames))
    still_open = {t: list(v) for t, v in opens.items() if v}
    if still_open:
        print("unresolved at end of video: %s" % still_open)


if __name__ == "__main__":
    main()
