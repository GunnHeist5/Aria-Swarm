"""resume.py — out-of-band Human-in-the-Loop resumption interface.

When the swarm freezes on a HITL breach (see ``graph.hitl_gate_node``), a human
reviews the alert (via the secure remote-view URL printed by ``tools.hitl``) and
then runs:

    python resume.py <thread_id> approve
    python resume.py <thread_id> reject

This fetches the frozen checkpoint, clears the breach flags, records the human
decision into the durable state history, and signals LangGraph to continue from
exactly where it froze.

IMPORTANT — checkpointer durability:
    ``graph.build_graph`` defaults to ``MemorySaver``, which is **in-process
    only**. A checkpoint created by a long-running daemon is NOT visible to this
    script when run as a separate process. For genuinely out-of-band resume,
    compile the graph with a durable checkpointer and point this script at the
    same store, e.g.:

        from langgraph.checkpoint.sqlite import SqliteSaver
        cp = SqliteSaver.from_conn_string("swarm_checkpoints.sqlite")
        app = build_graph(checkpointer=cp)

    (Requires ``pip install langgraph-checkpoint-sqlite``.) The resume logic
    below is identical regardless of checkpointer backend.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

# Load .env before project imports (graph reads some env at import time).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from langgraph.types import Command

from graph import app


def resume_thread(thread_id: str, decision: str) -> int:
    """Clear the HITL freeze on ``thread_id`` and resume the graph.

    Returns a process exit code (0 on success, non-zero if there is no frozen
    state for the thread).
    """

    config = {"configurable": {"thread_id": thread_id}}
    snapshot = app.get_state(config)

    if not snapshot.values:
        print(
            f"[resume] No checkpoint found for thread_id={thread_id!r}.\n"
            "         With the default in-memory checkpointer a fresh process "
            "cannot see another process's state — see the SqliteSaver note in "
            "resume.py.",
            file=sys.stderr,
        )
        return 1

    values = snapshot.values
    ts = datetime.now(timezone.utc).isoformat()

    # Clear the breach flags (fail-closed -> cleared only by explicit human act).
    hitl = dict(values["hitl"])
    hitl["hitl_pending"] = False
    hitl["requires_auth"] = False
    hitl["hitl_reason"] = None
    hitl["resume_signature"] = decision  # recorded; cryptographic verify is TODO

    # Append the human feedback to the durable state history.
    error_log = list(values.get("error_log", []))
    error_log.append(f"HUMAN_FEEDBACK: {decision} @ {ts}")

    op_flags = dict(values.get("operational_flags", {}))
    op_flags["last_hitl_decision"] = decision
    if decision == "reject":
        op_flags["hitl_rejected"] = True

    app.update_state(
        config,
        {"hitl": hitl, "error_log": error_log, "operational_flags": op_flags},
    )

    # Signal LangGraph to continue from the frozen interrupt.
    app.invoke(Command(resume=decision), config)

    resumed = app.get_state(config)
    status = "COMPLETE" if resumed.next == () else f"running -> next={resumed.next}"
    print(
        f"[resume] thread_id={thread_id} decision={decision} -> {status}\n"
        f"         flags cleared; feedback recorded at {ts}"
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
