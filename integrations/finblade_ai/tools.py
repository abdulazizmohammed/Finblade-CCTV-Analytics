"""Tool definitions for the FinBlade chatbot.

The chatbot answers questions about a site by CALLING THIS API, not by querying
the database. FinBlade asked for a database view instead; these are what we are
offering in its place, and the reason is in README.md — the semantics that make
these numbers correct do not survive being reimplemented in someone else's SQL.

Six tools. Each one maps to a single endpoint and each description tells the
model the thing it would otherwise get wrong.

WHAT THE MODEL MUST NOT GUESS, AND HOW THESE STOP IT

  * That a missing row means zero. The stored history is written on change, so
    a zone with four rows for a whole day is not a zone with four minutes of
    data. Every tool that reads history goes through an endpoint that holds
    readings forward.
  * That a `null` bucket is an empty room. It means the camera was not
    observing, and the descriptions say so in those words.
  * That an average covers the window it was asked for. `coverage` says how
    much of it was actually observed, and the descriptions tell the model to
    say so out loud below 0.95.
  * That `zone_id` identifies a zone. It is unique only within a camera. The
    API returns 409 with the candidates rather than picking one.

WHY `strict: true` ON EVERY SCHEMA. These are read-only, but the arguments
still reach a comparison and a time range. Strict validation means a
hallucinated field name is a schema error at the tool boundary rather than a
silently empty result — the model retries instead of reporting "never".

Transport is CCTVClient (cctv_client.py), so the allowlist, the API key and the
response cache apply to the chatbot exactly as they do to the tiles.
"""

from typing import Any, Dict, List, Optional

# Every tool below is a GET against one of these. Kept in one place so the
# allowlist in cctv_client.ROUTES and this file cannot drift apart.
TOOL_ROUTES = {
    "cctv_live_state":     "/api/v1/zones/state",
    "cctv_zone_history":   "/api/v1/zones/{zone_id}/series",
    "cctv_zone_at_time":   "/api/v1/zones/{zone_id}/at",
    "cctv_zone_duration":  "/api/v1/zones/{zone_id}/duration",
    "cctv_alerts":         "/api/v1/history/alerts",
    "cctv_occupancy_report": "/api/v1/reports/occupancy.json",
}

_COVERAGE_NOTE = (
    "The response carries `coverage` (0.0-1.0), the fraction of the window the "
    "camera was actually watching. If it is below 0.95 you MUST say so in your "
    "answer - for example 'the camera only covered 4 of those 24 hours'. An "
    "average over a partly observed window is a real number about a small slice "
    "of time, and presenting it unqualified is misleading."
)

