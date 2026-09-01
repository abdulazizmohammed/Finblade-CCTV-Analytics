"""Part A: the source-agnostic observation schema.

Mirrors tests/test_events.py in shape — valid payload accepted, malformed
rejected, one test per rule — plus the two properties that are the point of the
schema rather than incidental to it: an uncalibrated camera can publish, and a
sensor with no visual channel cannot claim an appearance.
"""

import unittest

from finblade.globalid import GlobalIdentityRegistry
from finblade.observation import (
    APPEARANCE_CAPABLE, BICYCLE, CAMERA, FRAME_IMAGE, FRAME_SITE, LIDAR,
    PERSON, RADAR, UNKNOWN, VEHICLE, can_appearance_match,
    looks_anonymous_global_ref, new_observation, observation_from_bbox,
    validate_observation,
)


def cam_obs(**over):
    o = new_observation(CAMERA, "CAM-03", "SITE-DXB-01", 1000.0, 640.0, 712.0,
                        frame=FRAME_IMAGE, object_class=PERSON, confidence=0.87)
    o.update(over)
    return o


def radar_obs(**over):
    o = new_observation(RADAR, "RAD-01", "SITE-DXB-01", 1000.0, 12.5, 3.25,
                        frame=FRAME_SITE, object_class=PERSON, confidence=0.9)
    o.update(over)
    return o


class TestBuild(unittest.TestCase):
    def test_envelope(self):
        o = cam_obs()
        self.assertEqual(o["source_type"], CAMERA)
        self.assertEqual(o["source_id"], "CAM-03")
        self.assertIn("observation_id", o)
        self.assertEqual(o["position"]["frame"], FRAME_IMAGE)
        self.assertEqual(o["position"]["x"], 640.0)

    def test_observation_ids_are_unique(self):
        self.assertNotEqual(cam_obs()["observation_id"],
                            cam_obs()["observation_id"])

    def test_position_extras_land_in_position_block(self):
        o = new_observation(RADAR, "RAD-01", "S", 1.0, 1.0, 2.0,
                            frame=FRAME_SITE, accuracy_m=0.4, z=1.7)
        self.assertEqual(o["position"]["accuracy_m"], 0.4)
        self.assertEqual(o["position"]["z"], 1.7)
        self.assertNotIn("accuracy_m", o)

    def test_from_bbox_positions_on_the_foot_point(self):
        o = observation_from_bbox("CAM-03", "SITE-1", 1000.0,
                                  (100.0, 200.0, 300.0, 500.0), 0.8,
                                  local_track_id=42, zone_id="ZONE-01")
        # bottom-centre, matching finblade.geometry.foot_point
        self.assertEqual(o["position"]["x"], 200.0)
        self.assertEqual(o["position"]["y"], 500.0)
        self.assertEqual(o["bbox"], [100.0, 200.0, 300.0, 500.0])
        self.assertEqual(o["local_track_id"], 42)
        self.assertEqual(o["zone_id"], "ZONE-01")
        self.assertTrue(validate_observation(o)[0])

    def test_from_bbox_omits_absent_optionals(self):
        o = observation_from_bbox("CAM-03", "SITE-1", 1.0, (0, 0, 10, 20), 0.5)
        for key in ("local_track_id", "zone_id", "global_ref"):
            self.assertNotIn(key, o)
        self.assertTrue(validate_observation(o)[0])


