# FinBlade AI ↔ CCTV — native tiles integration

**For:** the FinBlade platform developer.
**Companion to:** `docs/FINBLADE_CLIENT_GUIDE.md` (full route reference).

This is the "native tiles" option: FinBlade renders its own camera, zone, count
and alert tiles from CCTV data. **FinBlade's backend talks to the CCTV host;
FinBlade's browsers never do.**

That one constraint is what makes this option work where an iframe does not:

* **Mixed content.** FinBlade is https, this API is http. An https page cannot
  fetch http — the browser blocks it before it leaves the page. Server-to-server
  has no such rule.
* **The allowlist.** The CCTV host permits one egress IP, not every user's
  browser.
* **The key.** It stays server-side instead of being readable in devtools.

---

## 1. What you get

`cctv_client.py` — drop it into the FinBlade app and put thin views over it.
It handles the three things a bare HTTP call does not:

1. **An allowlist.** `ROUTES` is the entire reachable surface. Nothing that
   mutates state is in it, so an integration bug cannot call
   `DELETE /api/v1/alerts`.
2. **Caching with request coalescing.** Zone state is a 5-second aggregate
   upstream; polling faster returns the same numbers. Twenty open dashboards
   become one upstream call, not twenty per second.
3. **A shared frame cache.** One snapshot poll per camera serves every viewer,
   so upstream load does not scale with how many people have the page open.

It is sync (`requests`) so it drops into a Django view or a Celery task
unchanged. For an async stack, swap the two transport methods for `httpx`.

`tools.py` and `chat.py` — the chatbot half, described in §7.

---

## 2. Setup

```bash
CCTV_BASE_URL=http://<cctv-host>:8000
CCTV_API_KEY=<the integration key — ask us, do not reuse the operator key>
```

Verify from the **FinBlade backend host**, not your laptop, before writing code:

```bash
curl -s -o /dev/null -w 'with key:    %{http_code}\n' -H "Authorization: Bearer $CCTV_API_KEY" $CCTV_BASE_URL/api/v1/summary
curl -s -o /dev/null -w 'without key: %{http_code}\n'                                          $CCTV_BASE_URL/api/v1/summary
```

`200` then `401` means auth and routing are both correct. A hang on the first is
a security-group problem; a `401` is a key problem. They fail differently and
it is worth telling them apart before asking us.

Then:

```python
from cctv_client import client, CCTVError

def dashboard_data(request):
    try:
        snap = client().read("summary")
    except CCTVError as exc:
        return render_last_known(reason=str(exc))     # see §5
    return {
        "cameras": snap["cameras"],
        "zones":   snap["zones"],
        "alerts":  snap["alerts"],
        "people":  client().site_occupancy(snap),     # None == zones not drawn
        "as_of":   snap["ts"],
    }
```

---

## 3. Use the integration key, not the operator key

Ask us for a `FINBLADE_INTEGRATION_KEY`. It is scoped to exactly what this
integration does:

| | operator key | integration key |
|---|---|---|
| every `GET`, and `/ws` | ✅ | ✅ |
| `POST /alerts/{id}/ack` · `/resolve` | ✅ | ✅ |
| `DELETE /alerts`, `DELETE /cameras/{id}` | ✅ | ❌ 403 |
| `POST /zones` (overwrites polygons) | ✅ | ❌ 403 |
| camera provisioning, start/stop, identity tuning | ✅ | ❌ 403 |

`401` means the key is missing or wrong. `403` means the key is valid but the
route is out of scope — do not go hunting for a credentials bug.

The client's allowlist and the scoped key are deliberately redundant. Only the
key survives someone editing `ROUTES`.

---

## 4. Tiles → data

| Tile | Source | Refresh |
|---|---|---|
| Camera grid | `summary["cameras"]` + `client().frame(camera_id)` | 1s / 2s |
| Zone table | `summary["zones"]` | 5s |
| People count | `client().site_occupancy()` or `summary["counts"]["live"]` | 5s |
| Alert feed | `summary["alerts"]`, then `acknowledge()` / `resolve()` | 5s |
| Footfall | `summary["counts"]["unique_total"]` | 5s |

**Camera state:** use `effective_state`, not `state`. Values are `ONLINE`,
`DEGRADED`, `RECONNECTING`, `OFFLINE`, `DISABLED`. `DEGRADED` means frames are
arriving but frozen or slow — it is not offline and operators treat it
differently.

**Camera images:** each row carries `snapshot_path` and `stream_path`, relative
and stable. Proxy `snapshot_path` through your backend. Do **not** proxy the
MJPEG stream: it is ~10× the size of the equivalent H.265, 20–40 Mbit/s **per
viewer** at 1080p, with no fan-out. Every byte would cross your backend.

**Alert frames:** `frame` is non-null only on R-02 (critical density) and R-06
(restricted intrusion). Guard before building an `<img>`.

**Acknowledgement:** pass the real FinBlade user to `acknowledge()`. The CCTV
alert record stores it, so the action is attributable to a person rather than
to "the integration" — better attribution than the CCTV console itself has,
since that uses one shared key.

---

## 5. Failure states — design these now

| Condition | Meaning | Render |
|---|---|---|
| `summary["zones"] == []` | no polygons drawn; occupancy **cannot be computed** | "zones not configured", never `0` |
| `CCTVError` raised | CCTV unreachable | last-known values **with a visible staleness timestamp** |
| `409` on ack/resolve | already in that state, or unknown id — indistinguishable, idempotent | treat as done, stop retrying |
| `counts["unique_total"]` decreased | CCTV service restarted | session boundary, not bad data; do not compute deltas across it |

