"""Launch/stop per-camera detection pipelines for UI-provisioned cameras.

When a camera is added from the dashboard with a source (RTSP URL or file), the
API starts a `run_cpu.py` subprocess for it (assigning a stream port), and stops
it when the camera is deleted. Local process management only; args are passed as a
list (never a shell string) so a source value can't inject a command.
"""
import logging
import os
import socket
import subprocess
import sys
import threading
import time

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE = os.path.join(_REPO, "config", "cameras.template.yaml")
_RUNNER = os.path.join(_REPO, "services", "inference", "run_cpu.py")
_LOGDIR = os.path.join(_REPO, "scripts")

log = logging.getLogger("finblade.camera")

# Restart backoff after a worker exits on its own: first retry quickly (a
# transient RTSP drop), then slower, never faster than this once it keeps
# dying (a real fault — out of memory, a bad source — that a tight loop would
# only hide behind constant restarts). Never gives up: an operator sees the
# crash count and last log lines in /health and decides.
_BACKOFF_S = (5.0, 15.0, 60.0, 300.0)
_TAIL_LINES = 12


def _tail(path: str, n: int = _TAIL_LINES):
    """Last n non-trivial lines of a worker log — the crash reason lives there."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    keep = [ln for ln in lines if ln.strip() and "tracked=" not in ln and "heartbeat" not in ln]
    return keep[-n:]


def _slug(cid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in cid)


class CameraManager:
    def __init__(self, api_url: str = "http://127.0.0.1:8000", base_port: int = 8090):
        self.api_url = api_url
        self.base_port = base_port
        self._procs = {}                    # camera_id -> {proc, port, source, stream_url, log}
        self._lock = threading.Lock()
        # camera_id -> crash record: how often it has died, why (exit code +
        # log tail), and when it may next be relaunched. Kept after a stop()
        # only if the stop was ours to reap, so an operator's deliberate stop
        # never reads as a crash.
        self._crashes = {}

    @staticmethod
    def _port_free(port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _next_port(self) -> int:
        used = {e["port"] for e in self._procs.values()}
        port = self.base_port
        while port in used or not self._port_free(port):
            port += 1
        return port

    def is_running(self, camera_id: str) -> bool:
        with self._lock:
            e = self._procs.get(camera_id)
            return bool(e and e["proc"].poll() is None)

    def local_port(self, camera_id: str):
        """MJPEG port this camera serves on, or None if we did not launch it.

        The API and the camera processes share a host, so the API can always
        reach 127.0.0.1:<port> even when the browser cannot — which is what
        lets the stream be proxied through the API's single exposed port.
        """
        with self._lock:
            e = self._procs.get(camera_id)
            return e["port"] if e else None

    def launch(self, camera_id: str, source: str, site_id: str = None,
               stream_host: str = "localhost") -> dict:
        """Start (or restart) the pipeline for camera_id reading `source`."""
        self._stop_proc(camera_id)              # keeps the crash record; stop() clears it
        with self._lock:
            port = self._next_port()
            stream_url = f"http://{stream_host}:{port}/stream"
            cmd = [sys.executable, "-u", _RUNNER,
                   "--config", _TEMPLATE,
                   "--source", source,
                   "--camera-id", camera_id,
                   "--port", str(port),
                   "--stream-host", stream_host,
                   "--api-url", self.api_url]
            if site_id:
                cmd += ["--site-id", site_id]
            log_path = os.path.join(_LOGDIR, f"cam_{_slug(camera_id)}.log")
            logf = open(log_path, "w")
            proc = subprocess.Popen(cmd, cwd=_REPO, stdout=logf,
                                    stderr=subprocess.STDOUT)
            self._procs[camera_id] = {"proc": proc, "port": port, "source": source,
                                      "stream_url": stream_url, "log": logf,
                                      "log_path": log_path, "site_id": site_id,
                                      "stream_host": stream_host, "started_at": time.time()}
            return {"port": port, "stream_url": stream_url, "pid": proc.pid}

    # ---- supervision --------------------------------------------------------
    def reap(self, now: float = None) -> list:
        """Relaunch workers that exited on their own. Call every few seconds.

        A worker that dies — out of memory, a CUDA fault, a source it could
        not recover — used to stay dead: the camera went OFFLINE and waited
        for a person to press Start. Now the exit is recorded (code + the
        last lines of its log, so the REASON is visible in /health) and the
        worker is relaunched after a backoff that grows while it keeps dying.
        Returns the ids relaunched this call.
        """
        now = time.time() if now is None else now
        relaunched = []
        with self._lock:
            dead = [(cid, e) for cid, e in self._procs.items() if e["proc"].poll() is not None]
        for cid, e in dead:
            code = e["proc"].returncode
            rec = self._crashes.setdefault(cid, {"count": 0, "last_exit": None, "last_at": None,
                                                 "last_log": [], "next_at": 0.0})
            if rec["last_at"] is None or rec["last_at"] < e["started_at"]:
                # first time we notice THIS death
                rec["count"] += 1
                rec["last_exit"] = code
                rec["last_at"] = now
                rec["last_log"] = _tail(e["log_path"])
                delay = _BACKOFF_S[min(rec["count"] - 1, len(_BACKOFF_S) - 1)]
                # a worker that ran for a good while before dying earned a fresh start
                if now - e["started_at"] > 3600:
                    rec["count"] = 1
                    delay = _BACKOFF_S[0]
                rec["next_at"] = now + delay
                log.warning("camera %s: worker exited (code %s) after %.0fs; relaunch in %.0fs. tail: %s",
                            cid, code, now - e["started_at"], delay,
                            " | ".join(rec["last_log"][-3:])[:400])
                try:
                    e["log"].close()
                except Exception:                           # noqa: BLE001
                    pass
            if now >= rec["next_at"]:
                try:
                    self.launch(cid, e["source"], site_id=e.get("site_id"),
                                stream_host=e.get("stream_host") or "localhost")
                    relaunched.append(cid)
                    log.info("camera %s: relaunched (crash #%d)", cid, rec["count"])
                except Exception:                           # noqa: BLE001
                    log.exception("camera %s: relaunch failed", cid)
                    rec["next_at"] = now + _BACKOFF_S[-1]
        return relaunched

    def crash_report(self) -> dict:
        """Per-camera crash history for /health: count, last exit code and
        time, the log tail that explains it, and whether a relaunch is pending."""
        out = {}
        with self._lock:
            for cid, r in self._crashes.items():
                running = cid in self._procs and self._procs[cid]["proc"].poll() is None
                out[cid] = {"crashes": r["count"], "last_exit_code": r["last_exit"],
                            "last_crash_at": r["last_at"], "running": running,
                            "relaunch_due_at": None if running else r["next_at"],
                            "last_log": r["last_log"]}
        return out

    def stop(self, camera_id: str) -> bool:
        """An operator's stop: the worker goes and so does its crash record —
        a deliberate stop is not a crash, and must not be relaunched."""
        with self._lock:
            self._crashes.pop(camera_id, None)
        return self._stop_proc(camera_id)

    def _stop_proc(self, camera_id: str) -> bool:
        with self._lock:
            e = self._procs.pop(camera_id, None)
        stopped = False
        if e:
            try:
                e["proc"].terminate()
                e["proc"].wait(timeout=5)
            except Exception:
                try:
                    e["proc"].kill()
                except Exception:
                    pass
            try:
                e["log"].close()
            except Exception:
                pass
            stopped = True
        # Also kill any orphaned pipeline for this camera (e.g. one launched before
        # an API restart). Only matches OUR pipelines, which carry --camera-id <id>.
        try:
            # No leading "--" in the pattern: pkill parses an argument starting
            # with dashes as its own options, so `pkill -f "--camera-id X"` just
            # printed pgrep usage into the API log and killed nothing. Matching
            # "camera-id X" hits the same command lines without the ambiguity.
            subprocess.run(["pkill", "-f", f"camera-id {camera_id}"],
                           timeout=5, check=False)
        except Exception:
            pass
        return stopped

    def stop_all(self) -> None:
        for cid in list(self._procs):
            self.stop(cid)
