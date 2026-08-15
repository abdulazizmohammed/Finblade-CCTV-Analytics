#!/usr/bin/env python3
"""Read-only queries against wisenet_streams.json, in shell-friendly TSV.

Subcommands:
  sets                 -> setnum  ncams  WxH  fps  duration  codec
  setnums              -> setnum (one per line)
  cams <n>             -> cam_id  source  fps  width  height  codec  duration
  meta                 -> key  value
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPPING = os.path.join(ROOT, "wisenet_streams.json")


def load():
    with open(MAPPING) as fh:
        return json.load(fh)


def set_keys(data):
    keys = [k for k in data if k.startswith("set_")]
    return sorted(keys, key=lambda k: int(k.split("_")[1]))


def fmt(v, dash="?"):
    return dash if v is None else str(v)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: query.py {sets|setnums|cams <n>|meta}")
    cmd = sys.argv[1]
    data = load()

    if cmd == "setnums":
        for k in set_keys(data):
            print(k.split("_")[1])
        return

    if cmd == "sets":
        for k in set_keys(data):
            cams = data[k]
            first = cams[sorted(cams)[0]] if cams else {}
            res = "%sx%s" % (fmt(first.get("width")), fmt(first.get("height")))
            print("\t".join([
                k.split("_")[1],
                str(len(cams)),
                res,
                fmt(first.get("fps")),
                fmt(first.get("duration_sec")),
                fmt(first.get("codec")),
            ]))
        return

    if cmd == "cams":
        if len(sys.argv) < 3:
            sys.exit("usage: query.py cams <setnum>")
        key = "set_%s" % sys.argv[2]
        if key not in data:
            sys.exit("unknown set: %s" % key)
        cams = data[key]
        for cam in sorted(cams, key=lambda c: int(re.sub(r"\D", "", c) or 0)):
            e = cams[cam]
            print("\t".join([
                cam,
                e["source"],
                fmt(e.get("fps"), "25"),
                fmt(e.get("width"), "640"),
                fmt(e.get("height"), "480"),
                fmt(e.get("codec")),
                fmt(e.get("duration_sec")),
                e.get("source_name", ""),
            ]))
        return

    if cmd == "meta":
        for k, v in data.get("_meta", {}).items():
            print("%s\t%s" % (k, v))
        return

    sys.exit("unknown subcommand: %s" % cmd)


if __name__ == "__main__":
    main()
