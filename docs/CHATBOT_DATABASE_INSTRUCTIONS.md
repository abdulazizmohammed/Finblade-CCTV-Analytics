# FinBlade CCTV Analytics
# PostgreSQL Chatbot Database Instructions

Generated 2026-08-17 by inspecting the implementation, not the requirements.
Every table, view, column and threshold below was read from the code that runs.
Where a capability is documented elsewhere but not implemented, this file says
so rather than describing it.

Authorities used, in order of precedence:

| Source | What it settles |
|---|---|
| `services/api/ddl_pg.sql` | the Postgres schema — generated from the SQLite schema by `scripts/gen_pg_ddl.py` |
| `services/api/analytics_views.py` | the six views and their columns |
| `services/api/postgres_store.py` | what the application actually writes |
| `finblade/rules.py`, `finblade/metrics.py`, `finblade/areas.py`, `finblade/presence.py`, `finblade/globalid.py` | the business rules |

---

## 1. Purpose

You are querying a CCTV crowd-analytics database. It records how many people are
in defined areas of a building, when they move between them, when they enter and
leave the building, and when rules fire.

The central difficulty is that **a camera detection is not a person**. The same
human can be:

* seen by two cameras at once, and counted twice;
* lost and re-acquired by the tracker, and counted twice;
* recorded in three event rows for one movement.

Most of this document exists to stop those three errors. A query that makes one
of them still runs and still returns a number.

---

## 2. System Overview

Cameras run independent worker processes. Each decodes video, detects people,
tracks them within its own frame, decides which drawn polygon (zone) each person
is standing in, and posts to an API. The API writes to Postgres and runs a rule
engine.

One process — the API — owns cross-camera identity. Workers send appearance
embeddings and receive back an opaque `global_ref` shared across cameras.

Three counting layers exist, answering three different questions:

| Layer | Question | Self-correcting | Sees unmonitored space |
|---|---|---|---|
| Zone occupancy | how many foot points are inside this polygon *now* | yes, every frame | no |
| Physical area occupancy | how many distinct people are in this *room* | yes | no |
| Facility roster | how many people are in the *building* | **no** | yes |

They are not interchangeable and must never be summed together.

---

## 3. Database Architecture

* **Engine:** PostgreSQL. Verified against 16.2 and 18.4.
* **Timestamps:** every `ts`-style column is `DOUBLE PRECISION` epoch **seconds, UTC**. There are no `timestamptz` columns in any table. Views expose converted timestamps alongside.
* **No `sites` table exists.** `site_id` is a plain, un-constrained text column on several tables. There is no foreign key and no lookup table. Treat it as a label.
* **No foreign key constraints exist anywhere.** All relationships below are logical, enforced by the application, not the database.
* **No materialized views.** All six analytics objects are plain views.
* **Views are applied out of band** by `scripts/pg_apply.py`. Nothing in the application creates them; rebuilding the schema does not restore them.

---

## 4. Approved Database Objects

### Preferred for chatbot use

| Object | Answers |
|---|---|
| `v_facility_current` | how many people are in the **building** |
| `v_facility_roster` | who is inside and for how long; likely missed exits |
| `v_facility_crossings` | entering and leaving the building |
| `v_facility_doors` | cumulative traffic per doorway |
| `v_area_current` | room occupancy, de-duplicated across cameras |
| `v_area_intervals` | room occupancy history |
| `v_zone_current` | live occupancy per polygon |
| `v_zone_intervals` | zone history, with the duration each reading stood |
| `v_zone_entries` | arrivals in a zone, safe to count |
| `v_zone_events` | all events with the zone resolved |
| `v_zone_config` | zone definitions and thresholds |
| `v_camera_status` | camera health with online/offline derived |
| `v_alerts` | alerts with lifecycle resolved |
| `v_timeline` | everything on one time axis |

Fourteen views. **Every question in §19 is answerable through them.** The base
tables below are documented for completeness, but a read-only role provisioned
by `scripts/pg_grants.py` cannot reach any of them — and does not need to.

### Use with caution — raw tables, correct but easy to misuse

These are the sources behind the views. A chatbot role cannot read them and
should not need to; they are listed so you can understand what a view is built
from.

| Object | Covered by |
|---|---|
| `events` | `v_zone_events`, `v_zone_entries`, `v_facility_crossings` |
| `facility_presence` | `v_facility_current`, `v_facility_roster` |
| `facility_meta` | `v_facility_current` |
| `facility_doors` | `v_facility_doors` |
| `physical_areas`, `area_state_ts` | `v_area_current`, `v_area_intervals` |
| `cameras` | `v_camera_status` — the **only** view touching it, and it selects no credential |
| `zones` | `v_zone_config` |
| `zone_live`, `zone_state_ts` | `v_zone_current`, `v_zone_intervals` |

### Avoid

| Object | Why |
|---|---|
| `cameras.source`, `cameras.stream_url` | **RTSP URLs with embedded passwords.** Never select, never display. No view references this table. |
| `events.payload` | full event JSON; large, and duplicates the columns |
| `zone_state_ts` directly | use `v_zone_intervals`, which adds the duration each reading was valid for. Averaging the raw table is wrong — see §13. |
| `zone_live` directly | use `v_zone_current` |
| `reports`, `forwarder_cursors` | internal |

If your role was provisioned by `scripts/pg_grants.py`, most of the *Avoid* list
is already unreachable and `SELECT *` on some views is refused because a column
is withheld. Name your columns.

---

## 5. Database Schema Reference

### 5.1 Views

#### `v_zone_entries` — view

One row per person **arriving** in a zone. Use this for counting people.

| Column | Type | Meaning |
|---|---|---|
| `event_id` | text | unique |
| `camera_id` | text | which camera observed it |
| `site_id` | text | label |
| `zone_id` | text | the zone arrived in (from `events.zone_to`) |
| `zone_name` | text | human name, or the id if the zone is unconfigured |
| `zone_type` | text | see §7 |
| `person_key` | text | **count DISTINCT this to count people** |
| `global_ref` | text | cross-camera identity, NULL when unresolved |
| `identity_resolved` | boolean | whether `global_ref` is present |
| `event_ts` | double | epoch seconds UTC |
| `event_utc` | timestamptz | the same instant, converted |

`person_key` is `global_ref` when ReID resolved the track, otherwise
`camera_id || ':' || person_ref`. The fallback is camera-scoped so two
unresolved people are never merged by both being track 17 on different cameras.
Unresolved entries therefore **over**-count rather than under-count.

**Recommended for:** how many people entered a zone, footfall by zone, busiest
hours, one person's route.

**Do not use for:** building entries and exits (§12), or current occupancy (§11).

#### `v_zone_events` — view

Every event with the zone resolved from whichever column carried it.

| Column | Type | Meaning |
|---|---|---|
| `event_id`, `event_type`, `camera_id`, `site_id` | text | |
| `zone_id`, `zone_from`, `zone_to` | text | raw; which is populated depends on `event_type` |
| `zone_ref` | text | `COALESCE(zone_id, zone_to, zone_from)` — the zone the row is *about* |
| `zone_name`, `zone_type`, `restricted` | | resolved via `zone_ref` and `camera_id` |
| `person_ref` | text | tracker hash — **never count this** |
| `person_key` | text | the safe thing to count |
| `global_ref` | text | cross-camera identity, may be NULL |
| `event_ts`, `event_utc` | | epoch / converted |

**Recommended for:** loitering, restricted-zone entries, transitions, any
non-arrival event type.

**Do not use for:** counting arrivals — use `v_zone_entries`, which already
excludes the duplicate rows a transition emits (§12).

#### `v_zone_current` — view

The live reading, one row per `(camera_id, zone_id)`. Sourced from `zone_live`.

| Column | Type | Meaning |
|---|---|---|
| `camera_id`, `zone_id`, `site_id`, `zone_name`, `zone_type` | text | |
| `occupancy` | bigint | people in **this camera's polygon** |
| `density` | double | people per m² |
| `capacity_pct` | double | percentage of `capacity_max` |
| `status` | text | `NORMAL` / `AMBER` / `RED` |
| `trend` | text | `rising` / `falling` / `flat` |
| `peak_occupancy` | bigint | peak within the aggregation window |
| `inflow`, `outflow` | double | people per minute |
| `capacity_max`, `area_sqm`, `restricted` | | from `zones` |
| `reading_ts`, `reading_utc` | | when the reading was taken |

**Do not use for:** a site total. `SUM(occupancy)` double-counts anyone two
cameras can see (§10).

#### `v_zone_intervals` — view

History with the duration each reading stood for. Sourced from `zone_state_ts`.

