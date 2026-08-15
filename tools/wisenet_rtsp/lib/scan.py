#!/usr/bin/env python3
"""Scan the WiseNET dataset and write wisenet_streams.json.

Probes every video once with ffprobe and writes a deterministic
set -> camera -> {source, rtsp, codec, resolution, fps, duration} mapping.

The dataset itself is only ever read. Nothing is renamed or modified.
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.environ.get(
    "WISENET_DATASET", "/home/usv/finblade-cctv/media/video_sets"
)
FFPROBE = os.environ.get("WISENET_FFPROBE", os.path.join(ROOT, "bin", "ffprobe"))
OUT = os.path.join(ROOT, "wisenet_streams.json")
RTSP_PORT = 8554


def natural_key(name):
    """Sort video10_2.avi after video10_1.avi and after video9_6.avi."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def wsl_ip():
    """This WSL instance's address, taken from the outbound route.

    Reading the route avoids picking the loopback-scoped WSL DNS address
    (10.255.255.254/32 on lo), which is not reachable from Windows.
    """
    try:
        out = subprocess.run(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "127.0.0.1"


def probe(path):
    """Return codec/width/height/fps/duration for a video file."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,nb_frames:format=duration",
        "-of", "json", path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        data = json.loads(res.stdout or "{}")
    except Exception as exc:                     # probe failure must be visible
        return {"error": str(exc)}

    streams = data.get("streams") or [{}]
    st = streams[0]

    fps = None
    rate = st.get("r_frame_rate", "")
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            if float(den):
                fps = round(float(num) / float(den), 3)
        except ValueError:
            fps = None

    duration = None
    try:
        duration = round(float(data.get("format", {}).get("duration")), 2)
    except (TypeError, ValueError):
        pass

    return {
        "codec": st.get("codec_name"),
        "width": st.get("width"),
        "height": st.get("height"),
        "fps": fps,
        "duration_sec": duration,
        "size_bytes": os.path.getsize(path),
    }


def main():
    if not os.path.isdir(DATASET):
        sys.exit("dataset not found: %s" % DATASET)

    ip = wsl_ip()

    set_dirs = sorted(
        (d for d in os.listdir(DATASET)
         if d.startswith("set_") and os.path.isdir(os.path.join(DATASET, d))),
        key=natural_key,
    )

    out = {
        "_meta": {
            "generated_by": "tools/wisenet_rtsp/lib/scan.py",
            "dataset": DATASET,
            "rtsp_port": RTSP_PORT,
            "server_ip": ip,
            "note": "Test infrastructure only. Source files are read-only.",
        }
    }

    total = 0
    for sd in set_dirs:
        d = os.path.join(DATASET, sd)
        # *.avi only — this skips the ":Zone.Identifier" NTFS sidecar files.
        files = sorted(
            (f for f in os.listdir(d)
             if f.lower().endswith(".avi") and os.path.isfile(os.path.join(d, f))),
            key=natural_key,
        )
        cams = {}
        for i, fname in enumerate(files, start=1):
            cam = "cam_%02d" % i
            src = os.path.join(d, fname)
            path = "%s/%s" % (sd, cam)
            entry = {
                "source": src,
                "source_name": fname,
                "path": path,
                "rtsp": "rtsp://%s:%d/%s" % (ip, RTSP_PORT, path),
                "rtsp_localhost": "rtsp://127.0.0.1:%d/%s" % (RTSP_PORT, path),
            }
            entry.update(probe(src))
            cams[cam] = entry
            total += 1
        out[sd] = cams
        print("%-8s %d cameras" % (sd, len(cams)), file=sys.stderr)

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")

    print("wrote %s (%d sets, %d cameras)" % (OUT, len(set_dirs), total),
          file=sys.stderr)


if __name__ == "__main__":
    main()
