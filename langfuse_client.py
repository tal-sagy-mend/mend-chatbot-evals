"""
Langfuse API wrapper — fetch traces and post scores.
Uses Langfuse Python SDK 3.x.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterator

from langfuse import Langfuse
from langfuse.api.resources.score.types.create_score_request import CreateScoreRequest

from config import (
    LANGFUSE_HOST,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
    LANGFUSE_USER_ID,
    PRODUCTION_LOOKBACK_HOURS,
)


def get_client() -> Langfuse:
    return Langfuse(
        public_key=LANGFUSE_PUBLIC_KEY,
        secret_key=LANGFUSE_SECRET_KEY,
        host=LANGFUSE_HOST,
    )


# ---------------------------------------------------------------------------
# Trace extraction helpers
# ---------------------------------------------------------------------------

def extract_question(trace) -> str:
    """
    Extract the user question from a Langfuse trace.
    Handles dict input ({"content": "..."}) and plain strings.
    """
    inp = trace.input
    if isinstance(inp, str):
        return inp.strip()
    if isinstance(inp, dict):
        for key in ("content", "question", "message", "text", "input"):
            if key in inp:
                val = inp[key]
                return val.strip() if isinstance(val, str) else str(val)
    return str(inp) if inp else ""


def extract_response(trace) -> str:
    """
    Extract the bot response from a Langfuse trace.
    Handles dict output ({"content": "..."}) and plain strings.
    """
    out = trace.output
    if isinstance(out, str):
        return out.strip()
    if isinstance(out, dict):
        for key in ("content", "answer", "response", "message", "text", "output"):
            if key in out:
                val = out[key]
                return val.strip() if isinstance(val, str) else str(val)
    return str(out) if out else ""


AGENT_NODES = {"docs_agent", "ui_agent", "api_agent"}


def extract_steps(trace, client: Langfuse | None = None) -> str | None:
    """
    Extract agent routing from trace observations.
    Looks for langgraph_node values matching known agent names across all observations.
    Falls back to legacy trace metadata keys if observations are unavailable.
    """
    if client is not None:
        try:
            obs_result = client.api.observations.get_many(trace_id=trace.id, limit=100)
            nodes = []
            for obs in obs_result.data or []:
                node = (obs.metadata or {}).get("langgraph_node", "")
                if node in AGENT_NODES and node not in nodes:
                    nodes.append(node)
            if nodes:
                return " ".join(nodes)
        except Exception:
            pass
    # Fallback: legacy metadata keys
    meta = trace.metadata or {}
    return meta.get("steps") or meta.get("agent_steps") or meta.get("step_names")


def extract_judge_decision(trace) -> bool | None:
    """Extract judge_decision from trace metadata."""
    meta = trace.metadata or {}
    val = meta.get("judge_decision")
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


# ---------------------------------------------------------------------------
# Trace fetching
# ---------------------------------------------------------------------------

def fetch_recent_traces(
    client: Langfuse,
    hours_back: int = PRODUCTION_LOOKBACK_HOURS,
    page_size: int = 50,
    max_pages: int = 10,
) -> Iterator:
    """
    Yield all traces for the eval user from the last N hours.
    Paginates automatically up to max_pages.
    """
    from_ts = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    page = 1

    while page <= max_pages:
        result = client.api.trace.list(
            user_id=LANGFUSE_USER_ID,
            from_timestamp=from_ts,
            limit=page_size,
            page=page,
        )
        traces = result.data
        if not traces:
            break
        yield from traces
        if len(traces) < page_size:
            break
        page += 1


def fetch_traces_for_session(client: Langfuse, session_id: str) -> list:
    """Fetch all traces belonging to a specific conversation (session_id = conversation_uuid)."""
    result = client.api.trace.list(session_id=session_id, limit=50)
    return result.data


# ---------------------------------------------------------------------------
# Score posting
# ---------------------------------------------------------------------------

def post_score(
    client: Langfuse,
    trace_id: str,
    name: str,
    value: float,
    comment: str = "",
) -> None:
    """Post a numeric score to a Langfuse trace."""
    client.api.score.create(
        request=CreateScoreRequest(
            trace_id=trace_id,
            name=name,
            value=value,
            comment=comment or None,
        )
    )


def post_scores(
    client: Langfuse,
    trace_id: str,
    scores: dict[str, tuple[float, str]],
) -> None:
    """Post multiple scores to a single trace."""
    for name, (value, comment) in scores.items():
        post_score(client, trace_id, name, value, comment)
