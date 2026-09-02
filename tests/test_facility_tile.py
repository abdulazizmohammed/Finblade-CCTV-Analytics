"""The dashboard's "In facility" KPI tile says only what the roster can support.

Facility occupancy is counted on door crossings, so the tile has branches that
matter more than the arithmetic: a site with no door zones cannot count at all,
a declared baseline has to stay visibly separate from what was observed, and
roster drift has to surface without being dressed up as a live incident.

These lift facilitySub/facilityWindow verbatim out of web/dashboard.html and run
them under node, so they exercise the shipped code rather than a copy that can
drift. Skipped when node is unavailable — this is UI behaviour, and a missing JS
runtime must not fail a Python test run.
"""

import json
import os
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO, "web", "dashboard.html")

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not installed")


def subtitle_source():
    """The subtitle block, verbatim from the dashboard."""
    with open(DASHBOARD, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("function facilityWindow(")
    end = src.index("function renderFacility(")
    return src[start:end]


def sub_for(state, tmp_path):
    script = tmp_path / "t.js"
    script.write_text(
        subtitle_source()
        + "\nconsole.log(facilitySub(%s));" % json.dumps(state),
        encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _state(**over):
    """A site with one door and nothing unusual about it."""
    base = {
        "occupancy": 4, "observed": 4, "baseline": 0, "stale": 0,
        "stale_after_s": 3600.0,
        "doors": [{"door_zone_id": "DOOR-01", "entries": 9, "exits": 5,
                   "entry_rate_per_min": 1.2, "exit_rate_per_min": 0.8}],
        "policy": {"doors": ["DOOR-01"], "outside": []},
    }
    base.update(over)
    return base


# --- a site that cannot count ---------------------------------------------

@node
def test_no_door_zones_says_so_rather_than_reporting_zero(tmp_path):
    """The whole point of the honest empty state.

    With no door zones nothing can ever be counted, so a confident subtitle
    under a "0" would read as "the building is empty" — which is a claim the
    system has no basis for. Say the count is unavailable instead.
    """
    sub = sub_for(_state(policy={"doors": [], "outside": []}, doors=[]),
                  tmp_path)
    assert sub == "no door zones configured — cannot be counted"


@node
def test_a_missing_policy_block_is_treated_as_no_doors(tmp_path):
    """An older or partial payload must not silently produce a flow rate."""
    assert sub_for({"occupancy": 0}, tmp_path) == \
        "no door zones configured — cannot be counted"


# --- flow ------------------------------------------------------------------

@node
def test_door_rates_are_summed_across_the_site(tmp_path):
    two = _state(doors=[
        {"door_zone_id": "DOOR-01", "entry_rate_per_min": 1.2,
         "exit_rate_per_min": 0.8},
        {"door_zone_id": "DOOR-02", "entry_rate_per_min": 0.5,
         "exit_rate_per_min": 2.0}],
        policy={"doors": ["DOOR-01", "DOOR-02"], "outside": []})
    assert sub_for(two, tmp_path) == "↑1.7 ↓2.8 /min"


@node
def test_configured_doors_with_no_traffic_still_show_a_rate(tmp_path):
    """Zero flow through a real door is a fact; absent flow is not the same
    thing as an unconfigured site and must not borrow its message."""
    assert sub_for(_state(doors=[]), tmp_path) == "↑0.0 ↓0.0 /min"


# --- the baseline decomposition -------------------------------------------

@node
def test_baseline_is_shown_split_from_what_was_observed(tmp_path):
    """A declared opening count is not something the system saw. Merging the
    two into one figure would present a human's assertion as a measurement."""
    sub = sub_for(_state(occupancy=11, observed=4, baseline=7), tmp_path)
    assert "4 observed · 7 declared" in sub


@node
def test_no_baseline_means_no_split(tmp_path):
    """At zero the decomposition says nothing and only crowds the line."""
    assert "observed" not in sub_for(_state(baseline=0), tmp_path)


# --- drift -----------------------------------------------------------------

@node
def test_stale_entries_are_surfaced_in_amber_not_red(tmp_path):
    """Drift wants a human to look at the roster; it is not an incident on the
    floor. The .drift class is amber — see the rule in CLAUDE.md."""
    sub = sub_for(_state(stale=3), tmp_path)
    assert '<span class="drift">3 unseen &gt;1h</span>' in sub
    assert "critical" not in sub


@node
def test_no_drift_means_no_drift_line(tmp_path):
    assert "unseen" not in sub_for(_state(stale=0), tmp_path)


@node
def test_the_drift_window_is_rendered_from_the_servers_own_value(tmp_path):
    """The window is a query parameter, so the label must follow it rather than
    hard-coding the hour the endpoint happens to default to."""
    assert "1 unseen &gt;30m" in sub_for(
        _state(stale=1, stale_after_s=1800.0), tmp_path)
    assert "1 unseen &gt;2h" in sub_for(
        _state(stale=1, stale_after_s=7200.0), tmp_path)


@node
def test_everything_at_once_stays_on_one_composed_line(tmp_path):
    sub = sub_for(_state(occupancy=11, observed=4, baseline=7, stale=2),
                  tmp_path)
    assert sub == ("↑1.2 ↓0.8 /min · 4 observed · 7 declared · "
                   '<span class="drift">2 unseen &gt;1h</span>')


# --- static guards ---------------------------------------------------------
# These run without node, so the brand rules stay enforced on any machine. They
# check the shipped markup rather than behaviour — the branch tests above are
# the ones that exercise the logic, and they need a JS runtime.

def _dashboard():
    with open(DASHBOARD, encoding="utf-8") as fh:
        return fh.read()


def test_drift_is_amber_and_never_critical():
    """CLAUDE.md: red solid means something is wrong right now. Roster drift is
    a data-quality caveat, so it must not borrow the incident colour."""
    src = _dashboard()
    rule = src[src.index(".kpi .drift{"):]
    rule = rule[:rule.index("}")]
    assert "--fb-warning" in rule
    assert "--fb-critical" not in rule


def test_the_tile_does_not_wear_the_corner_bracket_motif():
    """The brackets echo framing marks on a monitored feed and belong to zone
    cards and video frames only — on a KPI tile they would mean nothing."""
    src = _dashboard()
    tile = src[src.index('<p class="eyebrow">In facility</p>'):]
    tile = tile[:tile.index("</div>")]
    assert "fb-bracket" not in tile


def test_the_headline_figure_uses_tabular_numerals():
    """It updates every few seconds; without tnum the strip twitches."""
    src = _dashboard()
    assert 'class="val fb-num" id="kFac"' in src


def test_a_site_with_no_doors_is_told_it_cannot_be_counted():
    """Guards the honest empty state against being replaced with a bare 0."""
    assert "no door zones configured — cannot be counted" in _dashboard()


def test_the_no_door_fallback_is_labelled_as_a_different_measure():
    """With no doors the tile shows people IN VIEW, which is not the same thing
    as door-counted occupancy: it drops to 0 when the last person leaves frame,
    where the roster would keep counting them. Substituting one for the other
    silently would make the tile mean two things with nothing on screen to say
    which, so the label is the feature — guard it."""
    src = _dashboard()
    body = src[src.index("function renderFacility("):src.index("async function pollFacility")]
    assert "LAST_COUNTS.live" in body
    assert "not door-counted" in body


def test_the_tile_polls_instead_of_riding_the_socket():
    """Facility counts must not be hung off /ws.

    The socket pushes at 2Hz and its REST fallback carries zones and alerts
    only, so a tile fed from the socket would go stale exactly when the socket
    dropped. Guard the independent timer so it cannot be "tidied" onto apply().
    """
    with open(DASHBOARD, encoding="utf-8") as fh:
        src = fh.read()
    assert "setInterval(pollFacility,FACILITY_MS)" in src
    assert "/api/v1/facility/occupancy" in src
    # apply() is the socket/REST sink; the facility tile paints from its own poll.
    apply_body = src[src.index("function apply(d){"):src.index("let ws=null")]
    assert "acility" not in apply_body
