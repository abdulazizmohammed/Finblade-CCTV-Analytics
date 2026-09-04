"""The live-feed tiles are sized to the cameras, not to a fixed two-up grid.

Both regressions guarded here are SILENT: nothing errors, no test fails, the
video is simply drawn smaller than it could be. That is why they get a test —
an operator squinting at a half-size feed has no way to tell it apart from a
camera that is just far away.

  * The grid was `1fr 1fr` unconditionally, so a site with one camera got a
    half-width tile with an empty column beside it — the smallest the video can
    be drawn, next to nothing at all.
  * The tile was `aspect-ratio:16/10` while the cameras are 1920x1080.
    object-fit:contain honours the video's own ratio, so every frame lost about
    a tenth of the tile's height to black bars.

Static CSS assertions rather than a rendered check: there is no browser here,
and the failure mode is a value being changed to something plausible, which a
string guard catches perfectly well.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO, "web", "dashboard.html")


def _style():
    with open(DASHBOARD, encoding="utf-8") as fh:
        src = fh.read()
    return src[src.index("<style>") + 7: src.index("</style>")]


def _rule(selector):
    """The declaration block for a selector, from the dashboard's own CSS."""
    style = _style()
    at = style.index(selector + "{")
    return style[at + len(selector) + 1: style.index("}", at)]


def test_a_single_camera_is_not_confined_to_half_the_panel():
    """auto-fit collapses the empty track, so one camera fills the width."""
    rule = _rule(".feeds")
    assert "auto-fit" in rule, \
        "the feeds grid is back to fixed columns; one camera will render half-size"
    assert "grid-template-columns:1fr 1fr" not in rule


def test_the_track_floor_cannot_exceed_the_container():
    """A bare minmax(360px,1fr) floor overflows the page sideways on a phone,
    because a grid track never shrinks below its own minimum. min(100%,360px)
    lets it collapse."""
    assert "minmax(min(100%," in _rule(".feeds")


def test_a_multi_camera_bank_still_sits_two_up():
    """The floor is what keeps several cameras side by side rather than in one
    tall column. At the panel widths this dashboard runs at, two 360px tracks
    fit and three do not — so the number is load-bearing, not decoration."""
    floor = re.search(r"minmax\(min\(100%,(\d+)px\)", _rule(".feeds"))
    assert floor, "the feeds grid has no track floor"
    assert 300 <= int(floor.group(1)) <= 420, (
        "a floor outside this range changes how many cameras sit side by side")


def test_the_tile_ratio_matches_the_camera_ratio():
    """16:9, because the cameras are 1920x1080 and contain never crops — a
    mismatched tile letterboxes instead, losing picture to black bars."""
    assert "aspect-ratio:16/9" in _rule(".feed")


def test_frames_are_contained_and_never_cropped():
    """object-fit:cover would fill the tile by cutting the edges off the scene.
    On a monitored feed the edges are where people enter, so this must stay
    contain even though it is what makes a non-16:9 camera letterbox."""
    assert "object-fit:contain" in _rule(".feed img")
