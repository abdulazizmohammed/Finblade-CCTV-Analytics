-- FinBlade CCTV - PostgreSQL schema. THE AUTHORITY.
--
-- Hand-maintained since SQLite was removed. It used to be generated from the
-- SQLite schema by scripts/gen_pg_ddl.py, which was the right design while two
-- backends existed: one definition, mechanically translated, so they could not
-- drift. With one backend that indirection has nothing left to protect.
--
-- WHAT STOPS THIS DRIFTING NOW. tests/test_store_conformance.py exercises every
-- PostgresStore method against a database built from this file. A column added
-- to a write without being added here fails there, loudly, rather than surfacing
-- months later as "column does not exist" on an endpoint nobody touched. That
-- suite is the reason this file can be hand-maintained; do not skip it.
--
-- EDITING. Add the column to the CREATE TABLE *and* add a matching
--   ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <c> <type>;
-- in the upgrade section below, or existing deployments never receive it -
-- CREATE TABLE IF NOT EXISTS is a no-op on a table that already exists. Every
-- statement here is idempotent and the API applies the whole file at startup.
--
-- Timestamps are DOUBLE PRECISION epoch seconds, matching what the application
-- speaks end to end. The analytics views expose a real timestamptz alongside.
--
--   psql "$DATABASE_URL" -f services/api/ddl_pg.sql

CREATE TABLE IF NOT EXISTS alerts (
    alert_id               BIGSERIAL PRIMARY KEY,
    rule_id                TEXT,
    severity               TEXT,
    message                TEXT,
    zone_id                TEXT,
    camera_id              TEXT,
    person_ref             TEXT,
    ts                     DOUBLE PRECISION,  -- epoch seconds, UTC
    frame                  TEXT,
    kind                   TEXT,
    acknowledged_by        TEXT,
    acknowledged_at        DOUBLE PRECISION,  -- epoch seconds, UTC
    status                 TEXT DEFAULT 'OPEN',
    note                   TEXT,
    resolved_by            TEXT,
    resolved_at            DOUBLE PRECISION,  -- epoch seconds, UTC
    site_id                TEXT
);

CREATE TABLE IF NOT EXISTS area_state_ts (
    id                     BIGSERIAL PRIMARY KEY,
    area_id                TEXT,
    ts                     DOUBLE PRECISION,  -- epoch seconds, UTC
    occupancy              BIGINT,
    capacity_pct           DOUBLE PRECISION,
    density                DOUBLE PRECISION,
    summed_observations    BIGINT,
    camera_count           BIGINT,
    site_id                TEXT
);

-- The topology, resolved into rows SQL can join against.
--
-- config/topology.yaml is the authority and this is a projection of it, written
-- by scripts/sync_topology.py. It exists because the journey views need to ask
-- "could a person have walked from here to there in this long?" and the answer
-- lives in a YAML file the database cannot read.
--
-- DIRECTED, and both directions are stored even though the YAML pair is
-- undirected. A join does not want to normalise (a,b) vs (b,a).
--
-- min_seconds IS NEGATIVE FOR OVERLAPPING PAIRS, deliberately. Two cameras
-- watching the same floor see one person at the same instant, and independent
-- camera processes disagree about the clock by a second or two, so the window
-- opens slightly before zero. Baking the tolerance into the bound means every
-- consumer is one BETWEEN with no special cases -- which is the whole point of
-- materialising this. See finblade/topology.py, which branches instead because
-- it answers a different question one pair at a time.
--
-- AN ABSENT ROW MEANS INFEASIBLE. With allow_unknown_pairs: false the sync
-- writes nothing for unsurveyed pairs, and a missing row drops the link on the
-- join. Nothing else needs to know the rule.
CREATE TABLE IF NOT EXISTS camera_transits (
    from_camera            TEXT NOT NULL,
    to_camera              TEXT NOT NULL,
    min_seconds            DOUBLE PRECISION NOT NULL,
    max_seconds            DOUBLE PRECISION NOT NULL,
    -- 'surveyed' | 'overlapping' | 'default' | 'same_camera'. Carried through
    -- to v_journey_links so a trace can say which hops rest on paced times and
    -- which rest on a fallback window.
    pair_kind              TEXT NOT NULL DEFAULT 'default',
    updated_at             DOUBLE PRECISION,
    PRIMARY KEY (from_camera, to_camera)
);

