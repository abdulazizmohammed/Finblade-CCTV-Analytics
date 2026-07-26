#!/usr/bin/env python3
"""Publish a local video file to an RTSP server (looping, real-time), so the
pipeline can be tested against an RTSP camera source instead of a file.

Self-contained: uses PyAV (bundles ffmpeg). It PUBLISHES to a running RTSP server
(MediaMTX, started by scripts/rtsp_stream.sh) rather than trying to be a server
itself — ffmpeg's built-in RTSP listen mode is unreliable for real clients.

Usage:
    scripts/rtsp_stream.py <video-file> <rtsp-publish-url>
e.g. scripts/rtsp_stream.py media/CAM01_T02_tracking_occupancy.mp4 rtsp://127.0.0.1:8554/cam01
"""
import sys
import time

import av


def serve(src: str, url: str) -> None:
    while True:                                   # keep (re)publishing forever
        try:
            _serve_once(src, url)
        except Exception as e:                    # server not up yet / dropped: retry
            print(f"[rtsp] publish ended ({type(e).__name__}: {e}); retrying…",
                  flush=True)
            time.sleep(1.0)


def _serve_once(src: str, url: str) -> None:
    inp = av.open(src)
    ivs = inp.streams.video[0]
    # publish to the RTSP server over TCP (reliable on loopback/LAN)
    out = av.open(url, mode="w", format="rtsp",
                  options={"rtsp_transport": "tcp"})
    try:
        ovs = out.add_stream_from_template(ivs)      # PyAV >= 10: copy codec params
    except AttributeError:                           # older PyAV fallback
        ovs = out.add_stream(ivs.codec_context.name)
        ovs.width, ovs.height = ivs.width, ivs.height
        ovs.pix_fmt = ivs.pix_fmt
    print(f"[rtsp] publishing {src} -> {url}", flush=True)
    tb = float(ivs.time_base)
    loops = 0
    try:
        while True:                               # loop the clip
            base = time.time()
            first = None
            for packet in inp.demux(ivs):
                if packet.dts is None:            # skip flush packets
                    continue
                if first is None:
                    first = packet.pts
                # real-time pacing so the consumer sees ~source fps
                target = (packet.pts - first) * tb
                delay = target - (time.time() - base)
                if delay > 0:
                    time.sleep(delay)
                packet.stream = ovs
                out.mux(packet)
            loops += 1
            inp.seek(0)                           # rewind and loop
    finally:
        try:
            out.close()
        except Exception:
            pass
        inp.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    serve(sys.argv[1], sys.argv[2])
