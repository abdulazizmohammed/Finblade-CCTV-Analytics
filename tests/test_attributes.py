"""Appearance attributes: vocabulary, voting, sampling policy, storage, search.

  * finblade/attributes.py   pure — no model: a fake scorer returns fixed
                             probability tables, so the policy is what is tested
  * store contract           person_sightings on both backends
  * IngestService            PERSON_ATTRIBUTES events land as sightings; search
                             groups by person; every search is audited
  * HTTP routes              search needs the full key when auth is on

The line held throughout: a tag is a description from a fixed vocabulary,
never who someone is. The vocabulary refuses gender/age/ethnicity by name.
"""

import os
import sys
import time
import unittest

os.environ.setdefault("FINBLADE_INMEMORY", "1")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from finblade import attributes as A
from finblade.events import PERSON_ATTRIBUTES, new_event, validate_event
from services.api.service import IngestService
from services.api.store import InMemoryStore
from tests import pgfixture

T0 = 1_800_000_000.0


def dist(**top):
    """A scorer answer: for each attribute, the named label gets `p`, the rest
    share the remainder."""
    out = {}
    vocab = A.Vocabulary()
    for attr in vocab.names():
        labels = vocab.labels(attr)
        want, p = top.get(attr, (labels[0], 0.9))
        rest = (1.0 - p) / max(1, len(labels) - 1)
        out[attr] = {lab: (p if lab == want else rest) for lab in labels}
    return out


class FakeScorer:
    def __init__(self, answers):
        self.answers = list(answers)      # one dist per call, in order
        self.calls = []

    def __call__(self, frame, boxes):
        self.calls.append(list(boxes))
        return [self.answers.pop(0) if self.answers else dist() for _ in boxes]


class NoGate:
    def check(self, box, conf, w, h):
        return (box[3] - box[1]) >= 96, "short"


# ------------------------------------------------------------- vocabulary --
class TestVocabulary(unittest.TestCase):
    def test_default_has_the_six_attributes_and_prompts(self):
        v = A.Vocabulary()
        self.assertEqual(["upper_colour", "lower_colour", "headwear", "mask", "bag", "outerwear"], v.names())
        self.assertIn("blue", v.labels("upper_colour"))
        self.assertTrue(all(p[0].startswith("a photo of a person") for p in v.prompts("headwear")))
        # an ensemble: "no mask" carries several phrasings, one list per label
        self.assertEqual(["no", "yes"], v.labels("mask"))
        self.assertGreater(len(v.prompts("mask")[0]), 1)
        self.assertEqual([["a clipboard"]], A.Vocabulary({"item": {"clipboard": "a clipboard", "none": "nothing"}}).prompts("item")[:1])

    def test_refuses_profiling_attributes_whatever_the_config_says(self):
        for bad in ("gender", "age_group", "Ethnicity", "skin_tone"):
            with self.assertRaises(ValueError):
                A.Vocabulary({bad: {"a": "x", "b": "y"}, "headwear": {"cap": "c", "none": "n"}})
        with self.assertRaises(ValueError):
            A.Vocabulary({"headwear": {"cap": "only one"}})

    def test_each_attribute_is_judged_on_its_own_region(self):
        v = A.Vocabulary()
        self.assertEqual((0.0, 0.28), v.region("headwear"))
        self.assertEqual((0.5, 1.0), v.region("lower_colour"))
        self.assertLess(v.region("upper_colour")[1], v.region("lower_colour")[1], "top above bottoms")
        v2 = A.Vocabulary.from_config({"regions": {"bag": [0.2, 0.8]}})
        self.assertEqual((0.2, 0.8), v2.region("bag"))
        with self.assertRaises(ValueError):
            A.Vocabulary(regions={"bag": (0.9, 0.2)})

    def test_config_override_replaces_wholesale(self):
        v = A.Vocabulary.from_config({"vocabulary": {"item": {"clipboard": "a photo of a person holding a clipboard",
                                                               "none": "a photo of a person holding nothing"}}})
        self.assertEqual(["item"], v.names())
        self.assertEqual(A.Vocabulary().names(), A.Vocabulary.from_config({}).names())

    def test_disable_drops_an_attribute_from_judgement(self):
        # The review sheet showed mask confidently wrong on one site's footage;
        # a site switches it off and the rest carries on unchanged.
        v = A.Vocabulary.from_config({"disable": ["mask"]})
        self.assertEqual(["upper_colour", "lower_colour", "headwear", "bag", "outerwear"], v.names())
        self.assertEqual(v.names(), A.Vocabulary.from_config({"disable": "mask"}).names())
        with self.assertRaises(ValueError):
            A.Vocabulary.from_config({"disable": A.Vocabulary().names()})
        # ingest stores the unjudged attribute as "unknown", not null
        svc = IngestService(InMemoryStore())
        e = new_event(PERSON_ATTRIBUTES, "CAM-1", "RUH-01", T0, person_ref="pr_" + "e" * 16,
                      attributes={"upper_colour": "blue"}, confidences={"upper_colour": 0.9}, samples=3,
                      description="blue top")
        self.assertEqual(202, svc.ingest_event(e)[0])
        row = svc.store.search_sightings(T0 - 1, T0 + 1)[0]
        self.assertEqual("unknown", row["mask"])
        self.assertEqual(1, len(svc.store.search_sightings(T0 - 1, T0 + 1, filters={"mask": "unknown"})))

    def test_describe_reads_like_a_sentence_and_omits_unknowns(self):
        self.assertEqual("lab coat, blue top, black bottoms, cap, face mask, backpack",
                         A.describe({"outerwear": "lab coat", "upper_colour": "blue", "lower_colour": "black",
                                     "headwear": "cap", "mask": "yes", "bag": "backpack"}))
        self.assertEqual("blue top", A.describe({"upper_colour": "blue", "lower_colour": "unknown",
                                                 "headwear": "none", "mask": "no", "bag": "none"}))
        self.assertEqual("no attributes determined", A.describe({"upper_colour": "unknown"}))


