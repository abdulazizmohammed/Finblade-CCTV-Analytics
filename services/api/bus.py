"""Redis Streams event bus (UC-27) + an in-memory fake for tests.

The real bus uses XADD/XREAD on a Redis Stream. InMemoryBus lets the ingest path
be tested end-to-end without Redis.

TWO STREAMS, ONE CONNECTION.

  fb:events     every ingested event, as posted. The firehose.
  fb:facility   merged facility counts — occupancy, observed, baseline, per-door
                totals. Derived from the roster in finblade/presence.py, which
                counts DOOR CROSSINGS BY GLOBAL IDENTITY, not per-camera
                detections, so a person visible on three cameras is one person
                here and someone in a corridor no camera watches is still
                counted.

A consumer that wants the headline number should not have to parse the firehose
to find it, and the two have completely different cadences: events arrive per
person per movement, counts change a handful of times a minute. They are
separated by stream rather than by a second bus object because a second object
would mean a second Redis connection for no gain.

Also provides the embedding-TTL helper (UC-56): a privacy story where any
transient per-person key expires automatically.
"""

import json
from typing import Dict, List

# Stream names. Constants rather than literals at the call sites so a publisher
# and a consumer cannot disagree about the spelling of a stream, which fails
# silently — XADD to a misspelled stream succeeds and creates it.
EVENTS_STREAM = "fb:events"
FACILITY_STREAM = "fb:facility"

# Retained entries per stream. Approximate trimming: exact trimming makes XADD
# O(n) on every call, and nothing here needs a precise backlog length.
MAXLEN = 10000


class InMemoryBus:
    def __init__(self):
        self.streams: Dict[str, List[dict]] = {}

    @property
    def published(self) -> List[dict]:
        """Everything on the default stream.

        Kept as the name it has always had — existing callers and tests read it
        — and live rather than a copy, so an append through it still works.
        """
        return self.streams.setdefault(EVENTS_STREAM, [])

    def publish(self, evt: dict) -> None:
        self.publish_to(EVENTS_STREAM, evt)

    def publish_to(self, stream: str, evt: dict) -> None:
        self.streams.setdefault(stream, []).append(dict(evt))

    def consume(self) -> List[dict]:
        return list(self.published)

    def consume_from(self, stream: str) -> List[dict]:
        return list(self.streams.get(stream, ()))


class RedisStreamBus:  # pragma: no cover - requires redis-server + redis-py
    def __init__(self, url: str = "redis://redis:6379/0",
                 stream: str = EVENTS_STREAM):
        import redis
        self._r = redis.from_url(url)
        self.stream = stream

    def publish(self, evt: dict) -> None:
        self.publish_to(self.stream, evt)

    def publish_to(self, stream: str, evt: dict) -> None:
        self._r.xadd(stream, {"data": json.dumps(evt)}, maxlen=MAXLEN,
                     approximate=True)

    def consume(self, last_id: str = "0", count: int = 100, block_ms: int = 1000):
        return self.consume_from(self.stream, last_id=last_id, count=count,
                                 block_ms=block_ms)

    def consume_from(self, stream: str, last_id: str = "0", count: int = 100,
                     block_ms: int = 1000):
        res = self._r.xread({stream: last_id}, count=count, block=block_ms)
        out = []
        for _stream, entries in res or []:
            for entry_id, fields in entries:
                out.append((entry_id.decode(), json.loads(fields[b"data"])))
        return out

    def set_person_embedding_ttl(self, person_ref: str, value: str, ttl_seconds: int) -> None:
        """UC-56: store a transient per-person key that auto-expires. We keep no
        appearance data long-term; the TTL is the enforced privacy boundary."""
        self._r.set(f"fb:emb:{person_ref}", value, ex=ttl_seconds)