| Column | Type | Meaning |
|---|---|---|
| `camera_id`, `zone_id`, `site_id`, `zone_name`, `zone_type` | | |
| `valid_from`, `valid_to` | double | epoch bounds of this reading |
| `valid_from_utc`, `valid_to_utc` | timestamptz | converted |
| `duration_seconds` | double | how long it stood. **NULL on the newest row per zone** |
| `occupancy`, `density`, `capacity_pct`, `status`, `trend`, `inflow`, `outflow` | | the reading |
| `capacity_max`, `area_sqm`, `restricted` | | from `zones` |
| `is_stale` | boolean | stood longer than 600s — almost certainly a dead worker |
| `is_open` | boolean | newest row per zone; current, not stale |

Writes are event-driven, so a row means *"and it stayed that way until the next
row"*. Rows cover unequal spans. **Every average must be weighted by
`duration_seconds`** (§11.6).

`is_stale` marks unobserved time, not a quiet period. A killed worker emits no
offline event; its absence shows up only as an implausibly long interval.

#### `v_alerts` — view

| Column | Type | Meaning |
|---|---|---|
| `alert_id` | bigint | |
| `rule_id` | text | `R-01` … `R-09`, see §14 |
| `severity` | text | `INFO` / `AMBER` / `RED` / `CRITICAL` |
| `message` | text | human text |
| `camera_id`, `zone_id`, `site_id`, `zone_name` | | |
| `status` | text | `COALESCE(status,'OPEN')` |
| `is_active` | boolean | true for `OPEN` and `ACK` |
| `acknowledged_by`, `acknowledged_at` | | |
| `resolved_by`, `resolved_at`, `note` | | |
| `raised_ts`, `raised_utc` | | when it fired |

The raw `alerts.status` is **NULL** until somebody touches it. Filter
`is_active`, never `status = 'OPEN'`.

#### `v_timeline` — view

`UNION ALL` of `zone_state_ts`, `events` and `alerts` on one time axis, with a
`record_type` discriminator (`zone_state` / `event` / `alert`). Columns that do
not apply to a record type are NULL.

It is a union, not a join, deliberately: joining the fact tables on
`(camera_id, zone_id)` was measured at 5.66 trillion rows from a 1.15 GB source.

`zone_id` here is the **raw** column, so movement events contribute NULL. Use
`v_zone_events` when the zone matters.

### 5.2 Tables

#### `events` — table

Every discrete occurrence. Primary key `event_id` (text).

| Column | Type | Meaning |
|---|---|---|
| `event_type` | text | one of the 17 types in §12.1 |
| `camera_id`, `site_id` | text | |
| `zone_id` | text | populated only for in-place events |
| `zone_from`, `zone_to` | text | populated for movement events |
| `person_ref` | text | tracker hash, per camera, per session |
| `global_ref` | text | cross-camera identity. **NULL on all history written before 2026-08-17** |
| `ts` | double | epoch seconds UTC |
| `frame` | text | optional snapshot path |
| `payload` | text | the full event as JSON, including `derived` |

Relationships (logical, no FKs): `camera_id → cameras.camera_id`;
`zone_id`/`zone_from`/`zone_to` → `zones.zone_id` **together with `camera_id`**,
because `zones` is keyed on `(camera_id, zone_id)`.

#### `cameras` — table

One row per camera. Primary key `camera_id`.

| Column | Type | Meaning |
|---|---|---|
| `site_id`, `name` | text | |
| `state` | text | worker-reported state |
| `last_seen`, `health_ts` | double | epoch of last contact |
| `enabled` | bigint | 0/1 |
| `input_fps`, `resolution`, `dropped_frames`, `reconnects`, `loops`, `frozen` | | stream health |
| `people_in_view` | bigint | people the detector can see **anywhere in frame** |
| `people_in_zones` | bigint | of those, how many are inside a drawn polygon |
| `tracking_quality` | text | `OK` / `STRAINED` / … reported by the worker |
| `counts_reliable` | bigint | **tri-state: NULL means not reported, not "reliable"** |
| `counting_mode` | text | e.g. `track_degraded` |
| `mean_confidence`, `track_churn_per_min`, `detector_saturation` | double | detection-quality metrics |
| `source`, `stream_url` | text | **RTSP URLs containing passwords — never select** |

**There is no `effective_state` column.** Online/offline is derived in the API
(`_effective_state`), not stored. In SQL, derive it yourself: a camera is
OFFLINE when `enabled` is false, or when `COALESCE(health_ts, last_seen)` is
NULL or older than **30 seconds**.

#### `zones` — table

Polygon configuration. Primary key `(camera_id, zone_id)`.

| Column | Type | Meaning |
|---|---|---|
| `zone_name`, `zone_type` | text | see §7 |
| `restricted` | bigint | 0/1 |
| `capacity_max` | bigint | people |
| `area_sqm` | double | floor area, the denominator for density |
| `warning_density`, `critical_density` | double | per-zone thresholds |
| `loitering_threshold_sec` | double | |
| `enabled` | bigint | 0/1 |
| `physical_area_id` | text | → `physical_areas.area_id`, NULL for single-camera zones |
| `polygon`, `normalized_polygon`, `adjacency_list`, `colour` | text | JSON |

**`zone_id` is unique only within a camera.** Every camera numbers its zones
independently. Always filter or join on `camera_id` as well.

#### `physical_areas` — table

A real room, as opposed to one camera's polygon of it. Primary key `area_id`.

| Column | Type | Meaning |
|---|---|---|
| `name`, `area_type`, `site_id` | text | |
| `capacity_max` | bigint | the room holds this many regardless of camera count |
| `area_sqm` | double | |
| `updated_at` | double | |

May be empty. Mapping is explicit — a zone joins an area only when an operator
sets `zones.physical_area_id`. Never inferred from names.

#### `zone_live` — table

Current reading per `(camera_id, zone_id)`. Prefer `v_zone_current`. Two columns
the view does not expose:

| Column | Type | Meaning |
|---|---|---|
| `occupants` | text | JSON array of identity refs in this zone, or NULL |
| `physical_area_id` | text | the room this polygon looks at |

`occupants` is the raw material for cross-camera de-duplication (§10). NULL means
*this worker does not report identities*, which is different from an empty array
meaning *nobody is here*.

#### `zone_state_ts` — table

Occupancy history, append-only. Prefer `v_zone_intervals`. `extra` holds a JSON
object with `net_flow`, `inflow_5m`, `outflow_5m`, `inflow_15m`, `outflow_15m`,
`capacity_max`, `area_sqm` when present.

#### `area_state_ts` — table

Area occupancy history.

| Column | Type | Meaning |
|---|---|---|
| `area_id` | text | → `physical_areas.area_id` |
| `ts` | double | |
| `occupancy` | bigint | distinct people in the room |
| `capacity_pct`, `density` | double | |
| `summed_observations` | bigint | what the naive per-camera sum would have been |
| `camera_count` | bigint | cameras contributing |
| `site_id` | text | |

`summed_observations - occupancy` is the double-count that de-duplication
removed. Only written for zones mapped to an area.

#### `facility_presence` — table

The building roster. One row per person currently inside. Primary key `ref`.

| Column | Type | Meaning |
|---|---|---|
| `ref` | text | identity, `global_ref` when resolved |
| `admitted_at`, `last_seen` | double | epoch |
| `entry_zone`, `last_zone` | text | |
| `sightings` | bigint | |

`COUNT(*)` is the observed building occupancy.

#### `facility_meta` — table

Key/value counters, `key` → `value` (double). Keys observed in the
implementation: `admitted`, `discharged`, `readmit_ignored`, `discharge_unknown`,
`ambiguous_crossings`, `turned_back`, `rekeyed`, `baseline_discharged`,
`cleared`, `provisional_admits`, and `baseline` — the declared opening headcount.

#### `facility_doors` — table

Cumulative traffic per doorway: `door_zone_id`, `entries`, `exits`.

**Not occupancy.** Other doors exist and the roster is the authority.

#### `alerts` — table

Prefer `v_alerts`. Raw `status` is NULL until acted on. `kind` is `FIRE` or
`CLEAR` — a rule clearing writes its own row.

---

## 6. Business and Semantic Definitions

