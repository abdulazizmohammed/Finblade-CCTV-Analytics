"""Physical areas end-to-end through the API.

test_areas.py proves the counting rules in isolation. This proves the wiring:
zone state posted by two camera workers, through validation, storage and the
areas endpoint, gives ONE person in an office watched by two cameras.
"""

import os
import time

import pytest

os.environ.setdefault("FINBLADE_INMEMORY", "1")

from fastapi.testclient import TestClient          # noqa: E402
from services.api.app import app, svc              # noqa: E402
from services.api.store import InMemoryStore       # noqa: E402

# Real wall-clock timestamps. Workers post time.time(), and both the live-zone
# freshness filter and the area observation TTL measure staleness against the
# real clock — a synthetic epoch reads as decades old and is discarded.
NOW = time.time()


@pytest.fixture()
def client():
    """Fresh store per test against the app module's global service.

    Deliberately NOT importlib.reload(app): reloading rebinds the module's
    globals, and every test module that had already imported `app` or `svc`
    kept referring to the old objects. That broke 23 tests in five unrelated
    files, all of which pass on their own — the reload was the only link.

    No context manager either, matching test_identity_api.py: it skips the
    lifespan so background monitors and the camera manager never start.
    """
    svc.store = InMemoryStore()
    svc._areas = None                 # drop the cached area tracker
    svc._areas_loaded = 0.0
    yield TestClient(app)


def _state(camera_id, zone_id, occupants, ts, occupancy=None):
    """A worker's 5s zone-state post."""
    return {
        "zone_id": zone_id, "camera_id": camera_id,
        "occupancy": len(occupants) if occupancy is None else occupancy,
        "density": 0.0, "capacity_pct": 0.0,
        "inflow_per_min": 0.0, "outflow_per_min": 0.0,
        "status": "NORMAL", "ts": ts,
        "occupants": list(occupants),
    }


def _map_office(client):
    """OFFICE-01, watched by CAM-04/ZONE-01 and CAM-05/ZONE-03."""
    r = client.post("/api/v1/areas", json={
        "area_id": "OFFICE-01", "name": "Office 1",
        "area_type": "ROOM", "capacity_max": 10})
    assert r.status_code == 200, r.text
    for cam, zid in (("CAM-04", "ZONE-01"), ("CAM-05", "ZONE-03")):
        r = client.post("/api/v1/zones", json={"camera_id": cam, "zones": [{
            "zone_id": zid, "zone_name": f"Office view {cam}",
            "zone_type": "MONITORED", "physical_area_id": "OFFICE-01",
            "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
        }]})
        assert r.status_code == 200, r.text


def test_one_office_two_cameras_one_person(client):
    """THE requirement: two cameras, same person, the office shows 1."""
    _map_office(client)

    # Same global_ref from both cameras — ReID resolved them to one person.
    client.post("/api/v1/zones/state", json=_state("CAM-04", "ZONE-01", ["gp_p001"], NOW))
    client.post("/api/v1/zones/state", json=_state("CAM-05", "ZONE-03", ["gp_p001"], NOW))

    body = client.get("/api/v1/areas/state").json()
    office = next(a for a in body["areas"] if a["area_id"] == "OFFICE-01")

    assert office["occupancy"] == 1, "one person in one office, not two"
    assert office["summed_observations"] == 2      # what summing would report
    assert office["double_counted"] == 1
    assert office["camera_count"] == 2
    # Each camera's own reading stays available for diagnostics.
    assert sorted(o["observed"] for o in office["observations"]) == [1, 1]


def test_partial_overlap_through_the_api(client):
    _map_office(client)
    client.post("/api/v1/zones/state",
                json=_state("CAM-04", "ZONE-01", ["gp_1", "gp_2"], NOW))
    client.post("/api/v1/zones/state",
                json=_state("CAM-05", "ZONE-03", ["gp_2", "gp_3"], NOW))

    st = client.get("/api/v1/areas/OFFICE-01").json()
    assert st["occupancy"] == 3            # not 4 (sum), not 2 (max)
    assert st["summed_observations"] == 4