CREATE TABLE IF NOT EXISTS cameras (
    camera_id              TEXT PRIMARY KEY,
    site_id                TEXT,
    last_seen              DOUBLE PRECISION,  -- epoch seconds, UTC
    name                   TEXT,
    state                  TEXT,
    input_fps              DOUBLE PRECISION,
    resolution             TEXT,
    dropped_frames         BIGINT,
    reconnects             BIGINT,
    loops                  BIGINT,
    frozen                 BIGINT,
    enabled                BIGINT,
    stream_url             TEXT,
    health_ts              DOUBLE PRECISION,
    sim_failure            BIGINT DEFAULT 0,
    source                 TEXT,
    people_in_view         BIGINT DEFAULT 0,
    people_in_zones        BIGINT DEFAULT 0,
    tracking_quality       TEXT,
    counts_reliable        BIGINT,
    counting_mode          TEXT,
    mean_confidence        DOUBLE PRECISION,
    track_churn_per_min    DOUBLE PRECISION,
    detector_saturation    DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS events (
    event_id               TEXT PRIMARY KEY,
    event_type             TEXT,
    camera_id              TEXT,
    site_id                TEXT,
    zone_id                TEXT,
    zone_from              TEXT,
    zone_to                TEXT,
    person_ref             TEXT,
    ts                     DOUBLE PRECISION,  -- epoch seconds, UTC
    frame                  TEXT,
    payload                TEXT,
    global_ref             TEXT
);

CREATE TABLE IF NOT EXISTS facility_doors (
    door_zone_id           TEXT PRIMARY KEY,
    entries                BIGINT,
    exits                  BIGINT
);

CREATE TABLE IF NOT EXISTS facility_meta (
    key                    TEXT PRIMARY KEY,
    value                  DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS facility_presence (
    ref                    TEXT PRIMARY KEY,
    admitted_at            DOUBLE PRECISION,
    last_seen              DOUBLE PRECISION,  -- epoch seconds, UTC
    entry_zone             TEXT,
    last_zone              TEXT,
    sightings              BIGINT
);

CREATE TABLE IF NOT EXISTS forwarder_cursors (
    name                   TEXT PRIMARY KEY,
    ts                     DOUBLE PRECISION NOT NULL  -- epoch seconds, UTC
);

CREATE TABLE IF NOT EXISTS physical_areas (
    area_id                TEXT PRIMARY KEY,
    name                   TEXT,
    area_type              TEXT,
    capacity_max           BIGINT,
    area_sqm               DOUBLE PRECISION,
    site_id                TEXT,
    updated_at             DOUBLE PRECISION  -- epoch seconds, UTC
);

CREATE TABLE IF NOT EXISTS reports (
    report_id              BIGSERIAL PRIMARY KEY,
    kind                   TEXT,
    generated_at           DOUBLE PRECISION,  -- epoch seconds, UTC
    from_ts                DOUBLE PRECISION,  -- epoch seconds, UTC
    to_ts                  DOUBLE PRECISION,  -- epoch seconds, UTC
    peak_occupancy         BIGINT,
    total_alerts           BIGINT,
    payload                TEXT
);

CREATE TABLE IF NOT EXISTS zone_live (
    camera_id              TEXT NOT NULL,
    zone_id                TEXT NOT NULL,
    site_id                TEXT,
    zone_name              TEXT,
    zone_type              TEXT,
    restricted             BIGINT,
    ts                     DOUBLE PRECISION,  -- epoch seconds, UTC
    occupancy              BIGINT,
    density                DOUBLE PRECISION,
    capacity_pct           DOUBLE PRECISION,
    peak_occupancy         BIGINT,
    avg_occupancy          DOUBLE PRECISION,
    trend                  TEXT,
    extra                  TEXT,
    inflow                 DOUBLE PRECISION,
    outflow                DOUBLE PRECISION,
    status                 TEXT,
    occupants              TEXT,
    physical_area_id       TEXT,
    PRIMARY KEY (camera_id, zone_id)
);

CREATE TABLE IF NOT EXISTS zone_state_ts (
    id                     BIGSERIAL PRIMARY KEY,
    zone_id                TEXT,
    camera_id              TEXT,
    zone_name              TEXT,
    zone_type              TEXT,
    restricted             BIGINT,
    ts                     DOUBLE PRECISION,  -- epoch seconds, UTC
    occupancy              BIGINT,
    density                DOUBLE PRECISION,
    capacity_pct           DOUBLE PRECISION,
    peak_occupancy         BIGINT,
    avg_occupancy          DOUBLE PRECISION,
    trend                  TEXT,
    extra                  TEXT,
    inflow                 DOUBLE PRECISION,
    outflow                DOUBLE PRECISION,
    status                 TEXT,
    site_id                TEXT
);

CREATE TABLE IF NOT EXISTS zones (
    camera_id              TEXT,
    zone_id                TEXT,
    zone_name              TEXT,
    zone_type              TEXT,
    restricted             BIGINT,
    capacity_max           BIGINT,
    area_sqm               DOUBLE PRECISION,
    warning_density        DOUBLE PRECISION,
    critical_density       DOUBLE PRECISION,
    loitering_threshold_sec DOUBLE PRECISION,
    colour                 TEXT,
    enabled                BIGINT,
    normalized_polygon     TEXT,
    polygon                TEXT,
    adjacency_list         TEXT,
    updated_at             DOUBLE PRECISION,  -- epoch seconds, UTC
    physical_area_id       TEXT,
    PRIMARY KEY (camera_id, zone_id)
);

-- Bring an EXISTING database up to the schema above. Every
-- statement is idempotent; on a current database all are no-ops.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS rule_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS severity TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS message TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS zone_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS camera_id TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS person_ref TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS frame TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_by TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at DOUBLE PRECISION;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'OPEN';
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS note TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_by TEXT;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_at DOUBLE PRECISION;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE area_state_ts ADD COLUMN IF NOT EXISTS area_id TEXT;
ALTER TABLE area_state_ts ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION;
ALTER TABLE area_state_ts ADD COLUMN IF NOT EXISTS capacity_pct DOUBLE PRECISION;
ALTER TABLE area_state_ts ADD COLUMN IF NOT EXISTS density DOUBLE PRECISION;
ALTER TABLE area_state_ts ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE camera_transits ADD COLUMN IF NOT EXISTS min_seconds DOUBLE PRECISION;
ALTER TABLE camera_transits ADD COLUMN IF NOT EXISTS max_seconds DOUBLE PRECISION;
ALTER TABLE camera_transits ADD COLUMN IF NOT EXISTS pair_kind TEXT DEFAULT 'default';
ALTER TABLE camera_transits ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS state TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS input_fps DOUBLE PRECISION;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS resolution TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS dropped_frames BIGINT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS reconnects BIGINT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS loops BIGINT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS frozen BIGINT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS enabled BIGINT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS stream_url TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS health_ts DOUBLE PRECISION;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS sim_failure BIGINT DEFAULT 0;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS people_in_view BIGINT DEFAULT 0;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS people_in_zones BIGINT DEFAULT 0;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS tracking_quality TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS counts_reliable BIGINT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS counting_mode TEXT;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS mean_confidence DOUBLE PRECISION;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS track_churn_per_min DOUBLE PRECISION;
ALTER TABLE cameras ADD COLUMN IF NOT EXISTS detector_saturation DOUBLE PRECISION;
ALTER TABLE events ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS camera_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS zone_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS zone_from TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS zone_to TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS person_ref TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION;
ALTER TABLE events ADD COLUMN IF NOT EXISTS frame TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS payload TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS global_ref TEXT;
ALTER TABLE facility_doors ADD COLUMN IF NOT EXISTS entries BIGINT;
ALTER TABLE facility_doors ADD COLUMN IF NOT EXISTS exits BIGINT;
ALTER TABLE facility_meta ADD COLUMN IF NOT EXISTS value DOUBLE PRECISION;
ALTER TABLE facility_presence ADD COLUMN IF NOT EXISTS admitted_at DOUBLE PRECISION;
ALTER TABLE facility_presence ADD COLUMN IF NOT EXISTS last_seen DOUBLE PRECISION;
ALTER TABLE facility_presence ADD COLUMN IF NOT EXISTS entry_zone TEXT;
ALTER TABLE facility_presence ADD COLUMN IF NOT EXISTS last_zone TEXT;
ALTER TABLE facility_presence ADD COLUMN IF NOT EXISTS sightings BIGINT;
ALTER TABLE forwarder_cursors ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION;
ALTER TABLE physical_areas ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE physical_areas ADD COLUMN IF NOT EXISTS area_type TEXT;
ALTER TABLE physical_areas ADD COLUMN IF NOT EXISTS capacity_max BIGINT;
ALTER TABLE physical_areas ADD COLUMN IF NOT EXISTS area_sqm DOUBLE PRECISION;
ALTER TABLE physical_areas ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE physical_areas ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS generated_at DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS from_ts DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS to_ts DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS payload TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS zone_name TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS zone_type TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS restricted BIGINT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS occupancy BIGINT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS density DOUBLE PRECISION;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS capacity_pct DOUBLE PRECISION;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS peak_occupancy BIGINT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS avg_occupancy DOUBLE PRECISION;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS trend TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS extra TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS inflow DOUBLE PRECISION;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS outflow DOUBLE PRECISION;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS occupants TEXT;
ALTER TABLE zone_live ADD COLUMN IF NOT EXISTS physical_area_id TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS zone_id TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS camera_id TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS zone_name TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS zone_type TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS ts DOUBLE PRECISION;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS density DOUBLE PRECISION;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS capacity_pct DOUBLE PRECISION;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS avg_occupancy DOUBLE PRECISION;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS trend TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS extra TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS inflow DOUBLE PRECISION;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS outflow DOUBLE PRECISION;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE zone_state_ts ADD COLUMN IF NOT EXISTS site_id TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS zone_name TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS zone_type TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS restricted BIGINT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS capacity_max BIGINT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS area_sqm DOUBLE PRECISION;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS warning_density DOUBLE PRECISION;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS critical_density DOUBLE PRECISION;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS loitering_threshold_sec DOUBLE PRECISION;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS colour TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS enabled BIGINT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS normalized_polygon TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS polygon TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS adjacency_list TEXT;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS updated_at DOUBLE PRECISION;
ALTER TABLE zones ADD COLUMN IF NOT EXISTS physical_area_id TEXT;

-- Indexes, mirrored from the SQLite schema.
CREATE INDEX IF NOT EXISTS ix_alerts_ts ON alerts(ts);
CREATE INDEX IF NOT EXISTS ix_ast_area_ts ON area_state_ts(area_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_gref ON events(global_ref);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_reports_gen ON reports(generated_at);
CREATE INDEX IF NOT EXISTS ix_zst_zone_cam_id ON zone_state_ts(zone_id, camera_id, id);
CREATE INDEX IF NOT EXISTS ix_zst_zone_ts ON zone_state_ts(zone_id, ts);
CREATE INDEX IF NOT EXISTS ix_zones_area ON zones(physical_area_id);

-- The index the analytics views live on. zone_state_ts is scanned by
-- (camera_id, zone_id, ts) for every interval and window function; the
-- SQLite schema's (zone_id, ts) index does not cover the partition.
CREATE INDEX IF NOT EXISTS ix_zst_cam_zone_ts
    ON zone_state_ts (camera_id, zone_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_cam_zone_ts
    ON events (camera_id, zone_id, ts);

-- TimescaleDB, if present. Optional; the views do not depend on it.
-- SELECT create_hypertable('zone_state_ts', 'ts', chunk_time_interval => 86400);