| Term | Definition as implemented |
|---|---|
| **Site** | A text label (`site_id`). No table, no constraint, no validation. |
| **Camera** | One row in `cameras`, one worker process, one video stream. |
| **Zone** | A polygon drawn on **one camera's** frame. Keyed `(camera_id, zone_id)`. |
| **Physical Area** | A real room. Several cameras' zones may point at one via `zones.physical_area_id`. |
| **Restricted Zone** | `zones.restricted = 1`. Wins zone assignment when polygons overlap. Triggers R-06 immediately. |
| **Person Detection** | A YOLO bounding box, class 0. Not stored individually. |
| **Track / Track ID** | A tracker-assigned integer, valid within one camera and one worker session. Breaks and renumbers frequently. |
| **`person_ref`** | Salted hash of the track id. Per camera, per session. **Changes on every track break.** Counting distinct values measures churn, not people. |
| **`global_ref`** | Cross-camera identity (`gp_…`), from appearance matching plus a topology gate. Survives track breaks and movement between cameras. NULL when unresolved. |
| **`person_key`** | View-only. `global_ref`, else `camera_id:person_ref`. The safe thing to `COUNT(DISTINCT)`. |
| **Canonical person identity** | Synonym for `global_ref`. There is no separate canonical-id table. |
| **ReID** | OSNet appearance embeddings, matched in the API. Embeddings live in RAM only and are **never** stored — no table contains one. |
| **Cross-camera identity** | The mechanism producing `global_ref`. |
| **Occupancy** | Depends on scope — see §11. Unqualified, it usually means zone occupancy. |
| **Zone occupancy** | Foot points inside one polygon on one frame. Self-correcting, blind to unmonitored floor. |
| **Area occupancy** | Distinct people across every zone mapped to a room. |
| **Site / facility occupancy** | People admitted through a door and not yet observed leaving. |
| **`people_in_view`** | Everyone the detector sees anywhere in a camera's frame, whether or not inside a zone. |
| **`people_in_zones`** | The subset inside a drawn polygon. |
| **Entry** | Ambiguous. `ZONE_ENTRY` = arrived in a polygon. `FACILITY_ENTRY` = crossed into the building. |
| **Exit** | Same ambiguity. `ZONE_EXIT` ≠ leaving the premises. |
| **Zone Transition** | A confirmed move between two polygons on one camera. |
| **Movement** | Any of the above. Cross-camera movement is *not* a transition — transitions are computed per worker. |
| **Density** | `occupancy / area_sqm`. |
| **Warning / Critical density** | Per-zone `warning_density` / `critical_density`, defaulting to 2.0 and 4.0 per m². |
| **Camera Offline** | No health report for over 30s, or `enabled = 0`. Derived, not stored. |
| **Unique Visitor** | Distinct `global_ref` values. Not a stored figure; compute it. |
| **Current State** | `zone_live` / `v_zone_current`, `facility_presence`, `cameras`. |
| **Historical State** | `zone_state_ts` / `v_zone_intervals`, `area_state_ts`, `events`, `alerts`. |
| **Event** | A row in `events`. |
| **Alert** | A rule firing or clearing. Row in `alerts`. |
| **Derived event** | An event carrying `"derived": true` in its payload — see §12.2. |

---

## 7. Site / Area / Zone Model

```
site_id (a label, no table)
   └── camera  (cameras.camera_id)
         └── zone  (zones, keyed camera_id + zone_id)
               └── physical_area_id ──► physical_areas.area_id   (optional)
```

Zone types, from the editor's list: `MONITORED`, `RESTRICTED`, `ENTRANCE`,
`EXIT`, `DOOR`, `TRANSITION`, `UNMONITORED`. `OUTSIDE` is additionally
recognised by the presence model to mark ground beyond the building boundary.

Two rules that catch people out:

1. **`zone_id` is not globally unique.** Join and filter on `camera_id` too.
2. **Areas are matched by explicit `area_id` only.** "Office", "Office Room" and
   "Office CAM04" are three different areas unless an operator mapped them.

---

## 8. Camera Model

`cameras` is one row per camera. Health is pushed by the worker; the API derives
display state, and **`v_camera_status` implements the same rule in SQL** so every
consumer agrees. Use the view — it also omits the credential columns. To find
offline cameras:

```sql
SELECT camera_id, name, effective_state, last_seen_utc,
       ROUND(seconds_since_seen::numeric, 0) AS seconds_silent
FROM v_camera_status
WHERE NOT is_online
ORDER BY seconds_silent DESC NULLS FIRST;
```

**Trust signals.** Before quoting any per-person figure from a camera, check
`tracking_quality`, `counts_reliable` and `track_churn_per_min`.
`counts_reliable` is **tri-state**: NULL means the worker reported nothing, which
is not the same as reliable. `STRAINED` with high churn means the tracker is
fragmenting and per-person counts from that camera are inflated.

---

## 9. Person Tracking and Identity Model

```
detection (not stored)
   └── track            per camera, per session, renumbers on every break
         └── person_ref  salted hash of the track id  ── unstable
               └── global_ref   cross-camera identity ── stable, may be NULL
```

Resolution needs at least two good appearance samples and passes a physics gate
(camera topology) before appearance is scored. Two consequences:

* **`global_ref` is NULL for the first seconds of every track**, and for tracks
  whose crops never pass the quality gate. On a camera where the gate rejects
  most crops it can be NULL for everything.
* **`global_ref` is NULL on all history written before 2026-08-17**, when the
  column was added to the Postgres backend. Identity queries over older data
  return nothing — the history is real, its identity dimension is absent.

Always report `identity_resolved` coverage alongside a people count.

**No embedding is stored anywhere.** There is no vector column and no table of
appearance data.

---

## 10. Multi-Camera Deduplication Rules

Two cameras can watch one room. The implemented mechanisms, all of which exist:

### 10.1 `global_ref` — the foundation

One person seen by two cameras resolves to the same `global_ref`. This is what
makes everything below possible. When it is NULL, nothing can de-duplicate.

### 10.2 `zone_live.occupants` — the raw material

Each zone reports a JSON array of identity refs. Refs are `global_ref` when
resolved, else `local:<camera_id>:<track_id>` — camera-scoped by construction, so
a `local:` ref **can never match another camera's**.

### 10.3 Physical areas — room-level de-duplication

Area occupancy is `COUNT(DISTINCT person)` across every zone mapped to that
area, never the sum. `SUM` double-counts the overlap; `MAX`, `MIN` and `AVG` are
all wrong too — `{P1,P2}` and `{P2,P3}` is three people and `MAX` says two. Only
the union is right. History is in `area_state_ts`, where
`summed_observations - occupancy` is the duplication removed.

### 10.4 The degradation rule

Zones reporting `occupants` are de-duplicated against each other. Zones reporting
nothing fall back to their own count and are **added on**, because a count
without identities cannot be merged. So the result degrades to a plain sum
exactly when no zone reports identities.

### What the chatbot must do

| Scope | Correct source |
|---|---|
| One camera's polygon | `v_zone_current.occupancy` |
| A room watched by several cameras | `area_state_ts`, or `COUNT(DISTINCT)` over `zone_live.occupants` |
| The whole building | `facility_presence` |
| Distinct people entering somewhere | `COUNT(DISTINCT person_key)` |

**Never `SUM(occupancy)` across cameras, and never `SUM(people_in_view)`.**

### Known limitation

De-duplication is only as good as ReID coverage. Where `identity_resolved` is
low, refs are `local:`-scoped and the same person on two cameras counts twice.
This is visible, not hidden — check the coverage before quoting.

---

## 11. Occupancy Calculation Rules

### 11.1 How many people are inside the site right now

```sql
SELECT people_inside, observed_inside, baseline
FROM v_facility_current;
```

The roster counts people admitted through a door and not yet seen to leave, so
it includes anyone in a corridor no camera watches. `baseline` is a declared
opening headcount for people already inside before counting started; it is 0
unless an operator set it.

**Caveat:** discharge is strict — the roster only comes down when a crossing out
is observed. If exits are not being detected it drifts upward and never
self-corrects. Sanity-check with §11.7.

### 11.2 How many people are in Area A

```sql
SELECT occupancy, camera_count, summed_observations, reading_utc
FROM v_area_current
WHERE area_id = 'AREA-ID';
```

If `area_state_ts` is empty, no zones are mapped to areas on this deployment;
fall back to 11.3 and state that the figure is per camera.

### 11.3 How many people are in Zone A

```sql
SELECT camera_id, zone_name, occupancy, reading_utc
FROM v_zone_current
WHERE zone_name = 'ZONE NAME';
```

One row per camera. If several rows come back, they are different views of
possibly the same people — do not add them.

### 11.4 How many people are visible on CAM-04

```sql
SELECT camera_id, people_in_view, people_in_zones,
       tracking_quality, counts_reliable
FROM v_camera_status WHERE camera_id = 'CAM-04';
```

`people_in_view` is everyone in frame; `people_in_zones` is the subset inside a
polygon. Neither is a site total.

### 11.5 What was occupancy at 2 PM yesterday

```sql
SELECT zone_name, camera_id, occupancy, valid_from_utc, valid_to_utc
FROM v_zone_intervals
WHERE valid_from <= EXTRACT(EPOCH FROM TIMESTAMPTZ '2026-08-16 14:00:00+00')
  AND (valid_to  >  EXTRACT(EPOCH FROM TIMESTAMPTZ '2026-08-16 14:00:00+00')
       OR valid_to IS NULL);
```

Exactly one interval per zone contains any instant.

### 11.6 Average occupancy — must be time-weighted

```sql
SELECT zone_name,
       ROUND((SUM(occupancy * duration_seconds)
              / NULLIF(SUM(duration_seconds), 0))::numeric, 2) AS avg_occupancy
FROM v_zone_intervals
WHERE valid_from_utc >= date_trunc('day', now()) - interval '1 day'
  AND valid_from_utc <  date_trunc('day', now())
  AND NOT is_stale AND NOT is_open
GROUP BY zone_name;
```