class TestValidation(unittest.TestCase):
    def test_valid_camera_observation(self):
        ok, errs = validate_observation(cam_obs())
        self.assertTrue(ok, errs)

    def test_valid_radar_observation(self):
        ok, errs = validate_observation(radar_obs())
        self.assertTrue(ok, errs)

    def test_not_an_object_rejected(self):
        self.assertFalse(validate_observation("nope")[0])
        self.assertFalse(validate_observation(None)[0])

    def test_unknown_source_type_rejected(self):
        ok, errs = validate_observation(cam_obs(source_type="RADR"))
        self.assertFalse(ok)
        self.assertTrue(any("source_type" in e for e in errs))

    def test_unknown_object_class_rejected(self):
        ok, errs = validate_observation(cam_obs(object_class="Person"))
        self.assertFalse(ok)
        self.assertTrue(any("object_class" in e for e in errs))

    def test_every_declared_class_is_accepted(self):
        for oc in (PERSON, VEHICLE, BICYCLE, UNKNOWN):
            self.assertTrue(validate_observation(cam_obs(object_class=oc))[0], oc)

    def test_empty_source_id_rejected(self):
        ok, errs = validate_observation(cam_obs(source_id=""))
        self.assertFalse(ok)
        self.assertTrue(any("source_id" in e for e in errs))

    def test_missing_site_id_rejected(self):
        o = cam_obs()
        del o["site_id"]
        self.assertFalse(validate_observation(o)[0])

    def test_negative_ts_rejected(self):
        self.assertFalse(validate_observation(cam_obs(ts=-1.0))[0])

    def test_confidence_out_of_range_rejected(self):
        self.assertFalse(validate_observation(cam_obs(confidence=1.5))[0])
        self.assertFalse(validate_observation(cam_obs(confidence=-0.1))[0])

    def test_bool_not_accepted_as_number(self):
        # bool is a subclass of int; the same trap events.py guards against.
        self.assertFalse(validate_observation(cam_obs(confidence=True))[0])
        self.assertFalse(validate_observation(cam_obs(ts=True))[0])

    def test_nan_and_inf_positions_rejected(self):
        nan, inf = float("nan"), float("inf")
        for bad in (nan, inf, -inf):
            o = cam_obs()
            o["position"]["x"] = bad
            ok, errs = validate_observation(o)
            self.assertFalse(ok, bad)
            self.assertTrue(any("finite" in e for e in errs), errs)

    def test_missing_position_rejected(self):
        o = cam_obs()
        del o["position"]
        ok, errs = validate_observation(o)
        self.assertFalse(ok)
        self.assertTrue(any("position" in e for e in errs))

    def test_bad_frame_rejected(self):
        o = cam_obs()
        o["position"]["frame"] = "WORLD"
        ok, errs = validate_observation(o)
        self.assertFalse(ok)
        self.assertTrue(any("frame" in e for e in errs))


class TestFrameDiscipline(unittest.TestCase):
    """The IMAGE/SITE split is the whole point; mixing the two is refused."""

    def test_uncalibrated_camera_can_publish(self):
        # The property that lets Part A ship before Part B: pixel coordinates,
        # no calibration, still a valid observation.
        ok, errs = validate_observation(cam_obs())
        self.assertTrue(ok, errs)
        self.assertEqual(cam_obs()["position"]["frame"], FRAME_IMAGE)

    def test_accuracy_in_metres_rejected_on_pixel_position(self):
        o = cam_obs()
        o["position"]["accuracy_m"] = 0.5
        ok, errs = validate_observation(o)
        self.assertFalse(ok)
        self.assertTrue(any("accuracy_m" in e for e in errs))

    def test_accuracy_accepted_on_site_position(self):
        o = radar_obs()
        o["position"]["accuracy_m"] = 0.5
        self.assertTrue(validate_observation(o)[0])

    def test_negative_accuracy_rejected(self):
        o = radar_obs()
        o["position"]["accuracy_m"] = -0.1
        self.assertFalse(validate_observation(o)[0])

    def test_bbox_rejected_on_site_position(self):
        o = radar_obs(bbox=[1.0, 2.0, 3.0, 4.0])
        ok, errs = validate_observation(o)
        self.assertFalse(ok)
        self.assertTrue(any("bbox" in e for e in errs))

    def test_velocity_rejected_on_pixel_position(self):
        ok, errs = validate_observation(cam_obs(velocity={"vx": 1.0, "vy": 0.0}))
        self.assertFalse(ok)
        self.assertTrue(any("velocity" in e for e in errs))

    def test_velocity_accepted_on_site_position(self):
        ok, errs = validate_observation(
            radar_obs(velocity={"vx": 1.2, "vy": -0.4, "speed_mps": 1.26}))
        self.assertTrue(ok, errs)

    def test_velocity_missing_component_rejected(self):
        ok, errs = validate_observation(radar_obs(velocity={"vx": 1.0}))
        self.assertFalse(ok)
        self.assertTrue(any("vy" in e for e in errs))

    def test_negative_speed_rejected(self):
        self.assertFalse(validate_observation(
            radar_obs(velocity={"vx": 0.0, "vy": 0.0, "speed_mps": -1.0}))[0])


