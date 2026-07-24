import unittest

from finblade.identity import PersonRefHasher


class TestPersonRef(unittest.TestCase):
    def test_same_track_same_ref_within_session(self):
        h = PersonRefHasher(session_salt="fixed-salt")
        self.assertEqual(h.ref(7), h.ref(7))

    def test_different_tracks_differ(self):
        h = PersonRefHasher(session_salt="fixed-salt")
        self.assertNotEqual(h.ref(7), h.ref(8))

    def test_different_session_salt_changes_ref(self):
        a = PersonRefHasher(session_salt="salt-a")
        b = PersonRefHasher(session_salt="salt-b")
        self.assertNotEqual(a.ref(7), b.ref(7))

    def test_ref_contains_no_pii_and_is_opaque(self):
        h = PersonRefHasher(session_salt="fixed-salt")
        ref = h.ref(7)
        self.assertTrue(PersonRefHasher.looks_anonymous(ref))
        self.assertTrue(ref.startswith("pr_"))
        # No raw track id leaked into the output.
        self.assertNotIn("7", ref[3:])  # (hash body; salt makes this robust)

    def test_looks_anonymous_rejects_plaintext(self):
        self.assertFalse(PersonRefHasher.looks_anonymous("john.smith"))
        self.assertFalse(PersonRefHasher.looks_anonymous("pr_XYZ"))
        self.assertFalse(PersonRefHasher.looks_anonymous("pr_" + "a" * 15))

    def test_auto_salt_is_random(self):
        # Two default hashers should not collide on the same track id.
        self.assertNotEqual(PersonRefHasher().ref(1), PersonRefHasher().ref(1))


if __name__ == "__main__":
    unittest.main()
