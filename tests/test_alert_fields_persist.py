"""Every field an Alert declares must survive the store.

WHY THIS TEST EXISTS. Four times in one day a field was added to a model
upstream and silently dropped at the persistence layer, each time returning
success to the caller:

  * zones.physical_area_id   - added to the dataclass, missing from the INSERT
  * zones.required_ppe       - same, one release later
  * zone_live.extra          - an explicit key whitelist that dropped PPE counts
  * alerts.track_id          - the alerts table has no free-form payload column,
                               so a per-person alert reached the UI with
                               track_id=None and no way to point at the crop of
                               the person it accused

The shape is always the same and always silent: the write path names columns
explicitly, an unknown key is simply not mentioned, and nothing errors. Pinning
one field per incident has not stopped it happening again, so this guard is
written against the MODEL rather than against a field list: it reflects over
Alert's declared fields and asserts each one round-trips. A field added to Alert
tomorrow is covered by this test the moment it is declared, with no test edit.

It deliberately does NOT assert values are equal by identity for floats, and it
deliberately DOES run against the in-memory store as well as Postgres - the
in-memory store keeps everything, so a failure there means the model itself is
broken, while a Postgres-only failure means a missing column.
"""
import dataclasses
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.rules import Alert


def _probe_alert() -> dict:
    """An alert carrying a distinctive, non-default value in every field.

    Defaults are useless for this: a dropped field reads back as its default and
    the assertion passes. Every value here is chosen to differ from the
    dataclass default so a drop is visible.
    """
    a = Alert(rule_id="R-11", severity="COMPLIANCE",
              message="probe: all fields must survive",
              ts=1735689600.5, zone_id="Z-PROBE", camera_id="CAM-PROBE",
              # CLEAR, not the default FIRE - the assertion below rejects a
              # probe value that matches its default, since such a field reads
              # back correct whether it was stored or dropped. A CLEAR alert is
              # filtered out of the live feed, so _read_back falls through to
              # history for it, which is the path the history page uses anyway.
              person_ref="pr_probe", kind="CLEAR", track_id=4242)
    payload = a.as_dict()
    for f in dataclasses.fields(Alert):
        assert payload[f.name] != f.default, (
            f"{f.name} probe value equals the field default, so this test "
            f"cannot tell a dropped field from a stored one")
    return payload


class _RoundTrip:
    """Shared body. Subclasses supply a store."""

    def _read_back(self, store, alert_id):
        for a in store.list_alerts(unacked_only=False):
            if str(a.get("alert_id")) == str(alert_id):
                return a
        for a in store.list_alerts_history(0, time.time() + 86400, limit=100000):
            if str(a.get("alert_id")) == str(alert_id):
                return a
        return None

    def test_every_declared_alert_field_round_trips(self):
        store = self.store
        payload = _probe_alert()
        alert_id = store.save_alert(dict(payload))
        got = self._read_back(store, alert_id)
        self.assertIsNotNone(got, "the alert did not come back from the store")

        missing = [f.name for f in dataclasses.fields(Alert) if f.name not in got]
        self.assertEqual(missing, [], f"fields absent from the stored alert: {missing}")

        dropped = {f.name: (payload[f.name], got.get(f.name))
                   for f in dataclasses.fields(Alert)
                   if got.get(f.name) != payload[f.name]}
        self.assertEqual(
            dropped, {},
            "fields changed between write and read (expected, got): "
            f"{dropped}. A column is almost certainly missing from the INSERT "
            "or the SELECT in services/api/postgres_store.py.")


class TestInMemoryStore(_RoundTrip, unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("FINBLADE_INMEMORY", "1")
        from services.api.store import InMemoryStore
        self.store = InMemoryStore()


class TestPostgresStore(_RoundTrip, unittest.TestCase):
    """The real thing. Skipped where no cluster is reachable, like the rest of
    the pg-backed tests - but this is the one that would have caught track_id,
    because the in-memory store keeps unknown keys and Postgres does not."""

    @classmethod
    def setUpClass(cls):
        cls._store = None
        dsn = os.environ.get("DATABASE_URL",
                             "postgresql://postgres@127.0.0.1:5432/finblade")
        try:
            from services.api.postgres_store import PostgresStore
            st = PostgresStore(dsn)
            st.list_alerts(unacked_only=False)
            cls._store = st
        except Exception:                                    # noqa: BLE001
            cls._store = None

    def setUp(self):
        if self._store is None:
            self.skipTest("no reachable Postgres")
        self.store = self._store
        self._ids = []

    def tearDown(self):
        # The probe alert is real as far as the dashboard is concerned; leaving
        # it behind would put a fake compliance failure in front of the client.
        # Deleted by primary key rather than through delete_alerts(scope),
        # which would take the operator's genuine alerts with it.
        for alert_id in getattr(self, "_ids", []):
            with self.store._pool.connection() as conn:
                conn.execute("DELETE FROM alerts WHERE alert_id = %s",
                             (int(alert_id),))

    def test_every_declared_alert_field_round_trips(self):
        payload = _probe_alert()
        alert_id = self.store.save_alert(dict(payload))
        self._ids.append(alert_id)
        got = self._read_back(self.store, alert_id)
        self.assertIsNotNone(got, "the alert did not come back from the store")

        missing = [f.name for f in dataclasses.fields(Alert) if f.name not in got]
        self.assertEqual(missing, [],
                         f"columns missing from the alerts table/SELECT: {missing}")

        dropped = {f.name: (payload[f.name], got.get(f.name))
                   for f in dataclasses.fields(Alert)
                   if got.get(f.name) != payload[f.name]}
        self.assertEqual(
            dropped, {},
            "fields changed between write and read (expected, got): "
            f"{dropped}. Check the INSERT column list, its placeholders, its "
            "params tuple, and _ALERT_COLS - all four must name the field.")


if __name__ == "__main__":
    unittest.main()
