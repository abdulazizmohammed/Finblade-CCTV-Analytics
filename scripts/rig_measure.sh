#!/usr/bin/env bash
# One clean measurement of cross-camera identity against the recorded rig.
#
#   bash scripts/rig_measure.sh              # full reset, then score one pass
#   WAIT=200 WIN=4 bash scripts/rig_measure.sh
#
# THE PROTOCOL IS THE POINT. Scoring "the last N minutes" of a running rig gives
# a different answer every time, and it took several contradictory results to
# work out why:
#
#   * The merged recordings are DIFFERENT LENGTHS - 143.2s to 158.8s, a 15.5s
#     spread - so the loops drift apart on every repeat. Only the first pass
#     after a simultaneous start has the real walk ordering; later passes have
#     the person arriving on cameras in an order nobody walked.
#   * The identity registry lives in RAM, so it must start empty. A warm
#     registry carries identities from earlier passes that compete with this
#     one, and the loop wrap-around gets scored as a hop.
#
# So: restart the streams, restart the stack, wait for exactly one pass, score.
# Anything less and you are tuning against noise.
set -uo pipefail

REPO="${REPO:-/home/usv/finblade-cctv}"
RIG="${RIG:-/home/usv/Recordcctv}"
CAMERAS="${CAMERAS:-Reception_Elevator Reception gf_lobby_elevator 1F_Lobby 2F_Lobby}"
WAIT="${WAIT:-170}"      # one loop plus startup
WIN="${WIN:-3}"          # scoring window, minutes

cd "$REPO" || exit 1
rm -f evidence/reid_decisions.jsonl
bash scripts/stop_all.sh >/dev/null 2>&1

cd "$RIG" || exit 1
./stream-merged.sh stop >/dev/null 2>&1

# STACK FIRST, STREAMS SECOND, and the order is the whole point.
#
# stream-merged.sh publishes on demand: each video starts playing when its
# first player connects. Start the streams first and the five videos begin
# staggered by however long each worker takes to load YOLO - tens of seconds -
# so the person arrives on the cameras in an order nobody walked.
#
# With every worker already up and retrying, all five connect within one retry
# interval and the recordings begin in step.
cd "$REPO" || exit 1
bash scripts/pg_dev.sh start >/dev/null 2>&1
( cd scripts && nohup bash start_stack.sh api > /tmp/stack_start.log 2>&1 & )
sleep "${STACK_WAIT:-45}"          # workers up and polling before anything plays

cd "$RIG" || exit 1
# shellcheck disable=SC2086
./stream-merged.sh start $CAMERAS >/dev/null 2>&1
cd "$REPO" || exit 1

sleep "$WAIT"
timeout 300 .venv/bin/python scripts/walk_score.py --minutes "$WIN"
