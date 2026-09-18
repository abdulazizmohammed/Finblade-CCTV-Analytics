# FinBlade CCTV — MCP server

**For:** the FinBlade platform developer wiring the chatbot.
**Replaces:** copying `integrations/finblade_ai/tools.py` into your agent loop.
**Does not replace:** the SQL views (`CHATBOT_DATABASE_INSTRUCTIONS.md`) — those
stay for ad-hoc analyst queries. This is the chatbot's front door.

The server exposes the whole system as [Model Context Protocol](https://modelcontextprotocol.io)
tools: hierarchy, cameras, zones, occupancy, restricted zones, alerts, events,
reports, facility presence, vehicle tracking, health. Your chatbot connects,
lists the tools, and hands them to the model. The caveats that make the
numbers correct live in the tool descriptions, so every client gets them.

---

## 0. The Wareed test instance

| | |
|---|---|
| MCP endpoint | `http://ec2-98-80-30-36.compute-1.amazonaws.com:8010/mcp` |
| REST API (what the tools call; also the `links` in webhook payloads) | `http://ec2-98-80-30-36.compute-1.amazonaws.com:8000` |
| Auth | `Authorization: Bearer <FINBLADE_MCP_TOKEN>` — sent separately, never in the same message as this URL |
| Transport | streamable HTTP, JSON responses, stateless |
| Quick check | `curl -s -o /dev/null -w '%{http_code}\n' -X POST …:8010/mcp -H 'Content-Type: application/json' -d '{}'` → `401` means up and gated; with the token, `initialize` → `200` |

Plain HTTP for now: use it from your backend, not from a browser, and not
through Anthropic's remote MCP connector until it sits behind TLS (§2).
Port 8010 must be opened in the instance's security group for your
backend's egress address.

## 1. Run it

```bash
# beside the API, on the CCTV host
FINBLADE_MCP_TOKEN=<secret>            # what MCP clients must present
CCTV_API_KEY=<integration key>         # only if the API has FINBLADE_API_KEY set
.venv/bin/python -m services.mcp.server --port 8010
# -> http://<cctv-host>:8010/mcp   (streamable HTTP, JSON responses)
```

`scripts/start_mcp.sh` does the same with the values from `.env`.
`--stdio` serves over stdio for a local desktop client instead.

**As a service** (survives reboot, like the API): put `FINBLADE_MCP_TOKEN`
in `.env` and run `sudo bash scripts/install_service.sh` — it installs
`finblade-mcp.service` beside `finblade-api.service` when the token is
present. `sudo systemctl restart finblade-mcp`, `journalctl -u finblade-mcp -f`.

Without `FINBLADE_MCP_TOKEN` the endpoint is **open** (a warning is printed) —
same posture as the API with no key. Set it on anything reachable beyond the
host.

**Appearance search from the chatbot.** `find_people` / `person_timeline` are
full-key routes on the API; with the integration key the server normally
holds they return the API's 403 as the tool error. To enable them set
`FINBLADE_MCP_SEARCH_KEY=<the full FINBLADE_API_KEY>` in `.env` — it is sent
on `/api/v1/search/*` **only**; every other tool keeps the integration key's
read-only scope. This is an operator decision: it lets anyone who can reach
the chatbot run description searches, each of which is audited.

## 2. Connect

**From your backend (recommended — works on-prem / air-gapped / over Tailscale):**

```python
import asyncio, httpx2 as httpx          # anthropic 1.x and mcp 2.x both use httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    hc = httpx.AsyncClient(headers={"Authorization": "Bearer <secret>"}, timeout=30)
    async with streamable_http_client("http://ec2-98-80-30-36.compute-1.amazonaws.com:8010/mcp", http_client=hc) as (read, write, *_):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools          # -> pass to the model as tools
            prompt = (await s.get_prompt("cctv_analyst", {"tenant": "Wareed", "timezone": "Asia/Riyadh"}))
            r = await s.call_tool("network_overview", {})
            print(r.content[0].text)
asyncio.run(main())
```

Map each MCP tool to a Messages API tool (`name`, `description`, `input_schema`)
and execute `tool_use` blocks with `s.call_tool(name, input)`. Image tools
(`camera_snapshot`, `incident_frame`, `sighting_crop`) return an `image`
content block — pass it to the model as an image **and render it to the
user**; a search hit without its crop is a description, not evidence.

**If your chat cannot render image blocks**, nothing is lost: every
`find_people` sighting carries `crop_url`, and `sighting_crop_link` /
`incident_frame_link` return the same kind of URL on demand. They are signed
and expire (default 60 min, `FINBLADE_LINK_TTL_MINUTES`), carry no API key,
and open exactly one image — safe to print in a transcript. Render them as
links. The host must be reachable from the user's browser: set
`FINBLADE_PUBLIC_URL=http://ec2-98-80-30-36.compute-1.amazonaws.com:8000`
in the CCTV host's `.env`.

**Through Anthropic's remote MCP connector** (`mcp_servers=[{type:"url",…}]` +
`tools=[{type:"mcp_toolset", mcp_server_name:…}]`, beta `mcp-client-2025-11-20`):
only when the server is reachable from the public internet over HTTPS — i.e.
the AWS deployment, not the on-prem host.

