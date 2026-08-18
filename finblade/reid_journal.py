"""What the matcher decided, and what it was choosing between.

WHY THIS EXISTS. Embeddings are never persisted — RAM only, cleared on track
reap, dropped at TTL. That is the privacy property the system rests on, and it
is not negotiable. The cost is that the matcher CANNOT BE REPLAYED: with no
vectors on disk, "would threshold 0.65 have matched that hop?" can only be
answered by walking the building again. Every tuning decision costs a walk, and
a walk is twenty minutes of somebody's day plus a quiet building.

So record the decisions instead of the inputs. A similarity score is a float,
not a biometric: it says how alike two crops looked, and it cannot be inverted
into a face. Keeping the score of every candidate — not just the winner — is
what makes offline replay real, because raising a threshold changes which
candidates cross it and you need the whole set to know what else would have.

WHAT IS DELIBERATELY NOT IN HERE: embeddings, crops, bounding boxes, image
paths. The global_ref is the same opaque session-salted hash that already goes
to the database, and person_ref never appears at all.

OFF BY DEFAULT. Set FINBLADE_REID_JOURNAL to a path to turn it on for an
evaluation run. It is a diagnostic, not telemetry, and a system that quietly
writes a record of every person it sees is a different product from one that
does not.

IT MUST NEVER BREAK A RESOLVE. A full disk, a read-only mount or a permissions
error is a reason to stop journalling, not a reason to stop identifying people.
Every failure here is swallowed and counted; the counter is what tells you the
journal is incomplete, rather than silence.
"""

import json
import os
import threading
from typing import Optional

# A bound, because this appends per resolve and an evaluation run that is left
# switched on for a week must not fill the disk. At roughly 300 bytes an entry
# this is about 175k decisions — far more than any walk produces.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024

ENV_PATH = "FINBLADE_REID_JOURNAL"


class DecisionJournal:
    """Append-only JSONL of match decisions. Thread-safe, bounded, non-fatal."""

    def __init__(self, path: Optional[str] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = path or os.environ.get(ENV_PATH) or None
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._fh = None
        self._written = 0
        self.stats = {"entries": 0, "errors": 0, "dropped_full": 0}

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _open(self):
        if self._fh is not None:
            return self._fh
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Append: a restart mid-evaluation continues the same run rather than
        # silently discarding what came before it.
        self._fh = open(self.path, "a", encoding="utf-8")
        try:
            self._written = os.path.getsize(self.path)
        except OSError:
            self._written = 0
        return self._fh

    def record(self, entry: dict) -> bool:
        """Append one decision. Returns whether it was written.

        Never raises. The caller is in the middle of answering "who is this?"
        and a journalling problem must not change that answer.
        """
        if not self.enabled:
            return False
        try:
            line = json.dumps(entry, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            self.stats["errors"] += 1
            return False

        with self._lock:
            try:
                fh = self._open()
                if self._written + len(line) + 1 > self.max_bytes:
                    self.stats["dropped_full"] += 1
                    return False
                fh.write(line + "\n")
                fh.flush()          # a killed worker must not lose the tail
                self._written += len(line) + 1
                self.stats["entries"] += 1
                return True
            except OSError:
                self.stats["errors"] += 1
                self._fh = None     # reopen next time; the mount may come back
                return False

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def snapshot(self) -> dict:
        return {"enabled": self.enabled, "path": self.path, **self.stats}
