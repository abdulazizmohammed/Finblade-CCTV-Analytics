"""Anonymous person_ref (UC-19).

We never store faces, appearance vectors, or any PII. A person_ref is a salted
hash of the ephemeral tracker id for the current session only. Same track ->
same ref within a session; a new session (new salt) yields a different ref, so
refs cannot be correlated across sessions.
"""

import hashlib
import secrets


class PersonRefHasher:
    def __init__(self, session_salt: str = None):
        # A random per-session salt means refs are not linkable across runs.
        self.session_salt = session_salt or secrets.token_hex(16)

    def ref(self, track_id: int) -> str:
        digest = hashlib.sha256(f"{self.session_salt}:{int(track_id)}".encode("utf-8"))
        return "pr_" + digest.hexdigest()[:16]

    @staticmethod
    def looks_anonymous(ref: str) -> bool:
        """Sanity gate for tests/output scrubbing: a ref must be the opaque
        hash form and contain nothing that could be PII."""
        if not ref.startswith("pr_"):
            return False
        body = ref[3:]
        return len(body) == 16 and all(c in "0123456789abcdef" for c in body)
