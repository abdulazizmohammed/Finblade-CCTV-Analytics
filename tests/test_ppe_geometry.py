"""Item -> person association (cases A-F of the Phase 3 spec).

Pure geometry, pure stdlib, no model. Coordinates are chosen so each case fails
for exactly one reason — if a test breaks, the reason it broke is the thing it
names.

Person boxes here are 100 wide x 300 tall, so the anatomical bands land at:
    hardhat      y -24 .. +120  (top -8% to 40% of height)
    mask         y -15 .. +96
    safety_vest  y +45 .. +210
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from finblade.geometry import (ANATOMY, anatomical_region, associate_item,
                               associate_items, containment)

# Two people, shoulder to shoulder but not overlapping.
ALICE = (100.0, 0.0, 200.0, 300.0)
BOB = (260.0, 0.0, 360.0, 300.0)
PEOPLE = {"alice": ALICE, "bob": BOB}


class TestRegions(unittest.TestCase):
    def test_hat_band_is_the_top_of_the_body(self):
        x1, y1, x2, y2 = anatomical_region(ALICE, "hardhat")
        self.assertEqual((x1, x2), (100.0, 200.0))
        self.assertLess(y1, 0.0, "band should allow a hat above the bbox top")
        self.assertAlmostEqual(y2, 120.0)

    def test_vest_band_is_the_torso_not_the_head(self):
        _, y1, _, y2 = anatomical_region(ALICE, "safety_vest")
        self.assertAlmostEqual(y1, 45.0)
        self.assertAlmostEqual(y2, 210.0)
        hat_top = anatomical_region(ALICE, "hardhat")[1]
        self.assertGreater(y1, hat_top,
                           "a white hardhat must not be able to satisfy a vest")

    def test_mask_band_is_higher_than_the_vest(self):
        self.assertLess(ANATOMY["mask"][1], ANATOMY["safety_vest"][1])

    def test_unknown_item_falls_back_to_the_whole_body(self):
        self.assertEqual(anatomical_region(ALICE, "banana"), ALICE)

    def test_containment_is_asymmetric_unlike_iou(self):
        """The reason IoU is not used: a small item fully inside a large region
        is 1.0 here and near zero under IoU."""
        small = (120.0, 10.0, 140.0, 30.0)
        self.assertAlmostEqual(containment(small, ALICE), 1.0)
        self.assertLess(containment(ALICE, small), 0.05)


class TestAssociation(unittest.TestCase):
    # --- A, B, C: the right item on the right worker --------------------
    def test_A_hardhat_associates_with_the_worker_wearing_it(self):
        hat = (120.0, 5.0, 180.0, 45.0)          # on Alice's head
        key, score = associate_item(hat, "hardhat", PEOPLE)
        self.assertEqual(key, "alice")
        self.assertGreater(score, 0.9)

    def test_B_vest_associates_with_the_worker_wearing_it(self):
        vest = (270.0, 90.0, 350.0, 180.0)       # on Bob's torso
        key, _ = associate_item(vest, "safety_vest", PEOPLE)
        self.assertEqual(key, "bob")

    def test_C_mask_associates_with_the_worker_wearing_it(self):
        mask = (130.0, 20.0, 165.0, 50.0)        # on Alice's face
        key, _ = associate_item(mask, "mask", PEOPLE)
        self.assertEqual(key, "alice")

    # --- D: the case that must REFUSE ------------------------------------
    def test_D_item_between_two_workers_is_not_assigned_to_either(self):
        """Geometry cannot arbitrate a hat halfway between two heads, and
        guessing would accuse whichever person happened to sort first."""
        between = (215.0, 5.0, 265.0, 45.0)      # gap between Alice and Bob
        key, _ = associate_item(between, "hardhat", PEOPLE)
        self.assertIsNone(key)

    def test_D2_overlapping_people_with_an_ambiguous_hat_refuse(self):
        overlapping = {"a": (100.0, 0.0, 200.0, 300.0),
                       "b": (140.0, 0.0, 240.0, 300.0)}
        hat = (150.0, 5.0, 190.0, 45.0)          # inside BOTH head regions
        key, _ = associate_item(hat, "hardhat", overlapping)
        self.assertIsNone(key, "an ambiguous hat must not be assigned")

    # --- E: outside anyone -----------------------------------------------
    def test_E_item_outside_every_person_is_rejected(self):
        shelf = (600.0, 400.0, 660.0, 440.0)
        key, score = associate_item(shelf, "hardhat", PEOPLE)
        self.assertIsNone(key)
        self.assertEqual(score, 0.0)

    def test_E2_hat_at_the_right_x_but_the_wrong_height_is_rejected(self):
        """A hardhat down at knee level is not on that person's head - this is
        the case plain person-box IoU would wrongly accept."""
        knees = (120.0, 240.0, 180.0, 280.0)
        key, _ = associate_item(knees, "hardhat", PEOPLE)
        self.assertIsNone(key)

    def test_E3_no_people_at_all(self):
        self.assertEqual(associate_item((0.0, 0.0, 10.0, 10.0), "hardhat", {}),
                         (None, 0.0))

    # --- F: several items at once ----------------------------------------
    def test_F_multiple_items_associate_independently(self):
        items = [((120.0, 5.0, 180.0, 45.0), "hardhat"),      # Alice hat
                 ((280.0, 5.0, 340.0, 45.0), "hardhat"),      # Bob hat
                 ((110.0, 90.0, 190.0, 180.0), "safety_vest")]  # Alice vest
        got = associate_items(items, PEOPLE)
        self.assertEqual([k for _, k, _ in got], ["alice", "bob", "alice"])

    def test_F2_one_item_never_belongs_to_two_people(self):
        """Structural guarantee: associate_item returns a single key, so the
        'one hat, two owners' error is not expressible."""
        hat = (120.0, 5.0, 180.0, 45.0)
        key, _ = associate_item(hat, "hardhat", PEOPLE)
        self.assertIn(key, ("alice", None))
        self.assertNotIsInstance(key, (list, tuple, set))

    def test_partial_containment_below_the_bar_is_refused(self):
        """Half in, half out is not good enough to convict on."""
        straddling = (180.0, 5.0, 260.0, 45.0)   # mostly in the gap
        key, _ = associate_item(straddling, "hardhat", PEOPLE)
        self.assertIsNone(key)

    def test_association_is_deterministic(self):
        items = [((120.0, 5.0, 180.0, 45.0), "hardhat")]
        first = associate_items(items, PEOPLE)
        for _ in range(20):
            self.assertEqual(associate_items(items, PEOPLE), first)


if __name__ == "__main__":
    unittest.main()