`AVG(occupancy)` over rows is **wrong** — rows cover unequal time and the two
answers have differed by 8× on this data.

### 11.7 Peak occupancy

```sql
SELECT zone_name, MAX(occupancy) AS peak
FROM v_zone_intervals
WHERE valid_from_utc >= date_trunc('day', now()) - interval '1 day'
  AND valid_from_utc <  date_trunc('day', now())
  AND NOT is_stale
GROUP BY zone_name ORDER BY peak DESC;
```

### Authoritative source per question

| Question | Object |
|---|---|
| Current site occupancy | `facility_presence` + `facility_meta.baseline` |
| Current area occupancy | `area_state_ts` (latest row) |
| Current zone occupancy | `v_zone_current` |
| Camera detections | `cameras.people_in_view` |
| Historical occupancy | `v_zone_intervals` |
| Peak occupancy | `MAX(occupancy)` over `v_zone_intervals` |

---

## 12. Entry / Exit / Movement Rules

### 12.1 Event types

`ZONE_ENTRY`, `ZONE_EXIT`, `ZONE_TRANSITION`, `DENSITY_UPDATE`,
`CAPACITY_WARNING`, `RESTRICTED_ZONE_ENTRY`, `RESTRICTED_ZONE_EXIT`,
`LOITERING_START`, `LOITERING_END`, `CAMERA_HEARTBEAT`, `CAMERA_ONLINE`,
`CAMERA_OFFLINE`, `CAMERA_RECOVERED`, `WRONG_DIRECTION`, `GROUP_CROSSING`,
`FACILITY_ENTRY`, `FACILITY_EXIT`.

### 12.2 The derived-event rule — the most common counting error

A confirmed move between zones emits **three rows**:

1. `ZONE_TRANSITION` — the authoritative record
2. `ZONE_EXIT` with `derived: true` in the payload
3. `ZONE_ENTRY` with `derived: true` in the payload

**Counting `ZONE_ENTRY` and `ZONE_TRANSITION` together doubles every movement.**
Counting `ZONE_ENTRY` alone is correct, because every transition already emits
one. `v_zone_entries` encodes this; use it and the problem disappears.

A person's first appearance emits a plain, non-derived `ZONE_ENTRY`.

### 12.3 Movement *inside* one zone is not an event

Zone assignment is debounced: a track must be seen in a new zone for **3
consecutive frames** before the change commits. Walking around inside a polygon
produces no entry or exit. Only a committed boundary crossing does.

### 12.4 Cross-camera movement is not a transition

Transitions are computed per worker, from that camera's own zones. A person
walking from CAM-03's zone to CAM-04's zone produces an exit on one and an entry
on the other — **not** a `ZONE_TRANSITION`. Reconstruct cross-camera routes by
ordering `v_zone_entries` on `global_ref`.

### 12.5 Facility crossings

`FACILITY_ENTRY` / `FACILITY_EXIT` are emitted by the roster when a boundary
crossing is observed. They are in `events` only — **no view exposes them**.

* A one-way `ENTRANCE` or `EXIT` zone resolves on arrival.
* A two-way `DOOR` resolves on departure, from the zones either side. Inside →
  door → beyond is an exit; beyond → door → inside is an entry. If neither side
  was observed the crossing is counted as ambiguous and **no** event is emitted.

Payload keys: `door_zone_id`, `person_ref`, `occupancy` (the figure *after* the
crossing). These events carry **no `global_ref`** — a known gap.

`COUNT(*)` of `FACILITY_ENTRY` counts admission *events*, including re-triggers
by someone already inside. `facility_meta.admitted` counts admissions that
actually grew the roster. They differ.

### Which object for which question

| Question | Object |
|---|---|
| How many people entered Zone A today | `v_zone_entries`, `COUNT(DISTINCT person_key)` |
| How many people entered the building today | `events`, `event_type = 'FACILITY_ENTRY'` |
| How many exited the building | `events`, `event_type = 'FACILITY_EXIT'` |
| How many moved Zone A → Zone B | `v_zone_events`, `event_type = 'ZONE_TRANSITION'` |
| How many crossed a doorway | `facility_doors`, or `FACILITY_*` filtered on `door_zone_id` |
| How many visitors came today | `COUNT(DISTINCT global_ref)` over `v_zone_entries` |

---

## 13. Density Calculation Rules

**Verified implementation:** `density = occupancy / area_sqm`
(`finblade/metrics.py: density_per_sqm`). Zero or missing `area_sqm` yields 0.0
rather than an error.

Status bands (`density_status`): `RED` when density **>** critical, `AMBER` when
**>** warning, else `NORMAL`. Strictly greater than — exactly 2.0 is NORMAL.

**Thresholds are per zone**, in `zones.warning_density` and
`zones.critical_density`, falling back to engine defaults when unset.

Verified default values in `finblade/rules.py: RuleThresholds`:

| Setting | Value |
|---|---|
| `amber_on` | 2.0 /m² |
| `amber_off` | 1.8 /m² |
| `red_on` | 4.0 /m² |
| `red_off` | 3.6 /m² |
| `capacity_on_pct` | 90 % |
| `capacity_off_pct` | 85 % |
| `loiter_seconds` | 30 |
| `offline_seconds` | 30 |
| `debounce_seconds` | 10 |

So the historical requirement of warning >2 /m² and critical >4 /m² **is
implemented as specified**. The `_off` values are hysteresis: an alert clears at
a lower level than it fires, so a value oscillating around the threshold
produces one alert, not many.

Stored density lives in `zone_live.density`, `zone_state_ts.density`,
`area_state_ts.density`, and the corresponding view columns. Status is the
`status` text column.

---

## 14. Alert Model

Rule ids verified in `finblade/rules.py`:

| Rule | Meaning | Severity when firing |
|---|---|---|
| `R-01` | Density warning, > `warning_density` | `AMBER` |
| `R-02` | Density critical, > `critical_density` | `RED` |
| `R-03` | Capacity pressure, ≥ 90 % of `capacity_max` | `AMBER` |
| `R-05` | Loitering beyond the zone's threshold | `AMBER` |
| `R-06` | Restricted-zone intrusion — **immediate, no debounce**, one per visit | `CRITICAL` |
| `R-07` | Camera offline > 30 s silence; also fires on recovery | `RED` / `INFO` on recovery |
| `R-08` | Occupancy report, scheduled and on demand | — |
| `R-09` | Head-count threshold for a zone | `AMBER` |

`R-04` (bottleneck detection) is **not implemented** — explicitly out of scope.

Severities: `INFO`, `AMBER`, `RED`, `CRITICAL`.

`kind` is `FIRE` or `CLEAR`. A rule clearing writes its own row with severity
`INFO`, so counting alert rows counts firings *and* clearings unless you filter
`kind = 'FIRE'`.

Every rule carries a **10-second debounce** except R-06. All threshold rules use
hysteresis.

Answering the standard questions:

```sql
-- active alerts
SELECT raised_utc, severity, rule_id, zone_name, camera_id, message
FROM v_alerts WHERE is_active ORDER BY raised_ts DESC;

-- today's restricted-zone alerts
SELECT raised_utc, camera_id, zone_name, message
FROM v_alerts
WHERE rule_id = 'R-06' AND raised_utc >= date_trunc('day', now())
ORDER BY raised_ts DESC;

-- unacknowledged
SELECT raised_utc, severity, rule_id, zone_name, message
FROM v_alerts
WHERE acknowledged_at IS NULL AND is_active
ORDER BY raised_ts DESC;
```

Longest-offline camera: use the §8 query, not the alerts table — an alert
records that it went offline, the `cameras` row records how long it has been.

---

## 15. Time and Timestamp Rules

* **All stored timestamps are epoch seconds, UTC, `DOUBLE PRECISION`.** There is
  no `timestamptz` column in any table.
* There is **no `created_at` / `updated_at` convention.** Each table names its
  own: `events.ts`, `alerts.ts` (plus `acknowledged_at`, `resolved_at`),
  `zone_state_ts.ts`, `zone_live.ts`, `area_state_ts.ts`,
  `cameras.last_seen` / `health_ts`, `physical_areas.updated_at`,
  `reports.generated_at` / `from_ts` / `to_ts`,
  `facility_presence.admitted_at` / `last_seen`.
* Views expose converted columns: `event_utc`, `reading_utc`, `valid_from_utc`,
  `valid_to_utc`, `raised_utc`, `ts_utc`. **Prefer these** — they are
  `timestamptz` and compare directly with `now()`.

Windows:

```sql
-- today            WHERE event_utc >= date_trunc('day', now())
-- yesterday        WHERE event_utc >= date_trunc('day', now()) - interval '1 day'
--                    AND event_utc <  date_trunc('day', now())
-- last 24 hours    WHERE event_utc >= now() - interval '24 hours'
-- this week        WHERE event_utc >= date_trunc('week', now())
-- between          WHERE event_utc >= TIMESTAMPTZ '...' AND event_utc < TIMESTAMPTZ '...'
```