_ZONE_NOTE = (
    "zone_id is unique only WITHIN a camera - every camera numbers its zones "
    "from ZONE-01, so 'ZONE-01' may name several unrelated areas. If the tool "
    "returns a 409 'ambiguous' result it will list the candidate cameras; ask "
    "the user which one they mean, or call cctv_live_state first to see the "
    "zone names. Never pick one yourself."
)

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "cctv_live_state",
        "description": (
            "Current occupancy, density and status for every monitored zone, "
            "right now. Use this for 'how busy is it', 'is anyone in X', "
            "'what's happening now'. Also the way to discover what zones and "
            "cameras exist and what they are called - call it first when the "
            "user names a place in words ('the lobby') rather than an id.\n\n"
            "Returns nothing for a zone whose camera has been silent for 30 "
            "seconds. An absent zone means 'not reporting', NOT 'empty'."),
        "input_schema": {
            "type": "object",
            "properties": {
                "camera_id": {"type": ["string", "null"],
                              "description": "Restrict to one camera. Omit for all."},
            },
            "required": ["camera_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "cctv_zone_history",
        "description": (
            "How one zone's occupancy changed over a period, as evenly spaced "
            "buckets. Use for 'how busy was the lobby this morning', 'show me "
            "yesterday', 'when was it busiest', or anything asking about a "
            "trend or a shape over time.\n\n"
            "IMPORTANT: an `occupancy` of null in a bucket means the camera was "
            "NOT OBSERVING then. It does not mean the zone was empty. Say "
            "'no data' for those periods. `gaps` lists them with a reason: "
            "'camera_offline' when the outage was logged, 'no_data' when the "
            "camera stopped reporting without saying why.\n\n"
            "Each bucket also carries `peak_occupancy`. Prefer it when the user "
            "asks whether anyone was there at all - a five-minute mean of 0.1 "
            "still means someone walked through.\n\n"
            + _COVERAGE_NOTE + "\n\n" + _ZONE_NOTE),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string"},
                "camera_id": {"type": ["string", "null"],
                              "description": "Required if zone_id is ambiguous."},
                "hours": {"type": ["number", "null"],
                          "description": "Look back this many hours from now. "
                                         "Ignored if from/to are given."},
                "from": {"type": ["number", "null"],
                         "description": "Window start, Unix epoch seconds UTC."},
                "to": {"type": ["number", "null"],
                       "description": "Window end, Unix epoch seconds UTC."},
                "bucket": {"type": ["number", "null"],
                           "description": "Bucket size in seconds (default 300). "
                                          "The server coarsens it if the window "
                                          "would produce more than 1000 buckets "
                                          "and reports what it used in "
                                          "`bucket_seconds` - read that value "
                                          "rather than assuming yours was used."},
            },
            "required": ["zone_id", "camera_id", "hours", "from", "to", "bucket"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "cctv_zone_at_time",
        "description": (
            "What one zone read at a specific moment. Use for 'how many people "
            "were in the lobby at 3pm', or to check conditions around an "
            "incident.\n\n"
            "Returns the last reading at or before that instant - readings are "
            "stored on change, so the governing row may be much older than the "
            "instant asked about. `state.age_seconds` says how much older. If "
            "`trustworthy` is false, the reading had gone stale or the camera "
            "was offline: report it as 'the last reading, from N minutes "
            "earlier' rather than as fact.\n\n"
            "`state: null` means nothing was recorded at or before that time. "
            "Say that; do not report zero.\n\n" + _ZONE_NOTE),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string"},
                "camera_id": {"type": ["string", "null"]},
                "ts": {"type": "number",
                       "description": "The instant, Unix epoch seconds UTC."},
            },
            "required": ["zone_id", "camera_id", "ts"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "cctv_zone_duration",
        "description": (
            "How long a zone met a condition, and in how many separate "
            "episodes. Use for 'how long was the lobby over capacity', 'how "
            "long was anyone in the restricted area', 'how much of the day was "
            "it busy'.\n\n"
            "Answers in SECONDS of real time, not row counts. `episodes` lists "
            "each stretch separately and `longest_seconds` is the longest one - "
            "six separate minutes is a different story from one continuous "
            "hour, so report both when they differ.\n\n"
            "Time the camera could not observe is EXCLUDED from the total and "
            "reported in `unobserved_seconds`. When that is non-zero the honest "
            "phrasing is 'at least N minutes, and the camera missed M minutes "
            "of the period'.\n\n"
            "Give EITHER status OR the field/op/value triple. Both sets are "
            "closed; a bad name returns a 422 rather than an answer of zero.\n\n"
            + _ZONE_NOTE),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string"},
                "camera_id": {"type": ["string", "null"]},
                "hours": {"type": ["number", "null"],
                          "description": "Look back this many hours (default 24)."},
                "from": {"type": ["number", "null"]},
                "to": {"type": ["number", "null"]},
                "field": {"type": ["string", "null"],
                          "enum": ["occupancy", "density", "capacity_pct", None],
                          "description": "occupancy = people; density = people "
                                         "per m2; capacity_pct = percent of the "
                                         "zone's configured capacity."},
                "op": {"type": ["string", "null"],
                       "enum": ["gt", "gte", "lt", "lte", "eq", None]},
                "value": {"type": ["number", "null"]},
                "status": {"type": ["string", "null"],
                           "enum": ["NORMAL", "WARNING", "CRITICAL", None],
                           "description": "Use instead of field/op/value to ask "
                                          "how long the zone sat at a density "
                                          "status."},
            },
            "required": ["zone_id", "camera_id", "hours", "from", "to",
                         "field", "op", "value", "status"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "cctv_alerts",
        "description": (
            "Alerts raised in a period: overcrowding (R-01/R-02), capacity "
            "(R-03), loitering (R-05), restricted-area intrusion (R-06), camera "
            "offline (R-07). Use for 'were there any alerts', 'what happened "
            "last night', 'has the restricted area been entered'.\n\n"
            "An alert has a lifecycle: OPEN (nobody has looked), ACK (someone "
            "is on it), RESOLVED or DISMISSED (closed). Say which - 'three "
            "alerts, two still open' is the useful answer.\n\n"
            "An empty list means no alert FIRED. It does not mean nothing "
            "happened: a camera that was offline raises R-07 but records no "
            "occupancy at all, so pair this with cctv_zone_history when the "
            "question is about whether something could have been missed."),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": ["number", "null"],
                          "description": "Look back this many hours from now."},
                "from": {"type": ["number", "null"]},
                "to": {"type": ["number", "null"]},
                "camera_id": {"type": ["string", "null"]},
                "zone_id": {"type": ["string", "null"]},
                "rule_id": {"type": ["string", "null"],
                            "enum": ["R-01", "R-02", "R-03", "R-05", "R-06",
                                     "R-07", "R-08", None]},
                "severity": {"type": ["string", "null"],
                             "enum": ["WARNING", "CRITICAL", None]},
                "status": {"type": ["string", "null"],
                           "enum": ["OPEN", "ACK", "RESOLVED", "DISMISSED", None]},
                "limit": {"type": ["integer", "null"]},
            },
            "required": ["hours", "from", "to", "camera_id", "zone_id",
                         "rule_id", "severity", "status", "limit"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "cctv_occupancy_report",
        "description": (
            "Per-zone summary for a period: average and peak occupancy, "
            "density, capacity use, and alert counts. Use for 'summarise "
            "yesterday', 'which zone is busiest', 'give me last week'.\n\n"
            "`avg_occupancy` is weighted by how long each reading held, which "
            "is the correct average over a history written on change. A "
            "`sampled` block carries the older row-mean for comparison; do not "
            "quote it - it over-weights busy periods.\n\n"
            + _COVERAGE_NOTE + " Each zone also carries `gaps`, and "
            "`totals.min_coverage` is the WORST zone's coverage - if that is "
            "low, at least one camera was down and the report is partial even "
            "if the others look complete."),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": ["number", "null"]},
                "from": {"type": ["number", "null"]},
                "to": {"type": ["number", "null"]},
                "camera_id": {"type": ["string", "null"]},
                "zone_id": {"type": ["string", "null"]},
            },
            "required": ["hours", "from", "to", "camera_id", "zone_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

SYSTEM_PROMPT = """You answer questions about a physical site from its CCTV \
analytics. You have tools that read a live occupancy system; use them rather \
than guessing, and say when you cannot know something.

Four rules specific to this data:

1. NO DATA IS NOT ZERO. A null bucket, an absent zone, or `state: null` means \
the camera was not observing. Never report those as an empty room. "The camera \
was down between 2am and 6am" is a complete answer; "nobody was there" is a \
wrong one.

2. QUOTE COVERAGE WHEN IT IS PARTIAL. Below 0.95, say how much of the period \
was actually watched before giving any average.

3. A ZONE ID IS NOT UNIQUE. Every camera numbers its zones from ZONE-01. If a \
tool returns an "ambiguous" result, ask which camera rather than choosing.

4. THIS SYSTEM HOLDS NO IDENTITY. Person references are anonymous hashes that \
do not persist between sessions. You cannot answer "who" - only how many, \
where and when. Say so plainly if asked.

Times in tool arguments and results are Unix epoch seconds UTC. Durations are \
seconds; convert to minutes or hours when you answer. Prefer relative phrasing \
("about 20 minutes around 3pm") over raw timestamps."""


def route_for(name: str) -> Optional[str]:
    return TOOL_ROUTES.get(name)


def tool_names() -> List[str]:
    return [t["name"] for t in TOOLS]
