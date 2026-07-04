"""resume.py — out-of-band Human-in-the-Loop resumption interface.

When the swarm freezes on a HITL breach (see ``graph.hitl_gate_node``), a human
reviews the alert (via the secure remote-view URL printed by ``tools.hitl``) and
then runs:

    python resume.py <thread_id> approve
    python resume.py <thread_id> reject

The swarm runs one-process-per-event with an in-process ``MemorySaver``, so the
LangGraph interrupt checkpoint dies with the process that created it — a fresh
``resume.py`` process could never see it. Therefore resume operates on the
**durable JSON snapshot** (``~/.automaton/state_snapshot.json``), which IS the
cross-process source of truth: it clears the breach flags there and records the
signed decision, so the very next event runs flag-free (main.run_one_cycle's
freeze guard reads exactly these flags). ``thread_id`` is accepted for CLI
compatibility; there is one production snapshot.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

# Load .env before project imports (main reads env at import time).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def resume_thread(thread_id: str, decision: str) -> int:
    """Clear the HITL freeze in the durable snapshot and record the decision.

    Returns 0 on a resumed freeze, 1 if the snapshot carries no pending freeze.
    """

    import main as harness  # load/save the durable snapshot

    state = harness.load_state()
    hitl = state.get("hitl", {})
    if not (hitl.get("hitl_pending") or hitl.get("requires_auth")):
        print(
            f"[resume] thread {thread_id}: no pending HITL freeze in the snapshot "
            "— nothing to resume."
        )
        return 1

    ts = datetime.now(timezone.utc).isoformat()
    prev_reason = hitl.get("hitl_reason")

    # Clear the breach flags (fail-closed -> cleared only by this explicit human
    # act) and record the signed decision. Cryptographic verification of the
    # resume signature remains a documented TODO.
    hitl["hitl_pending"] = False
    hitl["requires_auth"] = False
    hitl["hitl_reason"] = None
    hitl["pending_critical_gate_tool"] = None
    hitl["resume_signature"] = decision

    state.setdefault("error_log", []).append(
        f"HUMAN_FEEDBACK: {decision} (was: {prev_reason}) @ {ts}"
    )
    op_flags = state.setdefault("operational_flags", {})
    op_flags["last_hitl_decision"] = decision
    if decision == "reject":
        op_flags["hitl_rejected"] = True

    harness.save_state(state)
    print(
        f"[resume] thread_id={thread_id} decision={decision} -> freeze cleared "
        f"(was: {prev_reason}); the next event will run flag-free. Recorded {ts}."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resume a frozen swarm thread after human review."
    )
    parser.add_argument("thread_id", help="The frozen LangGraph thread id.")
    parser.add_argument(
        "decision",
        choices=["approve", "reject"],
        help="Explicit human authorization decision.",
    )
    args = parser.parse_args()
    return resume_thread(args.thread_id, args.decision)


if __name__ == "__main__":
    raise SystemExit(main())