# ----------------------------------------------------------------- voting --
class TestVote(unittest.TestCase):
    def test_majority_wins_and_confidence_is_the_winners_mean(self):
        s = [dist(upper_colour=("blue", 0.8)), dist(upper_colour=("blue", 0.6)), dist(upper_colour=("grey", 0.9))]
        labels, conf = A.vote(s, ["upper_colour"], min_confidence=0.45)
        self.assertEqual("blue", labels["upper_colour"])
        # mean of blue's probability over all three samples
        self.assertAlmostEqual((0.8 + 0.6 + (0.1 / 11)) / 3, conf["upper_colour"], places=2)

    def test_low_confidence_is_unknown_not_a_guess(self):
        s = [dist(upper_colour=("blue", 0.3)), dist(upper_colour=("blue", 0.35))]
        labels, conf = A.vote(s, ["upper_colour"], min_confidence=0.45)
        self.assertEqual("unknown", labels["upper_colour"])
        self.assertGreater(conf["upper_colour"], 0)

    def test_tie_breaks_on_probability_and_missing_attr_is_unknown(self):
        s = [dist(mask=("yes", 0.7)), dist(mask=("no", 0.9))]
        labels, _ = A.vote(s, ["mask"], 0.45)
        self.assertEqual("no", labels["mask"])
        labels, conf = A.vote([{"mask": {"yes": 0.9, "no": 0.1}}], ["mask", "bag"], 0.45)
        self.assertEqual(("yes", "unknown"), (labels["mask"], labels["bag"]))
        self.assertEqual(0.0, conf["bag"])

    def test_per_attribute_floor(self):
        # A single 0.45 floor can never bite on a two-label attribute: mask's
        # winner is always >= 0.5. With {mask: 0.85} a 0.7 mask is unknown
        # while a 0.7 colour (a clear winner among 12) still stands.
        s = [{"mask": {"yes": 0.7, "no": 0.3}, "upper_colour": dist(upper_colour=("blue", 0.7))["upper_colour"]}]
        labels, conf = A.vote(s, ["mask", "upper_colour"], {"default": 0.45, "mask": 0.85})
        self.assertEqual(("unknown", "blue"), (labels["mask"], labels["upper_colour"]))
        self.assertEqual(0.7, conf["mask"])
        labels, _ = A.vote([{"mask": {"yes": 0.9, "no": 0.1}}], ["mask"], {"default": 0.45, "mask": 0.85})
        self.assertEqual("yes", labels["mask"])
        # a bare float still applies to everything; a map without default uses 0.45
        self.assertEqual({"mask": 0.85, "bag": 0.45}, A.confidence_floors({"mask": 0.85}, ["mask", "bag"]))
        self.assertEqual({"mask": 0.6, "bag": 0.6}, A.confidence_floors(0.6, ["mask", "bag"]))
        # and the sampler carries the map through to its votes
        sam = A.AttributeSampler(A.Vocabulary(A.DEFAULT_VOCABULARY), lambda f, b: [{"mask": {"yes": 0.7, "no": 0.3}}],
                                 gate=None, samples=1, stable_age_s=0, min_confidence={"default": 0.45, "mask": 0.85})
        tags = sam.observe(None, [(1, 0, 0, 50, 200)], {1: 0.9}, now=10.0, frame_w=640, frame_h=480)
        self.assertEqual("unknown", tags[0].attributes["mask"])


