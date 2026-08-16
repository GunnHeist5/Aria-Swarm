"""Background execution + the live activity feed.

run_analysis_task / run_gym_set_task are plain synchronous functions — routes
spawn them on daemon threads via spawn(); tests call them directly (and
monkeypatch spawn to inline). llm_factory / grader_factory are test hooks for
injecting scripted LLMs into route-spawned runs.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app import config
from app.agent import analyst
from app.db import control, gym, tenant as tdb

# Test hooks: when set, background tasks build their LLMs from these.
llm_factory = None
grader_factory = None

_TOOL_PHRASES = {
    "list_datasets": "Looking at available datasets",
    "read_dataset_schema": "Reading dataset structure",
    "run_python": "Running analysis code",
    "search_methods": "Consulting the methods library",
    "note_learning": "Noting an insight",
}

_LIFECYCLE = {
    "done": "Analysis complete",
    "failed": "Analysis failed",
    "refused": "Analysis refused",
}


def friendly_phrase(event: dict) -> tuple[str, str] | None:
    """Map a raw tool event to a (kind, human text) feed line, or None to skip."""
    if event.get("type") == "tool_use":
        if event.get("name") == "save_deliverable":
            title = (event.get("input") or {}).get("title") or "deliverable"
            return ("step", f"Saving deliverable: {title}")
        return ("step", _TOOL_PHRASES.get(event.get("name"), f"Using {event.get('name')}"))
    if event.get("type") == "tool_result" and event.get("is_error"):
        return ("error", "Hit an error, adjusting approach")
    return None


def make_event_bridge(tenant_id: str, analysis_id: str):
    def bridge(event: dict) -> None:
        try:
            mapped = friendly_phrase(event)
            if mapped:
                tdb.add_analysis_event(tenant_id, analysis_id, mapped[0], mapped[1])
        except Exception:
            pass  # the feed must never kill an analysis

    return bridge


def is_stale(last_activity_iso: str) -> bool:
    try:
        last = datetime.fromisoformat(last_activity_iso)
    except (ValueError, TypeError):
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() > config.STALE_AFTER_S


def analysis_live_status(tenant_id: str, analysis: dict) -> str:
    """running | stale | done | failed | refused — read-only judgment."""
    if analysis["status"] != "running":
        return analysis["status"]
    events = tdb.list_analysis_events(tenant_id, analysis["analysis_id"])
    last = events[-1]["created_at"] if events else analysis["created_at"]
    return "stale" if is_stale(last) else "running"


def run_analysis_task(tenant_id: str, question: str, conversation_id: str | None,
                      analysis_id: str, key_id: str) -> None:
    llm = llm_factory() if llm_factory else None
    tdb.add_analysis_event(tenant_id, analysis_id, "lifecycle", "Analysis started")
    try:
        result = analyst.run_analysis(
            tenant_id, question, conversation_id, llm=llm,
            on_tool_event=make_event_bridge(tenant_id, analysis_id),
            analysis_id=analysis_id,
        )
        tdb.add_analysis_event(tenant_id, analysis_id, "lifecycle",
                               _LIFECYCLE.get(result.status, "Analysis finished"))
        control.audit(key_id, "analysis_finish", tenant_id=tenant_id,
                      resource=analysis_id, detail={"status": result.status})
    except Exception as exc:
        tdb.finish_analysis(tenant_id, analysis_id, "failed", str(exc)[:500])
        tdb.add_analysis_event(tenant_id, analysis_id, "lifecycle", "Analysis failed")
        control.audit(key_id, "analysis_finish", tenant_id=tenant_id,
                      resource=analysis_id, detail={"status": "failed"})


def run_gym_set_task(set_id: str, limit: int, run_id: str, key_id: str) -> None:
    from app.gym import runner

    llm = llm_factory() if llm_factory else None
    grader = grader_factory() if grader_factory else None
    try:
        result = runner.run_set(set_id, limit=limit, run_id=run_id, llm=llm, grader=grader)
        control.audit(key_id, "gym_run_finished", resource=run_id,
                      detail={"attempted": result["attempted"], "remaining": result["remaining"]})
    except Exception as exc:
        control.audit(key_id, "gym_run_finished", resource=run_id,
                      detail={"error": str(exc)[:200]})


def spawn(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()
