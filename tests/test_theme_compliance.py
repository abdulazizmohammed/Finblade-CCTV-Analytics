"""Static guards for the FinBlade brand rules (CLAUDE.md UI theme section).

These cannot check that the dashboard *looks* right (no eyes), but they enforce
the mechanical rules objectively: no hard-coded hex, theme imported, tabular
numerals used, corner-bracket motif present, and no accidental 'green' status.
"""

import os
import re
import unittest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DASH = os.path.join(_REPO, "web", "dashboard.html")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


class TestDashboardTheme(unittest.TestCase):
    def setUp(self):
        self.html = _read(DASH)

    def test_no_hardcoded_hex(self):
        # Any colour must come from theme variables, not literal hex in this file.
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b", self.html), [])

    def test_imports_theme(self):
        self.assertIn("finblade-theme.css", self.html)

    def test_uses_tabular_numerals(self):
        self.assertIn("fb-num", self.html)

    def test_uses_corner_bracket_motif(self):
        self.assertIn("fb-bracket", self.html)

    def test_restricted_uses_restricted_var_not_red(self):
        # Restricted severity must map to --fb-restricted (magenta), not critical.
        self.assertIn("--fb-restricted", self.html)
        self.assertRegex(self.html, r'data-sev="CRITICAL"[^{]*\{[^}]*--fb-restricted')

    def test_normal_has_no_green(self):
        # NORMAL is colourless (--fb-ok grey). No green words/vars anywhere.
        self.assertNotIn("green", self.html.lower())

    def test_no_external_font_fetch(self):
        # Air-gapped: no Google Fonts / CDN font imports.
        self.assertNotIn("fonts.googleapis", self.html)
        self.assertNotIn("@import", self.html)

    def test_reduced_motion_and_system_only(self):
        # Theme file owns prefers-reduced-motion; dashboard must not fetch remote assets.
        self.assertNotIn("https://", self.html.replace("http://${location", ""))


if __name__ == "__main__":
    unittest.main()
