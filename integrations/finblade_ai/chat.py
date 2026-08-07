"""Executing the chatbot's tool calls against the CCTV API.

tools.py declares what the model may ask for; this turns one of those calls
into an HTTP request through CCTVClient, so the allowlist, the API key handling
and the response cache apply to the chatbot exactly as they do to the tiles.

WHY A DISPATCH TABLE AND NOT A URL BUILT FROM THE TOOL NAME. The model chooses
the arguments. Mapping name -> handler means an argument it invents cannot
become part of a path, and a tool that does not exist is a KeyError here rather
than a request to a URL nobody designed.

WHAT THIS DELIBERATELY DOES NOT DO. It does not summarise, round, or drop
fields before the model sees them. `coverage`, `gaps`, nulls and the `stale`
flag are the parts that stop a confident wrong answer, and a helpful layer that
tidied them away would remove exactly the information the tool descriptions
tell the model to look for.

run_turn() at the bottom is a complete worked example against the Anthropic
SDK. FinBlade can use it as-is or lift the dispatch and drive it from their own
agent loop.
"""

import time
from typing import Any, Dict, Optional

from .cctv_client import CCTVClient, CCTVError
from .tools import SYSTEM_PROMPT, TOOLS

# Cache TTLs per tool. History does not change once written, so it is cached far
# longer than live state — a conversation that asks three follow-up questions
# about yesterday makes one upstream call, not three.
HISTORY_TTL = 60.0
LIVE_TTL = 5.0


def _window(args: Dict[str, Any], default_hours: float) -> Dict[str, Any]:
    """from/to, however the model chose to express it.

    A model asked about "yesterday" may send epoch bounds, or `hours`, or
    nothing at all. All three are legitimate; guessing wrong is not, so an
    explicit from/to always wins and `hours` is only a fallback.
    """
    frm, to = args.get("from"), args.get("to")
    if frm is not None or to is not None:
        return {k: v for k, v in (("from", frm), ("to", to)) if v is not None}
    hours = args.get("hours")
    if hours is None:
        hours = default_hours
    now = time.time()
    return {"from": now - float(hours) * 3600.0, "to": now}


def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
    """Drop nulls.

    Every tool schema is `strict`, which means the model must send every
    property — so it sends null for the ones it does not want. Forwarding those
    as literal "None" query strings would filter on the string "None" and
    return nothing, which looks like a real empty answer.
    """
    return {k: v for k, v in params.items() if v is not None}


def _zone_path(args: Dict[str, Any], suffix: str) -> str:
    zone_id = str(args["zone_id"])
    # The zone id reaches a URL path, and the model chose it. Path separators
    # and traversal are the only characters that could change which route is
    # hit, and no real zone id contains them.
    if "/" in zone_id or "\\" in zone_id or ".." in zone_id:
        raise CCTVError(f"invalid zone_id {zone_id!r}")
    return f"/api/v1/zones/{zone_id}/{suffix}"


def run_tool(client: CCTVClient, name: str, args: Dict[str, Any]) -> Any:
    """Execute one tool call. Returns the raw response body."""
    args = args or {}

    if name == "cctv_live_state":
        return client.read("zone_state", **_clean({
            "camera_id": args.get("camera_id")}))

    if name == "cctv_zone_history":
        params = dict(_window(args, 6.0), **_clean({
            "camera_id": args.get("camera_id"),
            "bucket": args.get("bucket") or 300}))
        return client.read_path(_zone_path(args, "series"), ttl=HISTORY_TTL,
                                **params)

    if name == "cctv_zone_at_time":
        return client.read_path(
            _zone_path(args, "at"), ttl=HISTORY_TTL,
            **_clean({"ts": args.get("ts") or time.time(),
                      "camera_id": args.get("camera_id")}))

    if name == "cctv_zone_duration":
        params = dict(_window(args, 24.0), **_clean({
            "camera_id": args.get("camera_id"),
            "field": args.get("field"), "op": args.get("op"),
            "value": args.get("value"), "status": args.get("status")}))
        # status and field/op/value are alternatives; sending both lets status
        # win server-side, which would silently ignore a condition the model
        # meant. Better to send only what it actually chose.
        if params.get("status"):
            for k in ("field", "op", "value"):
                params.pop(k, None)
        return client.read_path(_zone_path(args, "duration"), ttl=HISTORY_TTL,
                                **params)

    if name == "cctv_alerts":
        return client.read("history_alerts", **dict(_window(args, 24.0), **_clean({
            "camera_id": args.get("camera_id"), "zone_id": args.get("zone_id"),
            "rule_id": args.get("rule_id"), "severity": args.get("severity"),
            "status": args.get("status"), "limit": args.get("limit") or 100})))

    if name == "cctv_occupancy_report":
        return client.read("report_json", **dict(_window(args, 24.0), **_clean({
            "camera_id": args.get("camera_id"),
            "zone_id": args.get("zone_id")})))

    raise CCTVError(f"unknown tool {name!r}")