# --------------------------------------------------------------- sampling --
class TestSampler(unittest.TestCase):
    def track(self, tid=1, h=200):
        return (tid, 100.0, 100.0, 160.0, 100.0 + h)

    def test_scores_after_stable_age_at_intervals_and_emits_once(self):
        sc = FakeScorer([dist(upper_colour=("blue", 0.9)), dist(upper_colour=("blue", 0.8)),
                         dist(upper_colour=("grey", 0.6))])
        s = A.AttributeSampler(A.Vocabulary(), sc, gate=NoGate(), samples=3, sample_interval_s=3.0,
                               stable_age_s=2.0, crop_fn=lambda f, b: ("crop", b))
        out = []
        for t in (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 9.0):
            out += s.observe("frame", [self.track()], {1: 0.9}, T0 + t, 1920, 1080)
        self.assertEqual([T0 + 2.0, T0 + 5.0, T0 + 8.0], [T0 + t for t in (2.0, 5.0, 8.0)])
        self.assertEqual(3, len(sc.calls), "three samples, not one per frame")
        self.assertEqual(1, len(out))
        tag = out[0]
        self.assertEqual("blue", tag.attributes["upper_colour"])
        self.assertEqual(3, tag.samples)
        self.assertEqual(("crop", (100.0, 100.0, 160.0, 300.0)), tag.crop)
        self.assertEqual(0, len(s.observe("frame", [self.track()], {1: 0.9}, T0 + 20, 1920, 1080)), "once")
        self.assertIsNone(s.drop(1, T0 + 30), "already emitted")

    def test_gate_and_budget(self):
        sc = FakeScorer([])
        s = A.AttributeSampler(A.Vocabulary(), sc, gate=NoGate(), samples=1, stable_age_s=0.0,
                               budget_per_frame=2)
        tracks = [self.track(1), self.track(2), self.track(3), self.track(4, h=40)]   # #4 too short
        out = s.observe("frame", tracks, {}, T0, 1920, 1080)
        self.assertEqual(2, len(out), "budget of two per frame")
        out = s.observe("frame", tracks, {}, T0 + 1, 1920, 1080)
        self.assertEqual(1, len(out), "the third; the short one never")
        self.assertEqual(1, s.stats["gated_out"] >= 1 and 1)

    def test_drop_emits_a_partial_tag_or_nothing(self):
        sc = FakeScorer([dist(headwear=("cap", 0.95))])
        s = A.AttributeSampler(A.Vocabulary(), sc, gate=NoGate(), samples=3, stable_age_s=0.0)
        self.assertEqual([], s.observe("frame", [self.track(7)], {}, T0, 1920, 1080))
        tag = s.drop(7, T0 + 4)
        self.assertEqual(("cap", 1), (tag.attributes["headwear"], tag.samples))
        self.assertIsNone(s.drop(99, T0), "never seen")
        s.observe("frame", [self.track(8)], {}, T0, 1920, 1080)   # seen, not yet due (interval)
        s2 = A.AttributeSampler(A.Vocabulary(), sc, gate=NoGate(), samples=3, stable_age_s=5.0)
        s2.observe("frame", [self.track(9)], {}, T0, 1920, 1080)
        self.assertIsNone(s2.drop(9, T0 + 1), "no sample yet -> no row")