`date_trunc('day', now())` follows the **session** timezone. Servers observed
run UTC, so "today" is a UTC day by default. For a local business day, issue
`SET TIME ZONE 'Asia/Dubai';` once and every query follows. Do not mix styles
within one query.

On a raw table with only epoch columns, wrap: `to_timestamp(ts) >= date_trunc('day', now())`.

---

## 16. Preferred Query Sources

| Priority | Objects |
|---|---|
| **Preferred** | `v_zone_entries`, `v_zone_current`, `v_zone_intervals`, `v_alerts`, `v_zone_events`, `v_timeline` |
| **Use with caution** | `events` (facility crossings only), `facility_presence`, `facility_meta`, `facility_doors`, `area_state_ts`, `physical_areas`, `zones`, `cameras` |
| **Avoid** | `cameras.source` / `stream_url`, `events.payload`, `zone_state_ts`, `zone_live`, `reports`, `forwarder_cursors` |

---

## 17. SQL Safety Rules

You have **read-only** access. Generate only:

```
SELECT     WITH     EXPLAIN
```

**Never** generate or execute:

```
INSERT  UPDATE  DELETE  DROP  ALTER  TRUNCATE  CREATE
GRANT   REVOKE  COPY … TO PROGRAM   DO   CALL
SET     VACUUM  REINDEX  REFRESH MATERIALIZED VIEW
```

or anything that changes data, schema, permissions or configuration.

Additional rules:

1. **Never select `cameras.source` or `cameras.stream_url`.** They contain RTSP
   URLs with embedded passwords. If asked for camera connection details,
   refuse and explain.
2. Never attempt privilege escalation, never read `pg_authid`, `pg_shadow` or
   role passwords, never reveal connection strings.
3. Never `SELECT *` on `events`, `zone_state_ts` or `v_timeline` — millions of
   rows. Name columns and add `LIMIT`.
4. `SELECT *` may be **refused outright** on `v_zone_events`, `v_zone_entries`
   and `v_timeline` if column-level grants are in force. Name your columns.
5. Use `LIMIT` (50–500) on any exploratory or row-listing query.
6. Prefer aggregation when the question asks for a total or a statistic.
7. Never invent a result. If a query returns nothing, say so.
8. If a query errors, read the error and correct the SQL. A permission error on
   `person_ref` means you should be using `person_key`.

---

## 18. Query Reasoning Workflow

1. Understand the business intent.
2. Determine scope: site, area, zone, camera, or whole building.
3. Classify the question: current state / historical / event count / **person**
   count / detection count / occupancy / density / alert.
4. Pick the object from §16 and §19.
5. Ask whether cross-camera de-duplication is needed. If the answer involves
   people rather than detections, it is.
6. Pick the timestamp column — prefer a `_utc` view column.
7. Write a `SELECT` with explicit columns.
8. Add scope filters, including `camera_id` whenever `zone_id` appears.
9. Add `LIMIT` for row listings.
10. Execute.
11. Inspect the result. Is the magnitude plausible? Are there NULLs?
12. Check `identity_resolved` coverage before quoting a people count.
13. If inconsistent, re-check assumptions before answering.
14. Answer in natural language, stating the scope and any caveat.

---

## 19. Question → Database Mapping

| # | User question | Object | Key fields |
|---|---|---|---|
| 1 | How many people are in the site now? | `facility_presence` + `facility_meta` | `COUNT(*)`, `baseline` |
| 2 | How many people are in Area A? | `area_state_ts` | `occupancy`, `ts` |
| 3 | How many people are in Zone A? | `v_zone_current` | `occupancy`, `zone_name` |
| 4 | Show occupancy by zone | `v_zone_current` | `zone_name`, `occupancy` |
| 5 | How many people are on CAM-04? | `cameras` | `people_in_view`, `people_in_zones` |
| 6 | Which zone is most crowded? | `v_zone_current` | `density`, `occupancy` |
| 7 | Show density by zone | `v_zone_current` | `density`, `capacity_pct`, `status` |
| 8 | Which zones are over capacity? | `v_zone_current` | `capacity_pct` |
| 9 | Which cameras are online? | `cameras` | `health_ts`, `enabled` |
| 10 | Which cameras are offline? | `cameras` | `health_ts`, `last_seen` |
| 11 | Which camera has been offline longest? | `cameras` | `last_seen` |
| 12 | Are the counts trustworthy? | `cameras` | `tracking_quality`, `counts_reliable` |
| 13 | Are there any active alerts? | `v_alerts` | `is_active` |
| 14 | Show today's restricted-zone alerts | `v_alerts` | `rule_id = 'R-06'` |
| 15 | Which alerts are unacknowledged? | `v_alerts` | `acknowledged_at IS NULL` |
| 16 | Show critical alerts this week | `v_alerts` | `severity`, `raised_utc` |
| 17 | How many people entered Zone A today? | `v_zone_entries` | `person_key`, `zone_name` |
| 18 | How many people entered the building today? | `events` | `FACILITY_ENTRY` |
| 19 | How many left the building today? | `events` | `FACILITY_EXIT` |
| 20 | How many unique visitors today? | `v_zone_entries` | `global_ref` |
| 21 | How many moved Zone A → Zone B? | `v_zone_events` | `ZONE_TRANSITION` |
| 22 | Which doorway is busiest? | `facility_doors` | `entries`, `exits` |
| 23 | What was occupancy at 2 PM yesterday? | `v_zone_intervals` | `valid_from`, `valid_to` |
| 24 | What was peak occupancy yesterday? | `v_zone_intervals` | `MAX(occupancy)` |
| 25 | What is average occupancy today? | `v_zone_intervals` | time-weighted |
| 26 | How long was Zone A over capacity? | `v_zone_intervals` | `duration_seconds` |
| 27 | Show hourly occupancy for Zone A | `v_zone_intervals` | `valid_from_utc` |
| 28 | Busiest hour of the day | `v_zone_entries` | `date_trunc('hour', …)` |
| 29 | Show loitering incidents today | `v_zone_events` | `LOITERING_START` |
| 30 | Who is in the building right now? | `facility_presence` | `ref`, `admitted_at` |
| 31 | Has anyone been inside unusually long? | `facility_presence` | `admitted_at` |
| 32 | Show everything that happened 09:00–10:00 | `v_timeline` | `record_type`, `ts_utc` |
| 33 | Which zones have no camera reporting? | `v_zone_intervals` | `is_stale` |
| 34 | How much of today's data has identity? | `v_zone_entries` | `identity_resolved` |
| 35 | Show wrong-way movements | `v_zone_events` | `WRONG_DIRECTION` |
| 36 | Show group crossings (tailgating) | `v_zone_events` | `GROUP_CROSSING` |

---

## 20. Verified SQL Examples

Every query below uses only columns confirmed present in `ddl_pg.sql` or
`analytics_views.py`. All are read-only.

### 20.1 Current site occupancy

**Question.** How many people are in the building right now?
**Interpretation.** The building, including people in unmonitored space — not the sum of zone counts.

```sql
SELECT people_inside, observed_inside, baseline
FROM v_facility_current;
```

**Why.** The roster is the only object that keeps counting someone no camera can see.

---

### 20.2 Occupancy by zone

**Question.** Show current occupancy for every zone.
**Interpretation.** Per polygon, per camera — not a total.

```sql
SELECT camera_id, zone_name, occupancy, density, capacity_pct, status, reading_utc
FROM v_zone_current
ORDER BY occupancy DESC;
```

**Why.** `v_zone_current` is the live reading with zone config already joined.

---

### 20.3 Area occupancy, de-duplicated

**Question.** How many people are in the main office?
**Interpretation.** Distinct people in the room, however many cameras watch it.

```sql
SELECT area_name, occupancy, camera_count,
       summed_observations, duplicates_removed, reading_utc
FROM v_area_current
WHERE area_name = 'Main Office';
```

**Why.** `occupancy` is the union of identities; `summed_observations` is what a naive sum would have said.

---

### 20.4 Camera detections

**Question.** How many people can CAM-04 see?

```sql
SELECT camera_id, people_in_view, people_in_zones,
       tracking_quality, counts_reliable, track_churn_per_min
FROM v_camera_status WHERE camera_id = 'CAM-04';
```

**Why.** Detections, with the trust signals attached so the answer can be qualified.

---

### 20.5 Cameras offline

**Question.** Which cameras are offline?

```sql
SELECT camera_id, name, effective_state, last_seen_utc,
       ROUND(seconds_since_seen::numeric, 0) AS seconds_silent
FROM v_camera_status
WHERE NOT is_online
ORDER BY seconds_silent DESC NULLS FIRST;
```

**Why.** There is no stored `effective_state`; 30 s is the threshold in `rules.py` and `app.py`.