def run_tool_safely(client: CCTVClient, name: str, args: Dict[str, Any]) -> Any:
    """run_tool, with failures turned into something the model can act on.

    An exception here would end the turn. A described failure lets the model
    say "I could not reach the camera system" instead, which is a better answer
    than a stack trace and an honest one — the alternative is answering from
    nothing.
    """
    try:
        return run_tool(client, name, args)
    except CCTVError as exc:
        return {"error": str(exc),
                "note": "The CCTV system could not be read. Tell the user you "
                        "could not retrieve this; do not answer from memory or "
                        "guess a number."}


# --------------------------------------------------------------------------
# Worked example. Everything above is transport-agnostic and testable without
# an API key; this is the part that talks to Claude.

MODEL = "claude-opus-5"


MAX_TOOL_ROUNDS = 8


def run_turn(question: str, client: Optional[CCTVClient] = None,
             model: str = MODEL, max_tokens: int = 2048,
             history: Optional[list] = None):
    """Answer one question, running tool calls until the model is done.

    An explicit request -> execute -> continue loop against messages.create,
    rather than the SDK's tool runner. Two reasons, and the second is the real
    one:

      * The runner executes CALLABLE tools it holds in-process. These tools are
        HTTP reads against another service, so there is nothing for it to call
        — the work happens here either way.
      * This loop is the part FinBlade replaces. Their agent framework already
        has a turn loop, an audit trail and a user identity to attribute reads
        to; showing the exchange explicitly makes it obvious where their code
        goes. A helper would hide precisely the seam they need.

    Requires ANTHROPIC_API_KEY, plus CCTV_BASE_URL and CCTV_API_KEY for the
    client. Use the scoped FINBLADE_INTEGRATION_KEY, not the operator key — the
    chatbot needs to read and nothing else, and the scoped key cannot
    acknowledge an alert or delete data even if this file is edited.

    Returns (answer_text, messages) so a caller can keep the conversation.
    """
    from anthropic import Anthropic

    client = client or CCTVClient()
    anthropic = Anthropic()
    messages = list(history or []) + [{"role": "user", "content": question}]

    for _ in range(MAX_TOOL_ROUNDS):
        response = anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            # These questions routinely chain two or three calls — find the
            # zone, read its history, then check the alerts around what it
            # found. The planning is what makes that a coherent sequence rather
            # than three unrelated lookups.
            thinking={"type": "adaptive"},
        )
        messages.append({"role": "assistant", "content": response.content})

        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            text = "".join(b.text for b in response.content if b.type == "text")
            return text, messages

        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id,
             "content": _as_text(run_tool_safely(client, call.name, call.input))}
            for call in calls]})

    # Not silent. A model looping on tool calls has usually misunderstood the
    # question, and returning a partial answer as if it were complete is worse
    # than saying the turn did not finish.
    raise CCTVError(f"gave up after {MAX_TOOL_ROUNDS} rounds of tool calls")


def _as_text(result: Any) -> str:
    import json
    return json.dumps(result, default=str)
