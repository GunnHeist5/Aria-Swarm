"""main.py — core execution harness for the autonomous swarm.

Unified CLI entry point. The swarm is event-driven: operations fire on
triggers, and the clock is reserved for the metabolic heartbeat.

  python main.py --event TYPE [--payload '<json>']   # fire one trigger, persist, exit
  python main.py --cron          # sugar for --event heartbeat (systemd timer)
  python main.py --interactive   # step through ideation cycles, pausing for input
  python main.py --auto          # run autonomously until budget/freeze/max-cycles

Event types (see ``state.TriggerType``): heartbeat, ideation, seller_reply,
deal_closed, new_leads_synced, offer_accepted, contract_signed,
buyer_confirmed, venture_proposed, venture_validated, venture_killed,
wallet_low. Sensors (e.g.
Muffin's Seller Response Monitor on the same box) call ``--event`` directly —
see INTEGRATION.md. Unknown types are accepted here and fail closed in the
graph (HITL freeze), so a sensor typo can never be silently dropped.

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
from datetime import datetime, timezone
from pathlib import Path

# Silence langgraph's internal msgpack deprecation notice emitted when a
# checkpoint round-trips the ICRStage str-enum. It's logged (not warnings.warn)
# from langgraph.checkpoint.serde.jsonplus; we persist via JSON (default=str) and
# never touch raw msgpack ourselves, so the notice is pure noise here.
logging.getLogger("langgraph.checkpoint.serde.jsonplus").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=r".*unregistered type.*")

# Load a local .env into the process environment BEFORE any project import —
# graph.py / registry.py read some vars at module-import time. No-op if absent.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import bootstrap  # noqa: E402
import capital  # noqa: E402
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
    state = new_business_state(
        swarm_id=THREAD_ID,
        creator_audit_key=creator_key,
        seed_capital_usdc=_env_float("SEED_CAPITAL_USDC", 0.0),
        auto_mode_budget_usd=_env_float("AUTO_MODE_BUDGET_USD", 0.0),
        replication_threshold_usdc=_env_float("REPLICATION_THRESHOLD_USDC", 5_000.0),
    )
    print(
        f"[boot] genesis: seed {state['financials']['seed_capital_usdc']:.2f} USDC, "
        f"auto-budget {state['financials']['auto_mode_budget_usd']:.2f}, "
        f"replication@{state['financials']['replication_threshold_usdc']:.0f}"
    )
    return state


def _env_float(name: str, default: float) -> float:
    """Parse a float env var, falling back safely on missing/garbage input."""

    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[boot] WARNING: {name}={raw!r} is not a number — using {default}.")
        return default


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


def run_one_cycle(working_state: dict, event: dict | None = None) -> tuple[dict, bool]:
    """Run exactly one graph cycle on the production thread.

    Seeds the in-memory checkpointer from the working (disk-hydrated) state, runs
    one START->END pass, and reads back the resulting channel values. ``event``
    (``{"type", "payload", "received_at"}``) selects which subgraph the dispatch
    router wakes; ``None`` is a heartbeat. Returns ``(values, frozen)`` where
    ``frozen`` is True if a HITL gate interrupted.
    """

    working_state["cycle_count"] = working_state.get("cycle_count", 0) + 1
    working_state["event"] = event

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
    last_event = (values["operational_flags"].get("last_event") or {}).get("type", "?")
    print(
        f"[cycle {n}] event={last_event:<16} "
        f"state={values['metabolic_state']:<10} "
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


def bootstrap_if_needed(state: dict) -> None:
    """Run the first-run sovereignty daemon once, then persist the identity.

    No-op after the first successful boot (``first_run_complete``).
    """

    if not state["first_run_complete"]:
        bootstrap.run_bootstrap(state)
        _sync_onchain_balance(state)  # seed the real treasury if the wallet is live
        save_state(state)  # persist identity/env/creator before the first cycle


def _sync_onchain_balance(state: dict) -> None:
    """One-time (genesis) best-effort sync of the real wallet USDC balance.

    When the operational wallet came up ``ready``, overwrite ``wallet_balance_usdc``
    with the on-chain USDC balance so the metabolic/capital/lifecycle logic runs
    on the real treasury. Genesis-only so it never fights the simulated ledger
    (dividends / replication debits) on later cycles. Non-fatal.
    """

    if state["operational_flags"].get("operational_wallet") != "ready":
        return
    try:
        from tools.wallet import get_wallet_balance_usdc, initialize_wallet

        balance = get_wallet_balance_usdc(initialize_wallet())
        state["financials"]["wallet_balance_usdc"] = balance
        print(f"[boot] synced on-chain wallet balance: {balance:.2f} USDC")
    except Exception as exc:  # wallet not actually reachable — keep the env seed
        print(f"[boot] on-chain balance sync skipped ({exc}); using seed capital.")


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


def handle_capital(values: dict) -> None:
    """Run the capital-allocation rules on this cycle's newly booked revenue.

    Distributes only the *new* revenue since the last allocation (tracked via
    ``capital.distributed_revenue_usdc``) so accumulating ``revenue_generated_usdc``
    is never double-counted. Prints a line on a phase transition or a payout.
    """

    cap = values["capital"]
    fin = values["financials"]
    prev_phase = cap["capital_phase"]
    new_rev = max(0.0, fin["revenue_generated_usdc"] - cap["distributed_revenue_usdc"])

    result = capital.apply_capital_allocation(values, new_revenue_usdc=new_rev)
    cap["distributed_revenue_usdc"] = fin["revenue_generated_usdc"]

    if result["phase"] != prev_phase:
        print(f"[capital] phase: {prev_phase} -> {result['phase']}")
    if result["creator_dividend"] > 0:
        print(
            f"[capital] {result['phase']}: revenue {result['revenue']:.2f} -> "
            f"creator dividend {result['creator_dividend']:.2f} USDC, "
            f"replication pool +{result['replication_earmark']:.2f}"
        )


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


def run_event(event_type: str, payload: dict) -> int:
    """Fire one trigger: hydrate -> dispatch one invoke -> persist -> exit.

    The door every sensor uses (Muffin's monitors, webhooks, the cron timer).
    Each firing is a discrete, durable unit of work: the dispatch router wakes
    only the subgraph the event needs, then the snapshot is written back.
    """

    working = load_state()
    bootstrap_if_needed(working)
    bootstrap_registry(working)
    event = {
        "type": event_type,
        "payload": payload,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        values, frozen = run_one_cycle(working, event=event)
    except Exception as exc:  # never lose state on a cycle error
        print(f"[error] cycle failed: {exc}")
        save_state(working)
        return 1

    print_transition(values, working["cycle_count"])
    if frozen:
        return _persist_and_exit_on_freeze(values)
    handle_capital(values)
    term = handle_lifecycle(values)
    save_state(values)
    return term if term is not None else 0


def run_cron() -> int:
    """One discrete tick — sugar for ``--event heartbeat`` (systemd timer)."""

    return run_event("heartbeat", {})


def _ideation_event() -> dict:
    """A fresh ideation trigger — the auto/interactive loops run full dialectics."""

    return {
        "type": "ideation",
        "payload": {},
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


def run_auto(max_cycles: int) -> int:
    """Autonomous ideation loop until budget exhausted, freeze, or max cycles."""

    working = load_state()
    bootstrap_if_needed(working)
    bootstrap_registry(working)
    completed = 0
    while completed < max_cycles:
        budget = working["financials"]["auto_mode_budget_usd"]
        if budget <= 0:
            print(f"[auto] budget exhausted (auto_mode_budget_usd={budget}); halting.")
            break
        try:
            values, frozen = run_one_cycle(working, event=_ideation_event())
        except Exception as exc:
            print(f"[error] cycle failed: {exc}")
            save_state(working)
            return 1

        completed += 1
        print_transition(values, working["cycle_count"])
        if frozen:
            return _persist_and_exit_on_freeze(values)
        handle_capital(values)
        term = handle_lifecycle(values)  # extinction terminates; replication spawns
        if term is not None:
            save_state(values)
            return term
        working = values  # carry forward for the next cycle

    print(f"[auto] stopped after {completed} cycle(s).")
    save_state(working)
    return 0


def run_interactive(max_cycles: int) -> int:
    """Step through ideation cycles, printing transitions and pausing for input."""

    working = load_state()
    bootstrap_if_needed(working)
    bootstrap_registry(working)
    completed = 0
    while completed < max_cycles:
        try:
            values, frozen = run_one_cycle(working, event=_ideation_event())
        except Exception as exc:
            print(f"[error] cycle failed: {exc}")
            save_state(working)
            return 1

        completed += 1
        print_transition(values, working["cycle_count"])
        if frozen:
            return _persist_and_exit_on_freeze(values)
        handle_capital(values)
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
                      help="Step through ideation cycles, pausing for keyboard input.")
    mode.add_argument("--auto", action="store_true",
                      help="Run autonomously until budget/freeze/max-cycles.")
    mode.add_argument("--cron", action="store_true",
                      help="Fire one heartbeat (metabolic tick), persist, and exit.")
    mode.add_argument("--event", metavar="TYPE",
                      help="Fire one trigger (heartbeat, ideation, seller_reply, "
                           "deal_closed, new_leads_synced, offer_accepted, "
                           "contract_signed, buyer_confirmed, venture_proposed, "
                           "venture_validated, venture_killed, wallet_low), "
                           "persist, and exit.")
    parser.add_argument("--payload", default="{}",
                        help="JSON payload for --event (default: {}).")
    parser.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES,
                        help=f"Session cycle ceiling (default {DEFAULT_MAX_CYCLES}).")
    args = parser.parse_args(argv)

    if args.event:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            parser.error(f"--payload is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            parser.error("--payload must be a JSON object")
        return run_event(args.event, payload)
    if args.cron:
        return run_cron()
    if args.auto:
        return run_auto(args.max_cycles)
    return run_interactive(args.max_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