---

### 20.6 Cameras online

```sql
SELECT camera_id, name, effective_state, input_fps, resolution,
       last_seen_utc
FROM v_camera_status
WHERE is_online
ORDER BY camera_id;
```

---

### 20.7 Active alerts

```sql
SELECT raised_utc, severity, rule_id, zone_name, camera_id, status, message
FROM v_alerts
WHERE is_active
ORDER BY raised_ts DESC
LIMIT 100;
```

**Why.** `is_active` covers OPEN and ACK; raw `status` is NULL until touched.

---

### 20.8 Unique people entering a zone today

**Question.** How many people entered the lift lobby today?

```sql
SELECT COUNT(DISTINCT person_key) AS people,
       COUNT(DISTINCT global_ref) AS identified,
       COUNT(*)                   AS entry_events
FROM v_zone_entries
WHERE zone_name = 'GF Ele-Stairs'
  AND event_utc >= date_trunc('day', now())
  AND event_utc <  date_trunc('day', now()) + interval '1 day';
```

**Why.** `person_key` survives track breaks; `identified` says how much of the figure is trustworthy; `entry_events` shows the movement count for contrast.

---

### 20.9 Entered the building today

```sql
SELECT COUNT(*) AS admission_events,
       MIN(event_utc) AS first_entry,
       MAX(event_utc) AS last_entry
FROM v_facility_crossings
WHERE event_type = 'FACILITY_ENTRY'
  AND event_utc >= date_trunc('day', now());
```

**Why.** Facility crossings exist only in `events`. Note this counts admission *events*; `facility_meta.admitted` counts those that grew the roster.

---

### 20.10 Left the building today

```sql
SELECT COUNT(*) AS departures
FROM v_facility_crossings
WHERE event_type = 'FACILITY_EXIT'
  AND event_utc >= date_trunc('day', now());
```

**Why.** `ZONE_EXIT` would count internal movement instead.

---

### 20.11 The building's occupancy curve

```sql
SELECT event_utc AS at, event_type, door_zone_id, occupancy_after
FROM v_facility_crossings
WHERE event_utc >= date_trunc('day', now())
ORDER BY event_ts;
```

**Why.** Each crossing records the resulting headcount, so the curve is reconstructable from events alone.

---

### 20.12 Unique visitors today

```sql
SELECT COUNT(DISTINCT global_ref) AS identified_visitors,
       COUNT(DISTINCT person_key) AS upper_bound
FROM v_zone_entries
WHERE event_utc >= date_trunc('day', now())
  AND global_ref IS NOT NULL;
```

**Why.** Only a `global_ref` is stable enough to call a visitor. `person_key` gives the upper bound including unresolved tracks.

---

### 20.13 Zone-to-zone movements

```sql
SELECT zone_from, zone_to, camera_id, COUNT(*) AS movements,
       COUNT(DISTINCT person_key) AS people
FROM v_zone_events
WHERE event_type = 'ZONE_TRANSITION'
  AND event_utc >= date_trunc('day', now())
GROUP BY zone_from, zone_to, camera_id
ORDER BY movements DESC;
```

**Why.** `ZONE_TRANSITION` is the authoritative record of a move; the derived pair is excluded by the event-type filter.

---

### 20.14 Busiest doorway

```sql
SELECT door_zone_id, entries, exits, net
FROM v_facility_doors ORDER BY entries DESC;
```

**Why.** Cumulative traffic per doorway. Not occupancy.

---

### 20.15 Occupancy at a moment

```sql
SELECT zone_name, camera_id, occupancy, status, valid_from_utc, valid_to_utc
FROM v_zone_intervals
WHERE valid_from <= EXTRACT(EPOCH FROM TIMESTAMPTZ '2026-08-16 14:00:00+00')
  AND (valid_to > EXTRACT(EPOCH FROM TIMESTAMPTZ '2026-08-16 14:00:00+00')
       OR valid_to IS NULL);
```

---

### 20.16 Peak occupancy yesterday

```sql
SELECT zone_name, camera_id, MAX(occupancy) AS peak
FROM v_zone_intervals
WHERE valid_from_utc >= date_trunc('day', now()) - interval '1 day'
  AND valid_from_utc <  date_trunc('day', now())
  AND NOT is_stale
GROUP BY zone_name, camera_id
ORDER BY peak DESC;
```

---

### 20.17 Time-weighted average occupancy

```sql
SELECT zone_name,
       ROUND((SUM(occupancy * duration_seconds)
              / NULLIF(SUM(duration_seconds), 0))::numeric, 2) AS avg_occupancy,
       ROUND((SUM(duration_seconds) / 3600.0)::numeric, 1)     AS hours_observed
FROM v_zone_intervals
WHERE valid_from_utc >= date_trunc('day', now())
  AND NOT is_stale AND NOT is_open
GROUP BY zone_name
ORDER BY avg_occupancy DESC;
```

**Why.** Rows cover unequal time; weighting is mandatory.

---

### 20.18 Hourly occupancy profile

```sql
SELECT date_trunc('hour', valid_from_utc) AS hour,
       ROUND((SUM(occupancy * duration_seconds)
              / NULLIF(SUM(duration_seconds), 0))::numeric, 2) AS avg_occupancy,
       MAX(occupancy) AS peak
FROM v_zone_intervals
WHERE zone_name = 'Reception'
  AND valid_from_utc >= now() - interval '24 hours'
  AND NOT is_stale
GROUP BY hour ORDER BY hour;
```

---

### 20.19 Time over capacity

```sql
SELECT zone_name,
       ROUND((SUM(duration_seconds) / 60.0)::numeric, 1) AS minutes_over_90pct
FROM v_zone_intervals
WHERE valid_from_utc >= date_trunc('day', now())
  AND capacity_pct >= 90 AND NOT is_stale
GROUP BY zone_name ORDER BY minutes_over_90pct DESC;
```

---

### 20.20 Density history for a zone

```sql
SELECT valid_from_utc, density, occupancy, area_sqm, status
FROM v_zone_intervals
WHERE zone_name = 'Reception'
  AND valid_from_utc >= now() - interval '6 hours'
  AND NOT is_stale
ORDER BY valid_from ASC
LIMIT 500;
```

---

### 20.21 Most crowded zone right now

```sql
SELECT zone_name, camera_id, occupancy, density, capacity_pct, status
FROM v_zone_current
ORDER BY density DESC NULLS LAST
LIMIT 5;
```

**Why.** Density, not occupancy — a small busy zone outranks a large sparse one.

---

### 20.22 Restricted-zone alerts today

```sql
SELECT raised_utc, camera_id, zone_name, severity, message, status
FROM v_alerts
WHERE rule_id = 'R-06'
  AND raised_utc >= date_trunc('day', now())
ORDER BY raised_ts DESC;
```

---

### 20.23 Density alerts this week

```sql
SELECT rule_id, severity, zone_name, camera_id, raised_utc, message
FROM v_alerts
WHERE rule_id IN ('R-01', 'R-02')
  AND raised_utc >= date_trunc('week', now())
ORDER BY raised_ts DESC
LIMIT 200;
```

---

### 20.24 Unacknowledged alerts

```sql
SELECT raised_utc, severity, rule_id, zone_name, camera_id, message
FROM v_alerts
WHERE acknowledged_at IS NULL AND is_active
ORDER BY raised_ts DESC;
```

---

### 20.25 Alert counts by rule today

```sql
SELECT rule_id, severity, COUNT(*) AS n
FROM v_alerts
WHERE raised_utc >= date_trunc('day', now())
GROUP BY rule_id, severity
ORDER BY n DESC;
```

---

### 20.26 Loitering incidents today

```sql
SELECT event_utc, camera_id, zone_name, person_key
FROM v_zone_events
WHERE event_type = 'LOITERING_START'
  AND event_utc >= date_trunc('day', now())
ORDER BY event_ts DESC
LIMIT 100;
```

---

### 20.27 Wrong-way movements

```sql
SELECT event_utc, camera_id, zone_from, zone_to, person_key
FROM v_zone_events
WHERE event_type = 'WRONG_DIRECTION'
  AND event_utc >= now() - interval '7 days'
ORDER BY event_ts DESC
LIMIT 100;
```

---

### 20.28 Group crossings (tailgating)

```sql
SELECT event_utc, camera_id, zone_ref AS zone, event_id
FROM v_zone_events
WHERE event_type = 'GROUP_CROSSING'
  AND event_utc >= now() - interval '7 days'
ORDER BY event_ts DESC;
```

---

### 20.29 Who is inside, and for how long

```sql
SELECT ref, admitted_utc, last_seen_utc,
       ROUND(minutes_inside::numeric, 0) AS minutes_inside,
       entry_zone, last_zone, sightings
FROM v_facility_roster
ORDER BY admitted_at;
```

**Why.** `ref` is an anonymous hash — the system cannot answer *who*.

---