# ---------------------------------------------------------------- events --
class TestEvent(unittest.TestCase):
    def test_schema_accepts_a_tag_event_and_rejects_a_bad_one(self):
        e = new_event(PERSON_ATTRIBUTES, "CAM-1", "RUH-01", T0, person_ref="pr_" + "a" * 16,
                      attributes={"upper_colour": "blue"}, confidences={"upper_colour": 0.8},
                      samples=3, description="blue top", track_id=4)
        ok, errs = validate_event(e)
        self.assertTrue(ok, errs)
        bad = dict(e); bad["confidences"] = 0.8
        self.assertFalse(validate_event(bad)[0])


# ---------------------------------------------------------- store contract
def sighting(**kw):
    s = {"event_id": kw.pop("event_id", "ev-1"), "ts": T0, "site_id": "RUH-01", "camera_id": "CAM-1",
         "zone_id": "LOBBY", "person_ref": "pr_x", "global_ref": "gp_1", "upper_colour": "blue",
         "lower_colour": "black", "headwear": "cap", "mask": "no", "bag": "backpack", "outerwear": "none",
         "extra": {}, "confidences": {"upper_colour": 0.8}, "samples": 3, "description": "blue top",
         "frame": "/bookmarks/attr_1.jpg"}
    s.update(kw)
    return s


class SightingContract:
    def make_store(self):
        raise NotImplementedError

    def setUp(self):
        self.store = self.make_store()

    def test_save_search_dedupe_and_order(self):
        self.store.save_sighting(sighting())
        self.store.save_sighting(sighting())                                   # replay
        self.store.save_sighting(sighting(event_id="ev-2", ts=T0 + 60, upper_colour="grey", global_ref="gp_2"))
        self.store.save_sighting(sighting(event_id="ev-3", ts=T0 + 120, site_id="JED-01", camera_id="CAM-9",
                                          global_ref=None))
        self.assertEqual(3, len(self.store.search_sightings(T0 - 1, T0 + 999)))
        hits = self.store.search_sightings(T0 - 1, T0 + 999, filters={"upper_colour": "blue"})
        self.assertEqual(["ev-3", "ev-1"], [h["event_id"] for h in hits], "newest first")
        self.assertEqual(["ev-1"], [h["event_id"] for h in self.store.search_sightings(
            T0 - 1, T0 + 999, filters={"upper_colour": "blue", "headwear": "cap"}, site_ids={"RUH-01"})])
        self.assertEqual([], self.store.search_sightings(T0 - 1, T0 + 999, site_ids=set()))
        self.assertEqual(["ev-2"], [h["event_id"] for h in self.store.search_sightings(T0 + 30, T0 + 90)])
        self.assertEqual({"upper_colour": 0.8}, hits[0]["confidences"])
        self.assertEqual(["ev-1"], [h["event_id"] for h in self.store.sightings_of("gp_1", T0 - 1, T0 + 999)])

    def test_a_filter_may_list_acceptable_labels(self):
        self.store.save_sighting(sighting())                                              # blue
        self.store.save_sighting(sighting(event_id="ev-2", ts=T0 + 60, upper_colour="grey", global_ref="gp_2"))
        self.store.save_sighting(sighting(event_id="ev-3", ts=T0 + 120, upper_colour="white", global_ref="gp_3"))
        hits = self.store.search_sightings(T0 - 1, T0 + 999, filters={"upper_colour": ("white", "grey")})
        self.assertEqual(["ev-3", "ev-2"], [h["event_id"] for h in hits])
        self.store.save_sighting(sighting(event_id="ev-4", ts=T0 + 180, extra={"item": "clipboard"}))
        self.assertEqual(["ev-4"], [h["event_id"] for h in self.store.search_sightings(
            T0 - 1, T0 + 999, filters={"item": ["clipboard", "folder"]})])

    def test_extra_attributes_and_audit(self):
        self.store.save_sighting(sighting(extra={"item": "clipboard"}))
        self.assertEqual(1, len(self.store.search_sightings(T0 - 1, T0 + 1, filters={"item": "clipboard"})))
        self.assertEqual(0, len(self.store.search_sightings(T0 - 1, T0 + 1, filters={"item": "phone"})))
        self.store.record_search("full", {"upper_colour": "blue"}, 1, T0)
        a = self.store.list_search_audit()
        self.assertEqual(("full", 1, {"upper_colour": "blue"}), (a[0]["actor"], int(a[0]["hits"]), a[0]["query"]))

    def test_correct_sighting(self):
        self.store.save_sighting(sighting(mask="yes", extra={"item": "clipboard"}))
        self.assertTrue(self.store.correct_sighting("ev-1", {"mask": "unknown", "item": "folder"}, "blue top, cap"))
        row = self.store.get_sighting("ev-1")
        self.assertEqual(("unknown", "blue top, cap", "folder"), (row["mask"], row["description"], row["extra"]["item"]))
        self.assertEqual(0, len(self.store.search_sightings(T0 - 1, T0 + 1, filters={"mask": "yes"})))
        self.assertFalse(self.store.correct_sighting("ev-nope", {"mask": "unknown"}, ""))

    def test_retention_prunes(self):
        self.store.save_sighting(sighting(ts=T0))
        self.store.save_sighting(sighting(event_id="ev-2", ts=T0 + 1000))
        self.assertEqual(1, self.store.delete_before(T0 + 500)["person_sightings"])