def test_area_holds_when_one_camera_loses_the_person(client):
    _map_office(client)
    client.post("/api/v1/zones/state", json=_state("CAM-04", "ZONE-01", ["gp_p001"], NOW))
    client.post("/api/v1/zones/state", json=_state("CAM-05", "ZONE-03", ["gp_p001"], NOW))
    assert client.get("/api/v1/areas/OFFICE-01").json()["occupancy"] == 1

    # CAM-04 drops the track; CAM-05 still sees them.
    client.post("/api/v1/zones/state", json=_state("CAM-04", "ZONE-01", [], NOW + 1))
    client.post("/api/v1/zones/state", json=_state("CAM-05", "ZONE-03", ["gp_p001"], NOW + 1))
    assert client.get("/api/v1/areas/OFFICE-01").json()["occupancy"] == 1


def test_areas_endpoint_lists_the_zone_mapping(client):
    _map_office(client)
    body = client.get("/api/v1/areas").json()
    office = next(a for a in body["areas"] if a["area_id"] == "OFFICE-01")
    assert office["multi_camera"] is True
    assert {(z["camera_id"], z["zone_id"]) for z in office["zones"]} == {
        ("CAM-04", "ZONE-01"), ("CAM-05", "ZONE-03")}
    assert office["capacity_max"] == 10


def test_unknown_area_is_404(client):
    assert client.get("/api/v1/areas/NOPE").status_code == 404


def test_deleting_an_area_detaches_its_zones(client):
    _map_office(client)
    assert client.delete("/api/v1/areas/OFFICE-01").status_code == 200
    assert client.get("/api/v1/areas/OFFICE-01").status_code == 404
    # The zones survive; they simply no longer belong to an area.
    zones = client.get("/api/v1/zones?camera_id=CAM-04").json()["zones"]
    assert zones and not zones[0].get("physical_area_id")


# --- backward compatibility ------------------------------------------------

def test_zone_state_without_occupants_is_still_accepted(client):
    """An older worker that reports no identities must keep working."""
    payload = _state("CAM-01", "ZONE-01", [], NOW, occupancy=3)
    payload.pop("occupants")
    r = client.post("/api/v1/zones/state", json=payload)
    assert r.status_code == 202, r.text

    zones = client.get("/api/v1/zones/state").json()["zones"]
    row = next(z for z in zones if z["camera_id"] == "CAM-01")
    assert row["occupancy"] == 3           # unchanged single-camera behaviour


def test_unmapped_zone_never_appears_as_an_area(client):
    client.post("/api/v1/zones/state",
                json=_state("CAM-09", "ZONE-01", ["gp_x"], NOW))
    assert client.get("/api/v1/areas/state").json()["areas"] == []


def test_occupants_must_be_a_list_of_strings(client):
    bad = _state("CAM-01", "ZONE-01", [], NOW)
    bad["occupants"] = [1, 2]
    r = client.post("/api/v1/zones/state", json=bad)
    assert r.status_code == 422
    assert any("occupants" in e for e in r.json()["errors"])


def test_single_camera_area_matches_its_zone_count(client):
    """One camera, one area: the area total equals the camera's own count."""
    client.post("/api/v1/areas", json={"area_id": "LOBBY", "capacity_max": 4})
    client.post("/api/v1/zones", json={"camera_id": "CAM-01", "zones": [{
        "zone_id": "ZONE-01", "zone_name": "Lobby", "zone_type": "MONITORED",
        "physical_area_id": "LOBBY",
        "polygon": [[0, 0], [1, 0], [1, 1], [0, 1]]}]})
    client.post("/api/v1/zones/state",
                json=_state("CAM-01", "ZONE-01", ["a", "b", "c"], NOW))

    st = client.get("/api/v1/areas/LOBBY").json()
    assert st["occupancy"] == 3
    assert st["double_counted"] == 0
    assert st["capacity_pct"] == 75.0
