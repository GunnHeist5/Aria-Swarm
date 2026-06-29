"""main.py — core execution harness for the autonomous swarm.

Unified CLI entry point that drives the swarm's lifecycle across three modes:

  python main.py --interactive   # step through cycles, pausing for keyboard input
  python main.py --auto          # run autonomously until budget/freeze/max-cycles
  python main.py --cron          # run exactly one cycle, persist, exit (serverless)

The graph (``graph.app``) is compiled with an in-memory checkpointer, which does
NOT survive a process restart. So this harness owns explicit JSON persistence to
``~/.automaton/state_snapshot.json``: it hydrates the last snapshot on boot,
seeds it into the graph thread, runs cycles, and writes the snapshot back — the
durable contract that lets a stateless cron runner resume where it left off.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import warnings
from pathlib import Path

# Silence langgraph's internal msgpack deprecation notice emitted when a
# checkpoint round-trips the ICRStage str-enum. It's logged (not warnings.warn)
# from langgraph.checkpoint.serde.jsonplus; we persist via JSON (default=str) and
# never touch raw msgpack ourselves, so the notice is pure noise here.
logging.getLogger("langgraph.checkpoint.serde.jsonplus").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=r".*unregistered type.*")

import evolution  # noqa: E402
import registry  # noqa: E402
from graph import app  # noqa: E402 — import after the warning filter is installed
from state import new_business_state  # noqa: E402

try:  # GraphInterrupt is caught internally by Pregel in 1.2.6; guard defensively.
    from langgraph.errors import GraphInterrupt
except Exception:  # pragma: no cover - version-tolerance shim
    class GraphInterrupt(Exception):
        pass


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_DIR = Path("~/.automaton").expanduser()
STATE_FILE = STATE_DIR / "state_snapshot.json"

THREAD_ID = "swarm_production_v1"
CONFIG = {"configurable": {"thread_id": THREAD_ID}}

DEFAULT_MAX_CYCLES = 5  # local session ceiling — guards against infinite loops.

PLACEHOLDER_CREATOR_KEY = "0x000000000000000000000000000000000000dead"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_state() -> dict:
    """Hydrate the last snapshot from disk, or mint a genesis state.

    Returns a ``BusinessState``-shaped dict. On a cold boot (no snapshot) a clean
    genesis layout is created via ``new_business_state``; the creator audit key
    comes from ``CREATOR_AUDIT_KEY`` (a loud placeholder is used otherwise).
    """

    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
        print(f"[boot] hydrated state from {STATE_FILE} (cycle {state.get('cycle_count')})")
        return state

    creator_key = os.environ.get("CREATOR_AUDIT_KEY", PLACEHOLDER_CREATOR_KEY)
    if creator_key == PLACEHOLDER_CREATOR_KEY:
        print("[boot] WARNING: CREATOR_AUDIT_KEY unset — using placeholder key.")
    state = new_business_state(swarm_id=THREAD_ID, creator_audit_key=creator_key)
    print("[boot] no snapshot found — initialized fresh genesis state.")
    return state


def save_state(values: dict) -> None:
    """Serialize the latest channel data to disk as clean JSON.

    ``default=str`` resolves the ``ICRStage`` str-enum to its value and any other
    non-JSON-native object to a string, so the snapshot is always decodable.
    """

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(values, fh, indent=2, default=str)
    print(f"[persist] saved state snapshot -> {STATE_FILE}")


# ---------------------------------------------------------------------------
# Cycle execution
# ---------------------------------------------------------------------------


def run_one_cycle(working_state: dict) -> tuple[dict, bool]:
    """Run exactly one graph cycle on the production thread.

    Seeds the in-memory checkpointer from the working (disk-hydrated) state, runs
    one START->END pass, and reads back the resulting channel values. Returns
    ``(values, frozen)`` where ``frozen`` is True if a HITL gate interrupted.
    """

    working_state["cycle_count"] = working_state.get("cycle_count", 0) + 1

    # Feed the loaded disk state into graph memory (cross-process seed), then run.
    app.update_state(CONFIG, working_state)
    try:
        result = app.invoke(working_state, CONFIG)
    except GraphInterrupt:
        result = {"__interrupt__": True}

    values = app.get_state(CONFIG).values
    frozen = bool(values["hitl"]["hitl_pending"]) or "__interrupt__" in (result or {})
    return values, frozen


def print_transition(values: dict, n: int) -> None:
    """Print a one-line summary of a completed cycle."""

    fin = values["financials"]
    print(
        f"[cycle {n}] state={values['metabolic_state']:<10} "
        f"model={values['llm']['active_model']:<16} "
        f"ratio={fin['metabolic_ratio']:.3f} "
        f"icr={values['dialectic']['icr_stage']} "
        f"wallet={fin['wallet_balance_usdc']:.2f} USDC"
    )


def print_freeze_banner(hitl: dict) -> None:
    """Distinct diagnostic banner shown when the swarm freezes for HITL."""

    print(
        "\n"
        "################## ⛔ SWARM FROZEN — HITL REQUIRED ⛔ ##################\n"
        f"  reason       : {hitl.get('hitl_reason')}\n"
        f"  requires_auth: {hitl.get('requires_auth')}\n"
        f"  remote view  : {hitl.get('hitl_webhook_url')}\n"
        f"  resume with  : python resume.py {THREAD_ID} approve|reject\n"
        "######################################################################\n"
    )


# ---------------------------------------------------------------------------
# Mode drivers
# ---------------------------------------------------------------------------


def bootstrap_registry(state: dict) -> None:
    """Seed the Plasmid Registry baseline so the graph runs the evolved genome.

    Idempotent (hash dedupe): guarantees each role's `agents/*.md` baseline is in
    the registry, so the dialectic nodes resolve prompts registry-first (and pick
    up any sandbox-promoted champions) rather than always falling back to file.
    Non-fatal on failure — the graph's file fallback still runs.
    """

    swarm_id = state["evolution"]["swarm_id"]
    try:
        registry.seed_genesis_genome(swarm_id, registry.DB_PATH)

        # Horizontal Gene Transfer sync: adopt superior foreign genes from the
        # shared pool, then broadcast our own proven champions back into it.
        adopted = registry.pull_elite_genes(
            swarm_id, db_path=registry.DB_PATH, pool_path=registry.SHARED_POOL_PATH
        )
        shared = registry.broadcast_active_genome(
            swarm_id, db_path=registry.DB_PATH, pool_path=registry.SHARED_POOL_PATH
        )
        ev = state["evolution"]
        if adopted:
            ev["adopted_genes"] = sorted(
                {*ev.get("adopted_genes", []), *(a["gene_hash"] for a in adopted)}
            )
            ev["last_hgt_pull_cycle"] = state["cycle_count"]
            print(f"[hgt] pulled {len(adopted)} elite gene(s): "
                  f"{[a['role'] for a in adopted]}")
        if shared:
            ev["broadcast_genes"] = sorted(
                {*ev.get("broadcast_genes", []), *(s["gene_hash"] for s in shared)}
            )
            print(f"[hgt] broadcast {len(shared)} champion gene(s) to the pool")
        print(f"[boot] registry genome seeded for {swarm_id}")
    except Exception as exc:  # registry optional — file fallback keeps us running
        print(f"[boot] WARNING: registry bootstrap failed ({exc}); using file genome.")


def _persist_and_exit_on_freeze(values: dict) -> int:
    """Shared freeze handling: banner + persist + non-zero code."""

    print_freeze_banner(values["hitl"])
    save_state(values)
    return 2


def handle_lifecycle(values: dict) -> int | None:
    """Act on the cycle's metabolic_state. Returns a terminal exit code or None.

    * extinction  -> graceful self-termination; caller persists + exits 3.
    * replication -> spawn a mutated, funded child; parent continues (None).
    """

    mstate = values["metabolic_state"]
    if mstate == "extinction":
        evolution.extinct(values, db_path=registry.DB_PATH)
        print(
            "\n################## ☠️  SWARM EXTINCTION ☠️  ##################\n"
            f"  swarm   : {values['evolution']['swarm_id']}\n"
            f"  wallet  : {values['financials']['wallet_balance_usdc']:.2f} USDC\n"
            "  action  : graceful self-termination — final snapshot persisted\n"
            "############################################################\n"
        )
        return 3

    if mstate == "replication":
        try:
            child = evolution.replicate(
                values,
                db_path=registry.DB_PATH,
                pool_path=registry.SHARED_POOL_PATH,
                children_dir=STATE_DIR / "children",
            )
            print(
                f"[replication] spawned {child['child_swarm_id']} "
                f"(gen {child['generation']}, funded {child['funded_usdc']:.2f} USDC, "
                f"mutated {child['mutated_roles']}) -> {child['snapshot']}"
            )
        except Exception as exc:  # replication is best-effort; never crash the loop
            print(f"[replication] failed: {exc}")
    return None


def run_cron() -> int:
    """One discrete tick: hydrate -> run one cycle -> persist -> exit."""

    working = load_state()
    bootstrap_registry(working)
    try:
        values, frozen = run_one_cycle(working)
    except Exception as exc:  # never lose state on a cycle error
        print(f"[error] cycle failed: {exc}")
        save_state(working)
        return 1

    print_transition(values, working["cycle_count"])
    if frozen:
        return _persist_and_exit_on_freeze(values)
    term = handle_lifecycle(values)
    save_state(values)
    return term if term is not None else 0


def run_auto(max_cycles: int) -> int:
    """Autonomous loop until budget exhausted, freeze, or max cycles."""

    working = load_state()
    bootstrap_registry(working)
    completed = 0
    while completed < max_cycles:
        budget = working["financials"]["auto_mode_budget_usd"]
        if budget <= 0:
            print(f"[auto] budget exhausted (auto_mode_budget_usd={budget}); halting.")
            break
        try:
            values, frozen = run_one_cycle(working)
        except Exception as exc:
            print(f"[error] cycle failed: {exc}")
            save_state(working)
            return 1

        completed += 1
        print_transition(values, working["cycle_count"])
        if frozen:
            return _persist_and_exit_on_freeze(values)
        term = handle_lifecycle(values)  # extinction terminates; replication spawns
        if term is not None:
            save_state(values)
            return term
        working = values  # carry forward for the next cycle

    print(f"[auto] stopped after {completed} cycle(s).")
    save_state(working)
    return 0


def run_interactive(max_cycles: int) -> int:
    """Step through cycles, printing transitions and pausing for input."""

    working = load_state()
    bootstrap_registry(working)
    completed = 0
    while completed < max_cycles:
        try:
            values, frozen = run_one_cycle(working)
        except Exception as exc:
            print(f"[error] cycle failed: {exc}")
            save_state(working)
            return 1

        completed += 1
        print_transition(values, working["cycle_count"])
        if frozen:
            return _persist_and_exit_on_freeze(values)
        term = handle_lifecycle(values)
        if term is not None:
            save_state(values)
            return term
        working = values

        try:
            choice = input("[enter]=next cycle, q=quit > ").strip().lower()
        except EOFError:  # non-interactive stdin
            choice = "q"
        if choice == "q":
            break

    save_state(working)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Autonomous swarm execution harness."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--interactive", action="store_true",
                      help="Step through cycles, pausing for keyboard input.")
    mode.add_argument("--auto", action="store_true",
                      help="Run autonomously until budget/freeze/max-cycles.")
    mode.add_argument("--cron", action="store_true",
                      help="Run exactly one cycle, persist, and exit.")
    parser.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES,
                        help=f"Session cycle ceiling (default {DEFAULT_MAX_CYCLES}).")
    args = parser.parse_args(argv)

    if args.cron:
        return run_cron()
    if args.auto:
        return run_auto(args.max_cycles)
    return run_interactive(args.max_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
