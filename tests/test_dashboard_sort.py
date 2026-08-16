"""The dashboard orders cameras by how much of their pipeline is alive.

A wall display is read top-left first, so the cameras actually producing frames
have to be the ones you see. These tests lift the comparator out of
web/dashboard.html and run it under node, so they exercise the shipped code
rather than a copy of it that can drift.

Skipped when node is unavailable — this is UI behaviour, and a missing JS
runtime must not fail a Python test run.
"""

import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO, "web", "dashboard.html")

node = pytest.mark.skipif(shutil.which("node") is None,
                          reason="node not installed")


def read_dashboard():
    with open(DASHBOARD, encoding="utf-8") as fh:
        return fh.read()


def comparator_source():
    """The ranking block, verbatim from the dashboard."""
    src = read_dashboard()
    start = src.index("const STATE_RANK=")
    end = src.index("function renderFeeds(")
    return src[start:end]


def run_js(body, tmp_path):
    script = tmp_path / "t.js"
    script.write_text(comparator_source() + "\n" + body, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def order_of(cameras, tmp_path):
    js = ("const cams=%s;\n"
          "console.log(cams.slice().sort(byPipelineHealth)"
          ".map(c=>c.camera_id).join(','));" % cameras)
    return run_js(js, tmp_path).split(",")


# --- the ordering ---------------------------------------------------------

@node
def test_working_pipelines_come_before_stopped_ones(tmp_path):
    cams = """[
      {camera_id:"CAM-05", effective_state:"OFFLINE"},
      {camera_id:"CAM-01", effective_state:"DISABLED"},
      {camera_id:"CAM-04", effective_state:"ONLINE"},
      {camera_id:"CAM-02", effective_state:"RECONNECTING"},
      {camera_id:"CAM-03", effective_state:"DEGRADED"},
      {camera_id:"CAM-06", effective_state:"ONLINE"}
    ]"""
    assert order_of(cams, tmp_path) == [
        "CAM-04", "CAM-06",      # producing frames
        "CAM-03",                # degraded but running
        "CAM-02",                # still trying
        "CAM-05",                # stopped
        "CAM-01",                # switched off deliberately, least interesting
    ]


@node
def test_ties_are_stable_so_tiles_do_not_swap_on_every_poll(tmp_path):
    cams = ('[{camera_id:"CAM-09",effective_state:"ONLINE"},'
            '{camera_id:"CAM-02",effective_state:"ONLINE"}]')
    assert order_of(cams, tmp_path) == ["CAM-02", "CAM-09"]


@node
def test_an_unrecognised_state_is_treated_as_alive(tmp_path):
    """A worker reporting something we do not know about is still reporting.

    Ranking an unknown state as dead would hide a running camera, which is the
    worse of the two mistakes.
    """
    cams = ('[{camera_id:"A",effective_state:"OFFLINE"},'
            '{camera_id:"B",effective_state:"STARTING"}]')
    assert order_of(cams, tmp_path) == ["B", "A"]


@node
def test_a_missing_state_sorts_as_offline(tmp_path):
    cams = '[{camera_id:"A"},{camera_id:"B",effective_state:"ONLINE"}]'
    assert order_of(cams, tmp_path) == ["B", "A"]


# --- the wiring -----------------------------------------------------------

@node
def test_dashboard_inline_javascript_parses(tmp_path):
    """Cheap guard: a syntax error here blanks the whole dashboard."""
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", read_dashboard(), re.S)
    joined = "\n;\n".join(blocks)
    script = tmp_path / "dash.js"
    script.write_text(joined, encoding="utf-8")
    out = subprocess.run(["node", "--check", str(script)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_both_panels_use_the_same_ordering():
    """The feed tiles and the health chips must not disagree."""
    src = read_dashboard()
    assert src.count("byPipelineHealth") >= 3, \
        "expected the comparator plus its use in renderFeeds and renderCameras"
    assert ".sort(byPipelineHealth)" in src


def test_existing_tiles_are_reordered_not_just_the_array():
    """Tiles are appended only when created, so sorting the array is not enough.

    Without the DOM move, a camera coming back online keeps whatever position
    it was first created in and the sort has no visible effect.
    """
    src = read_dashboard()
    assert "host.dataset.order" in src, "order signature guard is missing"
    assert "host.appendChild(t.tile)" in src, "tiles are never re-appended"