---

## 6. Three ways to get the numbers wrong

These are not hypothetical — the first one shipped on the CCTV team's own
dashboard and reported 5–7 people while three were in the building.

1. **Never sum `people_in_view` across cameras.** It double-counts anyone
   visible to two cameras and includes detections anywhere in frame, including
   off the monitored floor. Use `site_occupancy()` (people on the monitored
   floor) or `counts["live"]` (distinct humans site-wide), and label which one
   the tile shows.
2. **Never key on `zone_id` alone.** It is unique per camera, not globally.
   `ZONE-01` exists on several cameras as different areas — use
   `CCTVClient.zone_key()`.
3. **Never treat `global_ref` / `person_ref` as stable.** They are salted
   hashes regenerated on every CCTV restart, by design, so the data stays
   non-identifying. Valid within one session only; not database keys.

---

## 7. Limits worth knowing before you design around them

* **Seated people may not appear in zone occupancy.** Zone membership uses the
  foot point — bottom-centre of the bounding box — which for someone sitting
  lands on the seat, possibly outside the polygon. Such a person appears in
  `people_in_view` and in no zone. If a seating area reads 0 with people in it,
  this is why.
* **Cross-camera identity is not validated on real two-camera footage.** The
  matcher runs and is unit-tested, but its accuracy figure comes from a
  synthetic second camera. Do not build anything depending on `counts["live"]`
  being exact until it is measured on site.
* **Detection quality tracks stream quality.** On a 640×360 substream a
  stationary person is detected in roughly half of frames. Counts are smoothed
  to absorb it; a low-resolution feed still produces a noisier number.
* **Timestamps** are epoch seconds as floats from the CCTV host clock. Ordered
  within a camera, not across them.

---

## 8. The chatbot — tools instead of a database view

You asked for a view on our database that the bot could query directly. We are
offering `tools.py` instead, and this section is the reason.

**The database is a SQLite file with no listener.** There is nothing to connect
to. Exposing it means either shipping the file or putting a network database in
front of it — a project, not a permission.

**The schema is not a contract and is still moving.** `zone_state_ts` changed
shape twice this month: it grew a `site_id` column, and it stopped receiving a
row every five seconds. A view pinned to it would have broken silently both
times, and the second break would not have thrown an error — it would have
quietly started returning a fifth of the data.

**The correctness is not in the schema.** These are the rules a `SELECT` does
not carry, all of which we have already got wrong once and fixed:

* A row means "and it stayed that way until the next row". `AVG(occupancy)`
  over rows is wrong whenever rows do not cover equal time, which is now always.
  On our own data the two answers differ by up to 8×.
* An absence of rows is ambiguous — nothing changed, or nobody was watching.
  Separating them needs the event log and the keepalive interval, neither of
  which is in the table.
* `zone_id` is unique only within a camera. Our own report query grouped on it
  alone and merged two unrelated zones into one row; nothing in the output
  showed it.

Whoever writes that SQL has to rediscover all of it. We would rather ship it
once, here, and be the ones who break if it is wrong.

### What you get

Six tools in `tools.py`, each mapping to one read endpoint, plus a
`SYSTEM_PROMPT`:

| Tool | Answers |
|---|---|
| `cctv_live_state` | "how busy is it now", and what zones exist |
| `cctv_zone_history` | "how busy was the lobby this morning" |
| `cctv_zone_at_time` | "how many people were there at 3pm" |
| `cctv_zone_duration` | "how long was it over capacity" |
| `cctv_alerts` | "were there any alerts last night" |
| `cctv_occupancy_report` | "summarise yesterday" |

`chat.py` executes them through `CCTVClient`, so the allowlist, the key and the
cache apply to the bot exactly as to the tiles. `run_turn()` is a complete
worked example against the Anthropic SDK — an explicit tool loop rather than a
helper, because the loop is the part you replace with your own.

### Four things the tool descriptions exist to prevent

The descriptions are longer than usual on purpose. Each one is a mistake a
model makes by default with this data:

1. **Reporting a gap as zero.** A `null` bucket means the camera was not
   observing. "The camera was down between 2am and 6am" is a complete answer;
   "nobody was there" is a wrong one.
2. **Quoting an average without its coverage.** Below 0.95 the model is told to
   say how much of the period was actually watched, first.
3. **Choosing a camera when a zone id is ambiguous.** The API returns 409 with
   the candidates; the model is told to ask rather than pick.
4. **Answering "who".** Person references are salted hashes that do not survive
   a restart. The system prompt says the bot cannot answer identity questions —
   for a surveillance system, implying otherwise is a serious misstatement.

Every schema is `strict` with a closed set of field and operator names, so a
hallucinated argument is a validation error rather than an empty result the
model reports as "never".

### If you still want direct access

Give us the queries you actually need and we will add endpoints for them. That
keeps one implementation of these rules and leaves you free to change your side
without us breaking.

---

## 9. Later: stop polling

Once the polling version is live, have the FinBlade **backend** hold one
WebSocket to `/ws` and fan snapshots out to browsers over your existing channel.
One socket for the whole deployment, the key sent as a proper `Authorization`
header on the handshake rather than a query string, and no `wss://` dependency
on the CCTV host.

Each message is a complete snapshot in the same shape as `/api/v1/summary`, so
there is no delta state to reconstruct and a missed message costs nothing. Keep
the polling path as the fallback — it is already built at that point, and a
WebSocket through a corporate proxy is not a given.