## 3. The tools

| Area | Tools |
|---|---|
| Network | `network_overview` (tree + roll-ups), `org_index`, `branch`, `summary` |
| Cameras | `cameras`, `camera`, `camera_snapshot` (image) |
| Zones | `zones_live`, `zone_config`, `restricted_zones` (intrusions now), `zone_history`, `zone_at_time`, `zone_duration`, `zone_movement` |
| Occupancy | `areas_live` (distinct people per room), `facility_occupancy` (door-counted), `people_counts` |
| Alerts | `alerts_active`, `alerts_history`, `alert`, `incident_frame` (image), `acknowledge_alert`, `resolve_alert` |
| Events / reports | `events_history`, `occupancy_report`, `reports_list` |
| Vehicles | `vehicles`, `vehicle`, `vehicle_track`, `vehicle_arrivals`, `vehicles_at_branch` |
| Appearance | `find_people` (description → candidates grouped by person, each sighting with a clickable `crop_url`; audited), `person_timeline`, `sighting_crop` (image block), `sighting_crop_link` (URL) |
| Images as links | `sighting_crop_link`, `incident_frame_link` — signed, expiring (default 60 min), key-free URLs to one image, for a chat that cannot render image blocks. Set `FINBLADE_PUBLIC_URL` on the CCTV host to the address users' browsers can reach |
| System | `system_health`, `rules_reference` |

Every listing tool takes `region_id` / `city_id` / `branch_id` (they intersect).
Time windows: `hours` back from now, or `from_ts` / `to_ts` epoch seconds UTC.

**Resources:** `finblade://data-notes` (read this first), `finblade://rules`,
`finblade://capabilities`. **Prompt:** `cctv_analyst(tenant, timezone)`.

## 4. What the model must not get wrong — and how the tools stop it

| Trap | Guard |
|---|---|
| A missing row means zero | history tools hold readings forward and return `coverage`; descriptions say to qualify below 0.95 |
| A `null` bucket is an empty room | it means "camera not watching"; the description says so |
| `zone_id` identifies a zone | unique only within a camera; the API returns 409 with candidates, surfaced verbatim as the tool error |
| Summing camera counts = people | `areas_live` and `summary.people_in_zones` are distinct-person; `people_in_view` says "may double count" |
| A tracker is a driver | it is a vehicle or asset; no driver field exists anywhere |
| An appearance match identifies someone | `find_people` returns candidates matching a clothing description, never an identity; the tool description forbids gender/age/ethnicity and tells the model to say "matches the description" |
| A colour tag is the colour | it is what CLIP read off a CCTV crop (a white shirt was stored as grey); `find_people` also returns look-alike colours as `match: near`, and the description tells the model to report those as "possibly, tagged as grey" |
| R-10 / R-11 are verdicts | they are evaluation models; the rule reference says so |

## 5. Security

- Read-only except `acknowledge_alert` / `resolve_alert` — exactly the
  integration role's write set. Nothing can delete, provision or reconfigure.
- The API redacts RTSP credentials before anything reaches this server.
- No person identifier that is not an opaque salted hash ever appears.
- Images (snapshots, incident frames) are of a monitored space; treat them
  under the same PDPL retention as the API does.

Tests: `tests/test_mcp_server.py` — every tool through the MCP layer against
the in-memory API, plus the bearer gate over the real transport.
