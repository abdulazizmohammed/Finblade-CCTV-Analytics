# WiseNET RTSP test streams

Replays the WiseNET multi-camera dataset as if it were a set of real RTSP IP
cameras, so the FinBlade CCTV application can be tested against a repeatable,
synchronized multi-camera scenario.

**This is test infrastructure only.** It does not touch the FinBlade
application, its config, its database, or its pipeline. Nothing here should
ever be pointed at a production camera.

---

## Quick start

```bash
cd /home/usv/finblade-cctv/tools/wisenet_rtsp

./start_rtsp_server.sh     # start MediaMTX on :8554
./list_sets.sh             # what scenarios exist
./start_set.sh 2           # play set 2 once, all cameras together
./list_urls.sh 2           # the URLs to paste into the FinBlade UI
./stop_set.sh 2            # stop it
```

## Scripts

| Script | Purpose |
| --- | --- |
| `start_rtsp_server.sh` | Start MediaMTX (RTSP on 8554). |
| `stop_rtsp_server.sh` | Stop MediaMTX; stops any running streams first. |
| `start_set.sh N [--loop]` | Stream every camera of set N. One-shot by default. |
| `stop_set.sh N` | Stop set N's streams. |
| `stop_all_sets.sh` | Stop all streams, leave the server up. |
| `status.sh` | Server state, addresses, which set is live, per-camera state. |
| `list_sets.sh` | All sets with camera count, resolution, fps, duration. |
| `list_urls.sh [N]` | RTSP URLs for one set, or for all sets. |
| `net_info.sh` | Which IP to use depending on where the app runs. |
| `test_set.sh N [--wait]` | End-to-end verification of a set. |

## The dataset

11 sets, 62 cameras total. Sets 1–4 are 1280x720 @ 30fps MPEG-4; sets 5–11
are 640x480 @ 25fps **rawvideo**. Raw video cannot be carried over RTSP, so
every stream is encoded to H.264 (`libx264 -preset ultrafast -tune
zerolatency`) on the way out — which is also what a real IP camera sends.

Source files are only ever read. Nothing is renamed or modified.

Cameras are numbered by sorting each set's `.avi` files naturally, so
`cam_01` always maps to the same file. The full mapping, including codec,
resolution, fps and duration, is written to `wisenet_streams.json` by
`lib/scan.py` and is regenerated automatically when the dataset changes.

## Behaviour worth knowing

- **One-shot by default.** `./start_set.sh 2` plays the set once and finishes,
  so entry/exit and tracking tests have a clean beginning and end. Use
  `--loop` for continuous playback.
- **Cameras start together.** Every ffmpeg is spawned first and parked on a
  shared barrier file, then all are released at once. Measured spread across
  the 5 cameras of set 2: under one 20 ms polling interval. No per-camera
  delays are ever added, and `-re` makes each file play at its recorded speed.
- **One scenario at a time.** Starting a set stops whatever was running, so
  scenarios cannot contaminate each other.
- **Stopping is scoped.** `stop_set.sh` only signals PIDs it recorded *and*
  whose `/proc/<pid>/cmdline` still contains that camera's exact RTSP URL. It
  never does anything like `killall ffmpeg`. Verified: an unrelated ffmpeg
  publishing to `production/camera_x` on the same server survived a full
  start/replace/stop cycle untouched.
- **SIGKILL is expected.** ffmpeg 7.0.2 catches SIGTERM and SIGINT but does
  not act on either while publishing (measured: still alive after 15s, parked
  in `futex_wait_queue`); only SIGQUIT ends it, and that dumps core. So stops
  send SIGTERM, wait 2s, then SIGKILL. That is safe here — these are synthetic
  feeds with nothing to flush, and MediaMTX drops the path within ~2s of the
  socket closing.
- **Streams outlive your terminal.** Publishers ignore SIGHUP and run in their
  own session, so closing the shell does not kill the scenario. Use
  `stop_set.sh` / `stop_all_sets.sh` to end it.

## Which address to use

Run `./net_info.sh`. In short:

| Where the FinBlade app runs | URL |
| --- | --- |
| Same WSL instance | `rtsp://127.0.0.1:8554/set_2/cam_01` |
| Windows host | `rtsp://<WSL-IP>:8554/set_2/cam_01` |
| Another machine on the LAN | needs a `netsh portproxy` on Windows — see `net_info.sh` |

WSL2 is NAT'd, so the WSL IP changes when WSL restarts. Re-run `net_info.sh`
after a reboot and update the URLs in the FinBlade UI if they changed.

## Files

```
bin/            mediamtx + ffprobe (downloaded, not in git)
lib/            shared shell helpers and python scanners
runtime/        pids, logs, generated config (not in git)
  mediamtx.log
  set_<n>_cam_<xx>.log
mediamtx.yml    server config template
wisenet_streams.json   generated set -> camera -> source/URL mapping
```

`bin/` is populated by downloading MediaMTX and a static ffprobe; the ffmpeg
binary already present at `../../.tools/ffmpeg` is reused.