### 20.30 Possible missed exits

```sql
SELECT ref, admitted_utc,
       ROUND((minutes_unseen / 60)::numeric, 1) AS hours_unseen
FROM v_facility_roster
WHERE possibly_stale
ORDER BY last_seen;
```

**Why.** An entry nobody has seen for hours is either a person in unmonitored space or a missed exit. The data cannot distinguish them; report both possibilities.

---

### 20.31 Cross-camera de-duplication, live

```sql
SELECT area_name, occupancy AS distinct_people,
       summed_observations AS naive_sum, duplicates_removed
FROM v_area_current
WHERE area_id = 'OFFICE-01';
```

**Why.** Shows the union and the naive sum side by side. Zones with NULL `occupants` are excluded by the lateral join, so state that caveat when reporting.

---

### 20.32 Identity coverage — run before quoting any people count

```sql
SELECT camera_id,
       COUNT(*) AS entries,
       COUNT(*) FILTER (WHERE identity_resolved) AS identified,
       ROUND(100.0 * COUNT(*) FILTER (WHERE identity_resolved)
             / NULLIF(COUNT(*), 0), 1) AS pct_identified
FROM v_zone_entries
WHERE event_utc >= date_trunc('day', now())
GROUP BY camera_id
ORDER BY pct_identified;
```

**Why.** A camera at 0 % cannot de-duplicate anything; its people counts are an upper bound.

---

### 20.33 Unobserved gaps

```sql
SELECT camera_id, zone_name, valid_from_utc,
       ROUND((duration_seconds / 60.0)::numeric, 1) AS minutes_unobserved
FROM v_zone_intervals
WHERE is_stale AND valid_from_utc >= now() - interval '24 hours'
ORDER BY duration_seconds DESC
LIMIT 20;
```

---

### 20.34 Everything in a window

```sql
SELECT ts_utc, record_type, camera_id, zone_id, detail,
       occupancy, event_type, rule_id, severity, message
FROM v_timeline
WHERE ts_utc >= TIMESTAMPTZ '2026-08-17 09:00:00+00'
  AND ts_utc <  TIMESTAMPTZ '2026-08-17 10:00:00+00'
ORDER BY ts
LIMIT 500;
```

---

### 20.35 Busiest hours by people

```sql
SELECT date_trunc('hour', event_utc) AS hour,
       zone_name,
       COUNT(DISTINCT person_key) AS people
FROM v_zone_entries
WHERE event_utc >= now() - interval '24 hours'
GROUP BY hour, zone_name
ORDER BY people DESC
LIMIT 20;
```

---

### 20.36 Zone configuration reference

```sql
SELECT camera_id, zone_id, zone_name, zone_type, restricted,
       capacity_max, area_sqm, warning_density, critical_density,
       physical_area_id, area_name, enabled
FROM v_zone_config
ORDER BY camera_id, zone_id;
```

**Why.** Useful for resolving a name the user gave into `(camera_id, zone_id)`.

---

## 21. Common Querying Mistakes

### 21.1 Summing occupancy across cameras

**Incorrect**
```sql
SELECT SUM(occupancy) FROM v_zone_current;
```
**Why.** Two cameras watching one room both report the person standing in the overlap. Observed live: the headline read 2 while the room card correctly read 1.
**Correct** — use the facility layer:
```sql
SELECT people_inside FROM v_facility_current;
```

---

### 21.2 Summing `people_in_view`

**Incorrect**
```sql
SELECT SUM(people_in_view) FROM cameras;
```
**Why.** Detections, not people, and the same human in two frames counts twice. It also includes people outside every drawn zone.
**Correct** — §20.1.

---

### 21.3 Counting `person_ref`

**Incorrect**
```sql
SELECT COUNT(DISTINCT person_ref) FROM v_zone_events WHERE event_type = 'ZONE_ENTRY';
```
**Why.** `person_ref` is a hash of the tracker id and changes on every track break. Live, this turned 21 people into 600.
**Correct**
```sql
SELECT COUNT(DISTINCT person_key) FROM v_zone_entries;
```

---

### 21.4 Counting rows instead of people

**Incorrect**
```sql
SELECT COUNT(*) AS people_entered_today
FROM v_zone_events WHERE event_type = 'ZONE_ENTRY';
```
**Why.** `COUNT(*)` counts *events*. One person crossing five zones produces five rows. Observed: 676 rows for perhaps 30 people.
**Correct**
```sql
SELECT COUNT(DISTINCT person_key) FROM v_zone_entries
WHERE event_utc >= date_trunc('day', now());
```

---

### 21.5 Double-counting derived events

**Incorrect**
```sql
SELECT COUNT(*) FROM v_zone_events
WHERE event_type IN ('ZONE_ENTRY', 'ZONE_TRANSITION');
```
**Why.** One movement emits both. Every figure doubles.
**Correct** — use `v_zone_entries`, which already handles it.

---

### 21.6 Zone exits treated as building exits

**Incorrect**
```sql
SELECT COUNT(DISTINCT person_ref) FROM v_zone_events WHERE event_type = 'ZONE_EXIT';
```
**Why.** That is leaving a polygon. Walking from reception into the corridor is a `ZONE_EXIT` and the person is still inside.
**Correct** — §20.10.

---

### 21.7 Unweighted average occupancy

**Incorrect**
```sql
SELECT zone_id, AVG(occupancy) FROM zone_state_ts
WHERE ts >= EXTRACT(EPOCH FROM date_trunc('day', now()))
GROUP BY zone_id;
```
**Why.** Rows cover unequal time; a four-hour row counts the same as a five-second one. Off by up to 8× on live data. It also groups on `zone_id` alone, mixing cameras (§21.8).
**Correct** — §20.17.

---

### 21.8 Ignoring `camera_id` on a zone filter

**Incorrect**
```sql
SELECT * FROM v_zone_current WHERE zone_id = 'ZONE-01';
```
**Why.** Every camera numbers its zones from `ZONE-01`. This mixes unrelated places.
**Correct**
```sql
SELECT camera_id, zone_name, occupancy FROM v_zone_current
WHERE zone_id = 'ZONE-01' AND camera_id = 'CAM-03';
```

---

### 21.9 Treating missing rows as zero

**Incorrect** — concluding "the room was empty overnight" from an absence of rows.
**Why.** No data means nobody was observing. A killed worker emits no offline event.
**Correct** — check `is_stale` (§20.33) and say "not observed".

---

### 21.10 Filtering `status = 'OPEN'` on alerts

**Incorrect**
```sql
SELECT * FROM alerts WHERE status = 'OPEN';
```
**Why.** `status` is NULL until somebody touches the alert, so this misses every untouched one.
**Correct**
```sql
SELECT * FROM v_alerts WHERE is_active;
```

---

### 21.11 Counting alert rows without filtering `kind`

**Incorrect**
```sql
SELECT COUNT(*) FROM alerts WHERE rule_id = 'R-01';
```
**Why.** A rule clearing writes its own row with `kind = 'CLEAR'`, so this roughly doubles the incident count.
**Correct** — add `AND kind = 'FIRE'`.

---

### 21.12 Reading current state from history

**Incorrect**
```sql
SELECT occupancy FROM zone_state_ts ORDER BY ts DESC LIMIT 1;
```
**Why.** Writes are event-driven; the newest history row may be old and says nothing about now.
**Correct** — `v_zone_current`.

---

### 21.13 Including disabled cameras

**Incorrect** — counting every row in `cameras` as a live camera.
**Why.** `enabled = 0` cameras are configured but not running, and deleting a camera does **not** cascade to its zones — orphaned zone rows remain.
**Correct** — filter `enabled IS DISTINCT FROM 0`, and join zones to cameras when counting zones.

---

### 21.14 Joining the fact tables

**Incorrect**
```sql
SELECT * FROM zone_state_ts s JOIN events e
  ON e.camera_id = s.camera_id AND e.zone_id = s.zone_id;
```
**Why.** A time series joined to a stream of occurrences multiplies. Measured at 5.66 trillion rows from a 1.15 GB source.
**Correct** — `v_timeline`, a `UNION ALL`.

---

### 21.15 Assuming `zone_id` is populated on movement events

**Incorrect**
```sql
SELECT * FROM events WHERE event_type = 'ZONE_ENTRY' AND zone_id = 'GF-01';
```
**Why.** Movement events leave `zone_id` NULL and carry the zone in `zone_to`/`zone_from`. Returns nothing, with no error.
**Correct** — use `v_zone_events` (`zone_ref` / `zone_name`) or filter `zone_to`.

---

### 21.16 Mixing timezone styles in one query

**Incorrect**
```sql
WHERE ts >= EXTRACT(EPOCH FROM date_trunc('day', now() AT TIME ZONE 'UTC'))
  AND ts <  EXTRACT(EPOCH FROM now())
```
**Why.** Two different conversions; they agree only while the session is UTC and diverge silently otherwise.
**Correct** — use the view's `_utc` column and compare with `now()`.

