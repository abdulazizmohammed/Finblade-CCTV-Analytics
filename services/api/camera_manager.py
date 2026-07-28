"""Launch/stop per-camera detection pipelines for UI-provisioned cameras.

When a camera is added from the dashboard with a source (RTSP URL or file), the
API starts a `run_cpu.py` subprocess for it (assigning a stream port), and stops
it when the camera is deleted. Local process management only; args are passed as a
list (never a shell string) so a source value can't inject a command.
"""
import os
import socket
import subprocess
import sys
import threading

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE = os.path.join(_REPO, "config", "cameras.template.yaml")
_RUNNER = os.path.join(_REPO, "services", "inference", "run_cpu.py")
_LOGDIR = os.path.join(_REPO, "scripts")


def _slug(cid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in cid)


class CameraManager:
    def __init__(self, api_url: str = "http://127.0.0.1:8000", base_port: int = 8090):
        self.api_url = api_url
        self.base_port = base_port
        self._procs = {}                    # camera_id -> {proc, port, source, stream_url, log}
        self._lock = threading.Lock()

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
        self.stop(camera_id)
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
            log = open(os.path.join(_LOGDIR, f"cam_{_slug(camera_id)}.log"), "w")
            proc = subprocess.Popen(cmd, cwd=_REPO, stdout=log,
                                    stderr=subprocess.STDOUT)
            self._procs[camera_id] = {"proc": proc, "port": port, "source": source,
                                      "stream_url": stream_url, "log": log}
            return {"port": port, "stream_url": stream_url, "pid": proc.pid}

    def stop(self, camera_id: str) -> bool:
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
