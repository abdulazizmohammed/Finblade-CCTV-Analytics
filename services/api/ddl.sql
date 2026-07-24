-- FinBlade CCTV — core schema (UC-30/31).
-- Target: Postgres (TimescaleDB optional for zone_state_ts). Apply once:
--   psql "$DATABASE_URL" -f services/api/ddl.sql
-- Not applied in the authoring env (no server — BLOCKERS.md B-3).

CREATE TABLE IF NOT EXISTS cameras (
    camera_id   TEXT PRIMARY KEY,
    site_id     TEXT NOT NULL,
    label       TEXT,
    online      BOOLEAN NOT NULL DEFAULT TRUE,
    last_seen   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS zones (
    zone_id       TEXT PRIMARY KEY,
    zone_name     TEXT NOT NULL,
    camera_id     TEXT REFERENCES cameras(camera_id),
    restricted    BOOLEAN NOT NULL DEFAULT FALSE,
    capacity_max  INTEGER NOT NULL DEFAULT 0,
    area_sqm      DOUBLE PRECISION NOT NULL DEFAULT 0,
    polygon       JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS zone_events (
    event_id    UUID PRIMARY KEY,
    event_type  TEXT NOT NULL,
    camera_id   TEXT NOT NULL,
    site_id     TEXT NOT NULL,
    zone_from   TEXT,
    zone_to     TEXT,
    person_ref  TEXT,               -- anonymous hash only; never PII
    ts          TIMESTAMPTZ NOT NULL,
    payload     JSONB
);
CREATE INDEX IF NOT EXISTS ix_zone_events_ts ON zone_events(ts);
CREATE INDEX IF NOT EXISTS ix_zone_events_type ON zone_events(event_type);

CREATE TABLE IF NOT EXISTS zone_state_ts (
    id              BIGSERIAL PRIMARY KEY,
    zone_id         TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    occupancy       INTEGER NOT NULL,
    density         DOUBLE PRECISION NOT NULL,
    capacity_pct    DOUBLE PRECISION NOT NULL,
    inflow_per_min  DOUBLE PRECISION NOT NULL,
    outflow_per_min DOUBLE PRECISION NOT NULL,
    status          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_zone_state_ts_zone_ts ON zone_state_ts(zone_id, ts);
-- TimescaleDB (optional): SELECT create_hypertable('zone_state_ts','ts');

CREATE TABLE IF NOT EXISTS alerts (
    alert_id        BIGSERIAL PRIMARY KEY,
    rule_id         TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT NOT NULL,
    zone_id         TEXT,
    camera_id       TEXT,
    person_ref      TEXT,
    ts              TIMESTAMPTZ NOT NULL,
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_alerts_unacked ON alerts(acknowledged_by) WHERE acknowledged_by IS NULL;
