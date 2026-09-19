#!/usr/bin/env python3
"""Run the demo questions through the MCP server and print condensed answers.

    .venv/bin/python scripts/mcp_demo.py                       # http://127.0.0.1:8010/mcp, token from .env
    .venv/bin/python scripts/mcp_demo.py --url http://host:8010/mcp --token <token>
    .venv/bin/python scripts/mcp_demo.py --hours 24 --only 4,19,22

The MCP server exposes TOOLS; the natural-language step belongs to the
chatbot. Each question below is paired with the tool call the chatbot
should make for it, so this shows what the server actually returns — the
raw material a good answer is built from, and a check that every tool is
reachable with the token in use. Nothing here writes, except question 11
(acknowledge), which is skipped unless --ack <alert_id> is given.
"""
import argparse
import asyncio
import json
import os
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO)


def _env():
    p = os.path.join(REPO, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def _short(x, n=900):
    s = json.dumps(x, ensure_ascii=False, default=str) if not isinstance(x, str) else x
    return s if len(s) <= n else s[:n] + f" … (+{len(s) - n} chars)"


def questions(hours):
    H = hours
    return [
        (1, "What sites and cameras are connected?", "network_overview", {}),
        (2, "Is the system healthy right now?", "system_health", {}),
        (3, "What rules are you enforcing and what do they mean?", "rules_reference", {}),
        (4, "How many people are in the building right now?", "facility_occupancy", {}),
        (5, "Which zones have people in them and how crowded are they?", "zones_live", {}),
        (6, "Is anyone in a restricted area right now?", "restricted_zones", {}),
        (7, "Show me what the first camera sees right now.", "camera_snapshot", {"camera_id": "@first_camera"}),
        (8, "Are there any open alerts?", "alerts_active", {}),
        (9, "What was the latest restricted-zone intrusion?", "alerts_history", {"hours": H, "rule_id": "R-06", "limit": 3}),
        (10, "Give me a link to the frame for the latest alert.", "incident_frame_link", {"alert_id": "@latest_alert"}),
        (11, "Acknowledge that alert on behalf of Mohammed.", "acknowledge_alert", {"alert_id": "@ack", "by": "Mohammed"}),
        (12, "How many critical alerts this week, by rule?", "alerts_history", {"hours": 168, "severity": "CRITICAL", "limit": 500}),
        (13, "How many people were in the busiest zone one hour ago?", "zone_at_time", {"zone_id": "@first_zone", "camera_id": "@first_zone_cam", "ts": time.time() - 3600}),
        (14, "When was that zone busiest in the window?", "zone_history", {"zone_id": "@first_zone", "camera_id": "@first_zone_cam", "hours": H}),
        (15, "How long did the last person spend in a restricted zone?", "zone_duration", {"zone_id": "@restricted_zone", "camera_id": "@restricted_zone_cam", "hours": H}),
        (16, "Which direction do people mostly move between zones?", "zone_movement", {"minutes": H * 60}),
        (17, "Generate an occupancy report for the window.", "occupancy_report", {"hours": H}),
        (18, "How many people came in and left in the window?", "events_history", {"hours": H, "event_type": "FACILITY_ENTRY", "limit": 2000}),
        (19, "Find everyone wearing a black top.", "find_people", {"upper_colour": "black", "hours": H, "limit": 20}),
        (20, "Anyone with a backpack?", "find_people", {"bag": "backpack", "hours": H, "limit": 20}),
        (21, "Show me the crop for the first hit.", "sighting_crop_link", {"sighting_id": "@first_sighting"}),
        (22, "Where else was that person seen?", "person_timeline", {"global_ref": "@first_person", "hours": H}),
        (23, "Did anyone in a lab coat enter a restricted zone?", "find_people", {"outerwear": "lab coat", "hours": H, "limit": 50}),
        (24, "Find the woman in the red abaya (must search abaya+red only, never gender).", "find_people", {"outerwear": "abaya", "upper_colour": "red", "hours": H}),
        (25, "Where is the delivery van now?", "vehicles", {}),
        (26, "Which vehicles arrived today?", "vehicle_arrivals", {"hours": H}),
    ]


async def main(args):
    import httpx2 as httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    hc = httpx.AsyncClient(headers={"Authorization": f"Bearer {args.token}"} if args.token else {}, timeout=60)
    async with streamable_http_client(args.url, http_client=hc) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            print(f"connected: {args.url}  tools: {len(names)}\n")

            ctx = {}

            async def call(tool, params):
                r = await s.call_tool(tool, params)
                if getattr(r, "is_error", False):
                    return {"error": True, "text": " ".join(getattr(c, "text", "") for c in r.content)}
                blocks = r.content
                if blocks and getattr(blocks[0], "type", "") == "image":
                    return {"image": True, "bytes": len(blocks[0].data or "") * 3 // 4, "mime": getattr(blocks[0], "mime_type", None)}
                text = "".join(getattr(c, "text", "") for c in blocks)
                try:
                    return json.loads(text)
                except ValueError:
                    return text

            def resolve(params):
                out = {}
                for k, v in params.items():
                    if isinstance(v, str) and v.startswith("@"):
                        if v[1:] not in ctx:
                            return None
                        out[k] = ctx[v[1:]]
                    else:
                        out[k] = v
                return out

            only = {int(x) for x in args.only.split(",")} if args.only else None
            for n, q, tool, params in questions(args.hours):
                if only and n not in only:
                    continue
                print(f"Q{n}. {q}")
                if tool not in names:
                    print(f"   tool {tool} not on this server\n"); continue
                if n == 11 and not args.ack:
                    print("   skipped (a write; pass --ack <alert_id> to run it)\n"); continue
                if n == 11:
                    ctx["ack"] = args.ack
                p = resolve(params)
                if p is None:
                    print(f"   skipped: needs a value an earlier answer did not provide ({params})\n"); continue
                t0 = time.time()
                try:
                    a = await call(tool, p)
                except Exception as e:                       # noqa: BLE001
                    print(f"   {tool}({_short(p, 120)}) -> EXCEPTION {e}\n"); continue
                dt = time.time() - t0
                print(f"   {tool}({_short(p, 160)})  [{dt:.2f}s]")

                # harvest ids later questions need
                if isinstance(a, dict):
                    cams = a.get("cameras") if tool == "network_overview" else None
                    if tool == "network_overview":
                        flat = []
                        def walk(node):
                            if isinstance(node, dict):
                                cs = node.get("cameras")
                                if isinstance(cs, list):
                                    flat.extend(c for c in cs if isinstance(c, dict))
                                for v in node.values():
                                    if isinstance(v, (list, dict)):
                                        walk(v)
                            elif isinstance(node, list):
                                for v in node:
                                    walk(v)
                        walk(a)
                        if flat:
                            ctx["first_camera"] = flat[0].get("camera_id")
                    if tool == "cameras" or ("first_camera" not in ctx and tool == "network_overview"):
                        try:
                            cams = await call("cameras", {})
                            cl = cams.get("cameras") if isinstance(cams, dict) else None
                            if cl:
                                ctx["first_camera"] = cl[0].get("camera_id")
                        except Exception:                    # noqa: BLE001
                            pass
                    if tool == "zones_live":
                        zs = a.get("zones") or []
                        busiest = sorted(zs, key=lambda z: -(z.get("occupancy") or 0))
                        if busiest:
                            ctx["first_zone"], ctx["first_zone_cam"] = busiest[0]["zone_id"], busiest[0]["camera_id"]
                        rz = [z for z in zs if z.get("restricted")]
                        if rz:
                            ctx["restricted_zone"], ctx["restricted_zone_cam"] = rz[0]["zone_id"], rz[0]["camera_id"]
                    if tool == "alerts_history" and n == 9:
                        al = a.get("alerts") or []
                        if al:
                            ctx["latest_alert"] = al[0].get("alert_id")
                    if tool == "alerts_active" and "latest_alert" not in ctx:
                        al = a.get("alerts") or []
                        if al:
                            ctx["latest_alert"] = al[0].get("alert_id")
                    if tool == "find_people" and n == 19:
                        ppl = a.get("people") or []
                        if ppl:
                            ctx["first_person"] = ppl[0]["person"]
                            if ppl[0].get("sightings"):
                                ctx["first_sighting"] = ppl[0]["sightings"][0].get("sighting_id")

                # condensed answer
                if isinstance(a, dict) and a.get("error"):
                    print(f"   -> ERROR: {_short(a.get('text'), 400)}")
                elif isinstance(a, dict) and a.get("image"):
                    print(f"   -> image block, {a['bytes']} bytes {a.get('mime')}")
                elif tool == "network_overview":
                    print(f"   -> {_short({k: a.get(k) for k in ('tenant', 'counts', 'rollup', 'totals') if k in a}, 500)}")
                elif tool == "facility_occupancy":
                    print(f"   -> occupancy {a.get('occupancy')} (observed {a.get('observed')}, stale {a.get('stale')}), doors {a.get('doors')}")
                elif tool == "zones_live":
                    zs = a.get("zones") or []
                    print(f"   -> {len(zs)} zones; " + "; ".join(f"{z['zone_id']}@{z['camera_id']} occ={z.get('occupancy')} {z.get('status')}" for z in zs[:8]))
                elif tool == "restricted_zones":
                    print(f"   -> {_short(a, 500)}")
                elif tool in ("alerts_active", "alerts_history"):
                    al = a.get("alerts") or []
                    if n == 12:
                        from collections import Counter
                        print(f"   -> {len(al)} alerts: {dict(Counter(x.get('rule_id') for x in al))}")
                    else:
                        print(f"   -> {len(al)} alerts; " + "; ".join(f"{x.get('alert_id')} {x.get('rule_id')} {x.get('severity')} {x.get('zone_id')} {x.get('status')}" for x in al[:3]))
                elif tool == "events_history":
                    ev = a.get("events") or []
                    refs = {e.get("global_ref") or e.get("person_ref") for e in ev}
                    print(f"   -> {len(ev)} FACILITY_ENTRY events, {len(refs)} distinct people (run again with event_type=FACILITY_EXIT for departures)")
                elif tool == "find_people":
                    ppl = a.get("people") or []
                    print(f"   -> {a.get('count')} people ({a.get('exact')} exact), {a.get('sightings')} sightings; near colours {a.get('near_colours')}")
                    for p in ppl[:3]:
                        s0 = p["sightings"][0]
                        print(f"      {p['person']} [{p.get('match')}] {p.get('description')} · {', '.join(p.get('cameras', []))} · crop: {s0.get('crop_url') or '-'}")
                elif tool == "person_timeline":
                    ss = a.get("sightings") or []
                    print(f"   -> {len(ss)} sightings: " + "; ".join(f"{time.strftime('%H:%M', time.localtime(x['ts']))} {x.get('camera_id')}/{x.get('zone_id')}" for x in ss[:6]))
                else:
                    print(f"   -> {_short(a, 500)}")
                print()
    return 0


if __name__ == "__main__":
    _env()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default=f"http://127.0.0.1:{os.environ.get('FINBLADE_MCP_PORT', '8010')}/mcp")
    ap.add_argument("--token", default=os.environ.get("FINBLADE_MCP_TOKEN"))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--only", default=None, help="comma-separated question numbers")
    ap.add_argument("--ack", default=None, help="alert id to acknowledge for Q11 (a write; off by default)")
    sys.exit(asyncio.run(main(ap.parse_args())))
