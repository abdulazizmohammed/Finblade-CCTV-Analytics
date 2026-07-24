"""Redis Streams event bus (UC-27) + an in-memory fake for tests.

The real bus uses XADD/XREAD on a Redis Stream. It is not exercised in the
authoring env (no redis-server / redis-py — BLOCKERS.md B-3). InMemoryBus lets
the ingest path be tested end-to-end without Redis.

Also provides the embedding-TTL helper (UC-56): a privacy story where any
transient per-person key expires automatically.
"""

import json
from typing import List


class InMemoryBus:
    def __init__(self):
        self.published: List[dict] = []

    def publish(self, evt: dict) -> None:
        self.published.append(dict(evt))

    def consume(self) -> List[dict]:
        return list(self.published)


class RedisStreamBus:  # pragma: no cover - requires redis-server + redis-py
    def __init__(self, url: str = "redis://redis:6379/0", stream: str = "fb:events"):
        import redis
        self._r = redis.from_url(url)
        self.stream = stream

    def publish(self, evt: dict) -> None:
        self._r.xadd(self.stream, {"data": json.dumps(evt)}, maxlen=10000, approximate=True)

    def consume(self, last_id: str = "0", count: int = 100, block_ms: int = 1000):
        res = self._r.xread({self.stream: last_id}, count=count, block=block_ms)
        out = []
        for _stream, entries in res or []:
            for entry_id, fields in entries:
                out.append((entry_id.decode(), json.loads(fields[b"data"])))
        return out

    def set_person_embedding_ttl(self, person_ref: str, value: str, ttl_seconds: int) -> None:
        """UC-56: store a transient per-person key that auto-expires. We keep no
        appearance data long-term; the TTL is the enforced privacy boundary."""
        self._r.set(f"fb:emb:{person_ref}", value, ex=ttl_seconds)
