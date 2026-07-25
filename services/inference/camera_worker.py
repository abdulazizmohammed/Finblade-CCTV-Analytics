"""CameraWorker — non-blocking video ingest with health tracking.

A dedicated capture thread reads the source and keeps ONLY the latest frame in a
one-slot buffer (no unbounded queue). It measures input FPS, detects frozen
frames and open/decoder failures, counts dropped frames and reconnects, and
drives a camera state machine — all INDEPENDENT of person detection, so an empty
scene is never mistaken for an offline camera.

Design for testability: the capture backend is injected via ``open_capture`` (cv2
is imported lazily only in the default factory), and one loop iteration is the
pure method ``_tick(now)``. Tests drive ``_tick`` with a fake capture and a
synthetic clock — no real camera, threads, or cv2 required.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional, Tuple

log = logging.getLogger("finblade.camera")


class CameraState:
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"


def _default_open(source):
    """Default capture factory (cv2). Returns an opened capture or None."""
    import cv2  # lazy: keeps this module importable without cv2 (tests)
    if isinstance(source, str) and source.startswith(("rtsp://", "http://", "https://")):
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    else:
        cap = cv2.VideoCapture(source)
    if cap is None or not cap.isOpened():
        try:
            if cap is not None:
                cap.release()
        except Exception:
            pass
        return None
    return cap


def _signature(frame) -> int:
    """Cheap content signature for frozen-frame detection.

    For real image frames (numpy) it samples ~a few KB of bytes; for the plain
    frames used in tests it just uses the (hashable) frame itself.
    """
    tb = getattr(frame, "tobytes", None)
    if callable(tb):
        try:
            b = tb()
            step = max(1, len(b) // 4096)
            return hash(b[::step])
        except Exception:
            return id(frame)
    try:
        return hash(frame)
    except TypeError:
        return id(frame)


class CameraWorker:
    def __init__(
        self,
        source,
        camera_id: str,
        *,
        enabled: bool = True,
        loop_file: bool = True,
        offline_seconds: float = 30.0,
        frozen_seconds: float = 6.0,
        degraded_fps: float = 0.0,      # 0 disables fps-based DEGRADED
        pace_fps: Optional[float] = None,  # throttle capture (files); None = full speed
        fps_window: float = 2.0,
        open_capture: Optional[Callable] = None,
    ):
        self.source = source
        self.camera_id = camera_id
        self.loop_file = loop_file
        self.offline_seconds = offline_seconds
        self.frozen_seconds = frozen_seconds
        self.degraded_fps = degraded_fps
        self.pace_fps = pace_fps
        self._open = open_capture or _default_open
        self._is_file = isinstance(source, str) and "://" not in str(source) \
            and not str(source).startswith("/dev/")

        # latest-frame slot (guarded by _lock)
        self._lock = threading.Lock()
        self._frame = None
        self._frame_ts: Optional[float] = None
        self._seq = 0
        self._resolution: Optional[Tuple[int, int]] = None

        # capture + health state
        self._cap = None
        self._enabled = enabled
        self._simulate_fail = False
        self._reconnecting = False
        self._frozen = False
        self._last_valid_ts: Optional[float] = None
        self._last_sig = None
        self._last_change_ts: Optional[float] = None
        self._dropped = 0
        self._reconnects = 0
        self._loops = 0
        self._ts_window = deque()
        self._fps_window = fps_window

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> "CameraWorker":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"cap-{self.camera_id}",
                                        daemon=True)
        self._thread.start()
        log.info("camera %s worker started (source=%s)", self.camera_id, self.source)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._close()
        log.info("camera %s worker stopped", self.camera_id)

    def _run(self) -> None:
        interval = (1.0 / self.pace_fps) if self.pace_fps and self.pace_fps > 0 else 0.0
        while not self._stop.is_set():
            t = time.time()
            try:
                self._tick(t)
            except Exception:                      # never let the thread die
                log.exception("camera %s capture tick failed", self.camera_id)
                self._close()
                self._reconnecting = True
                time.sleep(0.5)
                continue
            if interval:
                time.sleep(interval)
            else:
                time.sleep(0.001)                  # yield; RTSP read() blocks anyway

    # ---- one capture iteration (pure, testable) ---------------------------
    def _tick(self, now: float) -> None:
        if not self._enabled or self._simulate_fail:
            # Deliver nothing; last_valid_ts goes stale -> OFFLINE after threshold.
            self._close()
            return
        cap = self._ensure_open()
        if cap is None:
            self._reconnecting = True
            return
        ok, frame = cap.read()
        if not ok or frame is None:
            self._dropped += 1
            self._close()
            if self._is_file and self.loop_file:
                self._loops += 1                   # clip ended -> reopen to loop
            else:
                self._reconnects += 1
                self._reconnecting = True
            return
        self._reconnecting = False
        self._on_valid_frame(frame, now)

    def _ensure_open(self):
        if self._cap is not None:
            return self._cap
        try:
            self._cap = self._open(self.source)
        except Exception:
            log.exception("camera %s open failed", self.camera_id)
            self._cap = None
        if self._cap is None:
            return None
        return self._cap

    def _close(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _on_valid_frame(self, frame, now: float) -> None:
        sig = _signature(frame)
        # A gap longer than the frozen window breaks continuity, so a repeated
        # frame after an outage must NOT be counted as frozen.
        gap = self._last_valid_ts is None or (now - self._last_valid_ts) > self.frozen_seconds
        if gap or sig != self._last_sig:
            self._last_sig = sig
            self._last_change_ts = now
            self._frozen = False
        elif self._last_change_ts is not None and (now - self._last_change_ts) > self.frozen_seconds:
            self._frozen = True                    # identical content for too long

        self._last_valid_ts = now
        self._ts_window.append(now)
        cutoff = now - self._fps_window
        while self._ts_window and self._ts_window[0] < cutoff:
            self._ts_window.popleft()

        shape = getattr(frame, "shape", None)
        if shape is not None and len(shape) >= 2:
            self._resolution = (int(shape[1]), int(shape[0]))  # (w, h)

        with self._lock:
            self._frame = frame
            self._frame_ts = now
            self._seq += 1

    # ---- consumer API -----------------------------------------------------
    def read_latest(self):
        """Return (frame, frame_ts, seq). frame is None if no fresh frame yet."""
        with self._lock:
            return self._frame, self._frame_ts, self._seq

    def input_fps(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        cutoff = now - self._fps_window
        n = sum(1 for t in self._ts_window if t >= cutoff)
        return n / self._fps_window if self._fps_window else 0.0

    def state(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        if not self._enabled:
            return CameraState.DISABLED
        last = self._last_valid_ts
        if last is None or (now - last) > self.offline_seconds:
            return CameraState.OFFLINE
        if self._reconnecting:
            return CameraState.RECONNECTING
        if self._frozen:
            return CameraState.DEGRADED
        if self.degraded_fps > 0 and self.input_fps(now) < self.degraded_fps:
            return CameraState.DEGRADED
        return CameraState.ONLINE

    def health(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else time.time()
        with self._lock:
            seq, frame_ts, res = self._seq, self._frame_ts, self._resolution
        return {
            "camera_id": self.camera_id,
            "state": self.state(now),
            "enabled": self._enabled,
            "last_valid_ts": self._last_valid_ts,
            "seconds_since_valid": (now - self._last_valid_ts) if self._last_valid_ts else None,
            "input_fps": round(self.input_fps(now), 1),
            "frozen": self._frozen,
            "reconnecting": self._reconnecting,
            "dropped_frames": self._dropped,
            "reconnects": self._reconnects,
            "loops": self._loops,
            "resolution": res,
            "frame_seq": seq,
            "frame_ts": frame_ts,
        }

    # ---- demo + control ---------------------------------------------------
    def simulate_failure(self) -> None:
        """Predictable demo: stop delivering frames (camera goes OFFLINE in 30s)."""
        self._simulate_fail = True
        log.warning("camera %s: SIMULATED failure", self.camera_id)

    def restore(self) -> None:
        self._simulate_fail = False
        log.info("camera %s: restore requested", self.camera_id)

    @property
    def simulating(self) -> bool:
        return self._simulate_fail

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False
        self._close()