class TestBbox(unittest.TestCase):
    def test_wrong_length_rejected(self):
        self.assertFalse(validate_observation(cam_obs(bbox=[1.0, 2.0, 3.0]))[0])

    def test_inverted_bbox_rejected(self):
        ok, errs = validate_observation(cam_obs(bbox=[300.0, 200.0, 100.0, 500.0]))
        self.assertFalse(ok)
        self.assertTrue(any("x1 <= x2" in e for e in errs))

    def test_non_numeric_bbox_rejected(self):
        self.assertFalse(validate_observation(cam_obs(bbox=[1, 2, "3", 4]))[0])


class TestOptionalFields(unittest.TestCase):
    def test_negative_local_track_id_rejected(self):
        self.assertFalse(validate_observation(cam_obs(local_track_id=-1))[0])

    def test_bool_local_track_id_rejected(self):
        self.assertFalse(validate_observation(cam_obs(local_track_id=True))[0])

    def test_null_zone_id_allowed(self):
        self.assertTrue(validate_observation(cam_obs(zone_id=None))[0])

    def test_empty_zone_id_rejected(self):
        self.assertFalse(validate_observation(cam_obs(zone_id=""))[0])


class TestPrivacy(unittest.TestCase):
    def test_plain_name_as_global_ref_rejected(self):
        ok, errs = validate_observation(cam_obs(global_ref="john.smith"))
        self.assertFalse(ok)
        self.assertTrue(any("PII" in e or "anonymous" in e for e in errs))

    def test_person_ref_shape_rejected_as_global_ref(self):
        # pr_ refs are per-camera and per-session; a global_ref must be gp_.
        self.assertFalse(
            validate_observation(cam_obs(global_ref="pr_0123456789abcdef"))[0])

    def test_minted_registry_ref_is_accepted(self):
        # Guards the duplicated ref-shape rule in observation.py against drift
        # from GlobalIdentityRegistry._mint_ref, which is the authority.
        ref = GlobalIdentityRegistry()._mint_ref()
        self.assertTrue(looks_anonymous_global_ref(ref), ref)
        ok, errs = validate_observation(cam_obs(global_ref=ref))
        self.assertTrue(ok, errs)

    def test_signature_carrying_a_vector_rejected(self):
        for key in ("vector", "vectors", "embedding", "embeddings",
                    "features", "descriptor"):
            o = cam_obs(signature={"kind": "OSNET_X0_25", "dim": 512,
                                   key: [0.1] * 512})
            ok, errs = validate_observation(o)
            self.assertFalse(ok, key)
            self.assertTrue(any("appearance data" in e for e in errs), errs)

    def test_signature_metadata_alone_is_accepted(self):
        ok, errs = validate_observation(
            cam_obs(signature={"kind": "OSNET_X0_25", "dim": 512}))
        self.assertTrue(ok, errs)

    def test_signature_needs_a_kind(self):
        self.assertFalse(validate_observation(cam_obs(signature={"dim": 512}))[0])

    def test_signature_dim_must_be_positive(self):
        self.assertFalse(validate_observation(
            cam_obs(signature={"kind": "OSNET", "dim": 0}))[0])


class TestAppearanceCapability(unittest.TestCase):
    """A sensor with no visual channel must not claim a visual identity."""

    def test_camera_is_appearance_capable(self):
        self.assertTrue(can_appearance_match(cam_obs()))
        self.assertIn(CAMERA, APPEARANCE_CAPABLE)

    def test_radar_and_lidar_are_not(self):
        self.assertFalse(can_appearance_match(radar_obs()))
        self.assertNotIn(RADAR, APPEARANCE_CAPABLE)
        self.assertNotIn(LIDAR, APPEARANCE_CAPABLE)

    def test_radar_carrying_a_signature_rejected(self):
        ok, errs = validate_observation(
            radar_obs(signature={"kind": "OSNET_X0_25", "dim": 512}))
        self.assertFalse(ok)
        self.assertTrue(any("appearance channel" in e for e in errs), errs)

    def test_radar_without_a_signature_is_fine(self):
        self.assertTrue(validate_observation(radar_obs())[0])


if __name__ == "__main__":
    unittest.main()