class TestInMemorySightings(SightingContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


@pgfixture.skip_without_pg
class TestPostgresSightings(SightingContract, unittest.TestCase):
    def make_store(self):
        store, teardown = pgfixture.make_store("attr")
        self.addCleanup(teardown)
        return store


# ---------------------------------------------------------------- service --
class TestService(unittest.TestCase):
    def setUp(self):
        self.svc = IngestService(InMemoryStore())

    def tag(self, cam, ts, gref=None, **attrs):
        base = {"upper_colour": "blue", "lower_colour": "black", "headwear": "cap", "mask": "no",
                "bag": "none", "outerwear": "none"}
        base.update(attrs)
        e = new_event(PERSON_ATTRIBUTES, cam, "RUH-01", ts, person_ref="pr_" + "a" * 16,
                      attributes=base, confidences={k: 0.8 for k in base}, samples=3,
                      description=A.describe(base), frame="/bookmarks/x.jpg", track_id=3)
        if gref:
            e["global_ref"] = gref
        code, body = self.svc.ingest_event(e)
        self.assertEqual(202, code, body)

    def test_events_become_sightings_grouped_by_person(self):
        self.tag("CAM-1", T0, gref="gp_9")
        self.tag("CAM-2", T0 + 300, gref="gp_9")
        self.tag("CAM-3", T0 + 600, gref=None)                     # same clothes, unresolved
        self.tag("CAM-1", T0 + 900, gref="gp_5", upper_colour="grey")
        r = self.svc.find_people({"upper_colour": "blue", "headwear": "cap"}, T0 - 1, T0 + 9999, actor="test")
        self.assertEqual(2, r["count"], "gp_9 twice = one person; the unresolved one stays separate")
        self.assertEqual(3, r["sightings"])
        gp9 = next(p for p in r["people"] if p["person"] == "gp_9")
        self.assertEqual(["CAM-1", "CAM-2"], gp9["cameras"])
        self.assertEqual((T0, T0 + 300), (gp9["first_seen"], gp9["last_seen"]))
        self.assertTrue(gp9["resolved"])
        other = next(p for p in r["people"] if p["person"] != "gp_9")
        self.assertFalse(other["resolved"])
        self.assertEqual(1, len(self.svc.store.list_search_audit()))
        self.assertEqual("test", self.svc.store.list_search_audit()[0]["actor"])
        self.assertEqual(0, self.svc.find_people({"upper_colour": "blue"}, T0 - 1, T0 + 9999,
                                                  site_ids={"JED-01"})["count"])

    def test_near_colours_rank_behind_exact_and_are_labelled(self):
        # The Wareed instance stored a white shirt as "grey" (0.55); an exact
        # search for white returned nothing. Near colours make it a maybe.
        self.tag("CAM-1", T0, gref="gp_grey", upper_colour="grey")
        self.tag("CAM-1", T0 + 100, gref="gp_white", upper_colour="white")
        self.tag("CAM-1", T0 + 200, gref="gp_red", upper_colour="red")
        r = self.svc.find_people({"upper_colour": "white"}, T0 - 1, T0 + 9999)
        self.assertEqual(["gp_white", "gp_grey"], [p["person"] for p in r["people"]], "exact first")
        self.assertEqual(["exact", "near"], [p["match"] for p in r["people"]])
        self.assertEqual("near", r["people"][1]["sightings"][0]["match"])
        self.assertEqual(1, r["exact"])
        self.assertEqual({"upper_colour": ["grey", "beige"]}, r["near_colours"])
        exact = self.svc.find_people({"upper_colour": "white"}, T0 - 1, T0 + 9999, near=False)
        self.assertEqual(["gp_white"], [p["person"] for p in exact["people"]])
        self.assertEqual({}, exact["near_colours"])
        # Non-colour attributes never widen: a cap is not a helmet.
        self.tag("CAM-1", T0 + 300, gref="gp_helmet", headwear="helmet")
        self.assertEqual(0, self.svc.find_people({"headwear": "hat"}, T0 - 1, T0 + 9999)["count"])
        # A near hit on one attribute is near for the person even when the
        # others are exact.
        r = self.svc.find_people({"upper_colour": "white", "headwear": "cap"}, T0 - 1, T0 + 9999)
        self.assertEqual({"gp_white": "exact", "gp_grey": "near"}, {p["person"]: p["match"] for p in r["people"]})
        self.assertTrue(self.svc.store.list_search_audit()[-1]["query"]["near"])

    def test_human_correction_is_audited_and_bounded(self):
        self.tag("CAM-1", T0, gref="gp_9", mask="yes")
        sid = self.svc.store.search_sightings(T0 - 1, T0 + 1)[0]["event_id"]
        code, out = self.svc.correct_sighting(sid, "mask", "unknown", actor="A. Reviewer")
        self.assertEqual(200, code, out)
        self.assertEqual(("yes", "unknown"), (out["from"], out["to"]))
        self.assertNotIn("mask", out["description"])
        row = self.svc.store.get_sighting(sid)
        self.assertEqual("unknown", row["mask"])
        self.assertEqual(0.8, row["confidences"]["mask"], "what the model thought stays on record")
        audit = self.svc.store.list_search_audit()[-1]
        self.assertEqual(("A. Reviewer", sid, "yes", "unknown"),
                         (audit["actor"], audit["query"]["correction"], audit["query"]["from"], audit["query"]["to"]))
        # a real label must be in the vocabulary; forbidden attributes never
        self.assertEqual(422, self.svc.correct_sighting(sid, "headwear", "fedora")[0])
        self.assertEqual(200, self.svc.correct_sighting(sid, "headwear", "hat")[0])
        self.assertEqual(422, self.svc.correct_sighting(sid, "gender", "unknown")[0])
        self.assertEqual(404, self.svc.correct_sighting("ev-nope", "mask", "unknown")[0])

    def test_no_identity_ever_enters_a_sighting(self):
        self.tag("CAM-1", T0, gref="gp_9")
        row = self.svc.store.search_sightings(T0 - 1, T0 + 1)[0]
        for k in row:
            self.assertNotIn(k, ("name", "gender", "age", "face"))


# ------------------------------------------------------------- HTTP routes --
try:
    from fastapi.testclient import TestClient
    from services.api.app import app, svc as app_svc
    from services.api import auth as _auth
    HAVE_APP = True
except Exception:                              # noqa: BLE001
    HAVE_APP = False


@unittest.skipUnless(HAVE_APP, "fastapi app not importable")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        app_svc.store._sightings.clear()

    def test_vocabulary_search_timeline_audit(self):
        v = self.c.get("/api/v1/search/vocabulary").json()["attributes"]
        self.assertIn("cap", v["headwear"])
        e = new_event(PERSON_ATTRIBUTES, "CAM-R1", "RUH-01", time.time() - 60, person_ref="pr_" + "b" * 16,
                      attributes={"upper_colour": "red", "headwear": "none", "mask": "yes"},
                      confidences={"upper_colour": 0.9}, samples=2, description="red top, face mask")
        e["global_ref"] = "gp_route"
        self.assertEqual(202, self.c.post("/api/v1/events/ingest", json=e).status_code)
        r = self.c.get("/api/v1/search/people?upper_colour=red&mask=yes&hours=1").json()
        self.assertEqual(1, r["count"])
        self.assertEqual("red top, face mask", r["people"][0]["description"])
        self.assertEqual(0, self.c.get("/api/v1/search/people?upper_colour=blue&hours=1").json()["count"])
        # pink is a near colour of red: found by default, not with near=0
        self.assertEqual("near", self.c.get("/api/v1/search/people?upper_colour=pink&hours=1").json()["people"][0]["match"])
        self.assertEqual(0, self.c.get("/api/v1/search/people?upper_colour=pink&hours=1&near=0").json()["count"])
        # no attribute at all = everyone tagged in the window
        self.assertEqual(1, self.c.get("/api/v1/search/people?hours=1").json()["count"])
        t = self.c.get("/api/v1/search/people/gp_route?hours=1").json()
        self.assertEqual(1, t["count"])
        a = self.c.get("/api/v1/search/audit").json()["searches"]
        self.assertGreaterEqual(len(a), 3)
        # a human rejects the mask tag on that sighting
        sid = t["sightings"][0]["sighting_id"]
        r = self.c.post(f"/api/v1/search/sightings/{sid}/correct", json={"attribute": "mask", "value": "unknown", "by": "ops"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual(0, self.c.get("/api/v1/search/people?mask=yes&hours=1").json()["count"])
        self.assertEqual(422, self.c.post(f"/api/v1/search/sightings/{sid}/correct", json={}).status_code)
        self.assertEqual(404, self.c.post("/api/v1/search/sightings/nope/correct", json={"attribute": "mask"}).status_code)

    def test_search_needs_the_full_key_when_auth_is_on(self):
        old = dict(os.environ)
        os.environ["FINBLADE_API_KEY"] = "full-key-full-key"
        os.environ["FINBLADE_INTEGRATION_KEY"] = "integ-key-integ-key"
        try:
            self.assertEqual(403, self.c.get("/api/v1/search/people?upper_colour=red",
                                             headers={"Authorization": "Bearer integ-key-integ-key"}).status_code)
            self.assertEqual(200, self.c.get("/api/v1/search/people?upper_colour=red",
                                             headers={"Authorization": "Bearer full-key-full-key"}).status_code)
            # Through the MCP layer with only the integration key, the tool
            # error must say what the HOST is missing — the Wareed chatbot
            # reported it as "this connection lacks the permission", and the
            # operator went looking at the wrong end.
            from services.mcp.server import TestClientBackend, build_server
            import asyncio
            srv = build_server(TestClientBackend(self.c, api_key="integ-key-integ-key"))
            with self.assertRaises(Exception) as cm:
                asyncio.run(srv.call_tool("find_people", {"upper_colour": "red", "hours": 1}))
            self.assertIn("FINBLADE_MCP_SEARCH_KEY", str(cm.exception))
            self.assertIn("403", str(cm.exception))
        finally:
            os.environ.clear(); os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