---

## 22. Ambiguous Question Handling

Ask for clarification only when the answer would materially change.

**Ask when:**

* "Occupancy" could mean building, room, zone or camera **and** the deployment has physical areas mapped. Offer the building figure as the default and say so.
* A zone name matches several `(camera_id, zone_id)` rows.
* "Entered" could mean a zone or the building **and** the numbers differ by an order of magnitude. Prefer to answer both in one sentence rather than asking.
* A time expression is genuinely unresolvable ("recently", "the other day").

**Do not ask when:**

* Only one `site_id` exists — scope it silently.
* Only one camera covers the named zone.
* "Today" is meant; assume the session timezone and state which day you used.

When you answer without asking, name the scope: *"Reception, on CAM-04, over the last 24 hours."*

---

## 23. Known Limitations

State these when they affect the answer.

1. **`global_ref` is NULL on all history before 2026-08-17.** Identity questions over older data return nothing. The events are real; the identity dimension is absent.
2. **ReID coverage varies by camera.** Where `identity_resolved` is low, people counts are upper bounds and cross-camera de-duplication does not happen. Check §20.32 first.
3. **The facility roster only decreases on an observed exit.** No timeout. If exits are not detected it drifts upward indefinitely.
4. **`FACILITY_ENTRY` / `FACILITY_EXIT` carry no `global_ref`** — only `person_ref`. Building crossings cannot be joined to a person's zone movements.
5. **Physical areas may be unmapped.** If `physical_areas` is empty, room-level de-duplication is not in effect anywhere and only zone-level figures exist.
6. **`area_state_ts` is only written for mapped zones.**
7. **No `sites` table.** `site_id` is an unvalidated label.
8. **No foreign keys.** Orphaned rows are possible — deleting a camera does not remove its zones.
9. **Camera online/offline is not stored.** Derive it (§8).
10. **`R-04` bottleneck detection is not implemented.** Explicitly out of scope.
11. **Cross-camera re-identification is implemented but unverified against ground truth on real cameras.** The published evaluation used a synthetic second camera; real cameras score lower.
12. **No embeddings, faces or PII are stored.** The system cannot answer "who". `person_ref` and `global_ref` are anonymous, session-scoped hashes.
13. **Views are not recreated automatically.** Rebuilding the schema drops them until `scripts/pg_apply.py` runs.

---

## 24. Deprecated / Legacy Database Objects

| Object | Status |
|---|---|
| `services/api/ddl.sql` | **Legacy, superseded.** Hand-written, drifted to five tables against nine, missing `zone_live` entirely. Replaced by the generated `ddl_pg.sql`. Do not use. |
| SQLite backend (`services/api/sqlite_store.py`, `data/finblade.db`) | **Still supported** and selected when `DATABASE_URL` is unset. Same logical schema. Not the Postgres deployment; a SQLite file may hold a divergent history from an earlier period. |
| `InMemoryStore` | Ephemeral, for tests. Not a deployment target. |
| `events.person_ref` | Not deprecated, but **superseded for counting** by `global_ref` / `person_key`. |
| `alerts.frame` | Snapshot path on disk, not a database object. |

No table in the current Postgres schema is deprecated. All thirteen are written
by the running application.

---

## 25. Quick Reference for the Chatbot

| I need… | Use |
|---|---|
| People in the building | `v_facility_current.people_inside` |
| People in a room | `v_area_current.occupancy` |
| People in a polygon | `v_zone_current.occupancy` |
| People a camera can see | `v_camera_status.people_in_view` |
| Distinct people | `COUNT(DISTINCT person_key)` |
| Unique visitors | `COUNT(DISTINCT global_ref)` |
| Entered a zone | `v_zone_entries` |
| Entered the building | `v_facility_crossings`, `FACILITY_ENTRY` |
| Left the building | `v_facility_crossings`, `FACILITY_EXIT` |
| Moved between zones | `v_zone_events`, `ZONE_TRANSITION` |
| History / averages | `v_zone_intervals`, weighted by `duration_seconds` |
| Alerts | `v_alerts`, filter `is_active` |
| Camera state | `v_camera_status.effective_state` |
| Anything in a window | `v_timeline` |

**Six things never to do**

1. `SUM(occupancy)` across cameras, or `SUM(people_in_view)`.
2. `COUNT(DISTINCT person_ref)`.
3. `COUNT(*)` when the question says "people".
4. `ZONE_ENTRY` + `ZONE_TRANSITION` together.
5. `AVG(occupancy)` unweighted.
6. `SELECT cameras.source` — it contains a password.

---

# Recommended Chatbot System Instructions

Paste the block below into the chatbot as its system prompt.

```
You are the FinBlade CCTV analytics assistant. You answer questions about a
physical site from its crowd-analytics database using read-only PostgreSQL.

SQL RULES
- Generate only SELECT, WITH and EXPLAIN. Never INSERT, UPDATE, DELETE, DROP,
  ALTER, TRUNCATE, CREATE, GRANT, REVOKE, COPY, DO or CALL.
- Name columns explicitly. Never SELECT * on events, zone_state_ts or
  v_timeline; some views refuse SELECT * because a column is withheld.
- LIMIT any query that lists rows. Aggregate when asked for a total.
- Never select cameras.source or cameras.stream_url: they contain RTSP URLs
  with passwords. Refuse if asked for camera credentials.
- If a query errors, read the error and fix the SQL. A permission error on
  person_ref means you should be using person_key.
- Never invent a result. If the query returns nothing, say so.

USE THESE VIEWS. You have no access to any base table and need none.
  v_facility_current    the BUILDING count: people_inside
  v_facility_roster     who is inside, how long, likely missed exits
  v_facility_crossings  entering and leaving the building
  v_facility_doors      cumulative traffic per doorway
  v_area_current        room occupancy, de-duplicated across cameras
  v_area_intervals      room occupancy history
  v_zone_current        live occupancy per polygon
  v_zone_intervals      zone history, with the duration each reading stood for
  v_zone_entries        arrivals in a zone, safe to count
  v_zone_events         all events with the zone resolved
  v_zone_config         zone definitions and thresholds
  v_camera_status       camera health, online/offline already derived
  v_alerts              alerts with lifecycle resolved
  v_timeline            everything on one time axis

COUNTING PEOPLE — the six errors that return plausible wrong numbers
1. Never SUM(occupancy) across cameras or SUM(people_in_view). Two cameras can
   watch one room and both see the same person. For the building use
   v_facility_current.people_inside; for a room use v_area_current.occupancy.
2. Never COUNT(DISTINCT person_ref). It is a hash of the tracker id and changes
   on every track break; it measures churn. Use person_key, or global_ref for
   confirmed unique visitors.
3. COUNT(*) counts events, not people. One person crossing five zones makes
   five rows.
4. Never count ZONE_ENTRY and ZONE_TRANSITION together — one movement emits
   both. v_zone_entries already handles this.
5. Never AVG(occupancy). Rows cover unequal time. Use
   SUM(occupancy*duration_seconds)/SUM(duration_seconds) and exclude is_stale.
6. ZONE_EXIT is leaving a polygon, not the building. Use FACILITY_EXIT.

SCOPE AND JOINS
- zone_id is unique only within a camera. Always filter or join on camera_id.
- site_id is a plain label; there is no sites table and no foreign keys.
- Camera online/offline is derived inside v_camera_status. Use effective_state
  or is_online; do not re-implement the rule.

TIME
- All stored timestamps are epoch seconds UTC in DOUBLE PRECISION columns.
- Prefer the views' converted columns: event_utc, reading_utc, valid_from_utc,
  raised_utc, ts_utc. Compare them directly with now() and date_trunc.
- On raw tables wrap with to_timestamp(ts). Do not mix styles in one query.

CURRENT VERSUS HISTORICAL
- Current: v_zone_current, v_area_current, v_facility_current, v_camera_status.
- Historical: v_zone_intervals, v_area_intervals, v_zone_events, v_alerts.
- Never read current state from the newest history row; writes are event-driven
  and the newest row may be hours old.

QUALIFY YOUR ANSWERS
- Before quoting any people count, check identity coverage:
  COUNT(*) FILTER (WHERE identity_resolved) over v_zone_entries. A camera at 0%
  cannot de-duplicate; its figures are an upper bound. Say so.
- Missing data is not zero. An absence of rows means nobody was observing.
  Check is_stale and report "not observed" rather than "empty".
- The system holds no identity: person refs are anonymous hashes that do not
  persist across restarts. You can answer how many, where and when — never who.
  Say so plainly if asked.
- Always state the scope you used: which zone, which camera, which window.

WORKFLOW
Understand the intent, decide the scope, classify the question (current /
historical / event count / people count / occupancy / density / alert), pick the
view, decide whether de-duplication matters, choose the timestamp column, write
the SELECT, execute, sanity-check the magnitude, then answer in plain language
with the scope and any caveat stated.
```
