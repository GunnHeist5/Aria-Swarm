"""tools/ventures/pipeline.py — the venture lifecycle state machine.

The business-creation analogue of the wholesaling deal pipeline: a venture is a
"deal", the validation gauntlet is the "dispo clock", and apoptosis is the hard
kill. Deterministic over ``state`` + an injected ``today`` (no wall-clock
reads). The swarm's heartbeat calls ``venture_tick`` so a validation deadline or
a kill criterion can never silently slip.

Capital moves are simulated ledger entries on ``financials['wallet_balance_usdc']``
(the same pattern as ``evolution.replicate``); a real on-chain spend is gated by
the wallet layer + ``autonomy.resolve_autonomy`` and is out of scope here.
"""

from __future__ import annotations

from datetime import date

from .autonomy import (
    DEV_HANDS,
    HUMAN_GATE,
    proven_score,
    record_outcome,
    resolve_autonomy,
)
from .genome import DEFAULT_PORTFOLIO_CONFIG, PortfolioConfig, VentureGenome

# Lifecycle phases. Terminal: dead.
LIVE_PHASES = ("proposed", "validating", "live", "scaling")


def _ventures(state: dict) -> dict:
    return state["operational_flags"].setdefault("ventures", {})


def _treasury(state: dict) -> float:
    return state["financials"]["wallet_balance_usdc"]


def _debit(state: dict, amount: float) -> None:
    fin = state["financials"]
    fin["wallet_balance_usdc"] = round(fin["wallet_balance_usdc"] - amount, 6)


def _credit(state: dict, amount: float) -> None:
    fin = state["financials"]
    fin["wallet_balance_usdc"] = round(fin["wallet_balance_usdc"] + amount, 6)


def propose_venture(
    state: dict,
    genome: VentureGenome,
    *,
    venture_id: str,
    today: date,
    config: PortfolioConfig = DEFAULT_PORTFOLIO_CONFIG,
) -> dict:
    """Open a venture: resolve its autonomy, fund stage 1, start the clock.

    Returns ``{status, mode, ...}``. ``status``:
      * ``gated``     — needs a human (big spend / critical) — record held, no funding.
      * ``held``      — portfolio at capacity — not opened this tick.
      * ``opened``    — funded stage 1, phase ``validating``.
      * ``duplicate`` — id already exists.
    """

    ventures = _ventures(state)
    if venture_id in ventures:
        return {"status": "duplicate", "venture_id": venture_id}

    live = [v for v in ventures.values() if v["phase"] in LIVE_PHASES]
    if len(live) >= config.max_concurrent:
        return {"status": "held", "venture_id": venture_id, "reason": "portfolio_full"}
    # Generalist niche diversity: don't over-concentrate in one kind — a niche
    # that dries up would otherwise take the whole cohort down together.
    if sum(1 for v in live if v["kind"] == genome.kind) >= config.max_per_kind:
        return {"status": "held", "venture_id": venture_id, "reason": "kind_saturated"}

    stage0 = genome.stage_budgets[0]
    cost = stage0["budget_usd"]
    decision = resolve_autonomy(
        action="launch_venture",
        cost_usd=cost,
        treasury_usd=_treasury(state),
        proven_score=proven_score(state, genome.kind),
        per_call_cap_usd=state["financials"]["spend_per_call_cap_usdc"],
        per_day_cap_usd=state["financials"]["spend_per_day_cap_usdc"],
        spent_today_usd=state["financials"]["spent_today_usdc"],
        config=config,
    )

    record = {
        "kind": genome.kind,
        "hypothesis": genome.hypothesis,
        "opened": today.isoformat(),
        "phase": "proposed",
        "stage_index": 0,
        "autonomy": decision["mode"],
        "autonomy_reason": decision["reason"],
        "genome": {
            "kind": genome.kind,
            "seed_cap_usd": genome.seed_cap_usd,
            "stage_budgets": [dict(s) for s in genome.stage_budgets],
            "kill_criteria": dict(genome.kill_criteria),
            "parent_ids": list(genome.parent_ids),
        },
        "capital_committed_usd": 0.0,
        "capital_spent_usd": 0.0,
        "signal": 0.0,
        "revenue_usd": 0.0,
        "log": [],
    }
    ventures[venture_id] = record

    if decision["mode"] == HUMAN_GATE:
        record["log"].append(f"{today.isoformat()}: proposed — HITL gate ({decision['reason']})")
        state["error_log"].append(
            f"VENTURE_GATE: {venture_id} [{genome.kind}] needs approval ({decision['reason']})"
        )
        return {"status": "gated", "venture_id": venture_id, "mode": HUMAN_GATE,
                "reason": decision["reason"]}

    # dev_hands or autonomous -> fund stage 1 and start validating.
    _debit(state, cost)
    record["phase"] = "validating"
    record["capital_committed_usd"] = round(cost, 6)
    record["log"].append(
        f"{today.isoformat()}: funded stage 'validate' ${cost:.2f} via {decision['mode']}"
    )
    state["error_log"].append(
        f"VENTURE_OPENED: {venture_id} [{genome.kind}] stage validate ${cost:.2f} ({decision['mode']})"
    )
    return {"status": "opened", "venture_id": venture_id, "mode": decision["mode"]}


def record_metrics(
    state: dict, venture_id: str, *, signal: float | None = None,
    revenue_usd: float | None = None, spent_usd: float | None = None,
) -> dict:
    """Feed observed metrics into a venture (from the hands / real world)."""

    record = _ventures(state).get(venture_id)
    if record is None:
        return {"status": "unknown_venture", "venture_id": venture_id}
    if signal is not None:
        record["signal"] = float(signal)
    if revenue_usd is not None:
        record["revenue_usd"] = round(float(revenue_usd), 6)
    if spent_usd is not None:
        record["capital_spent_usd"] = round(record["capital_spent_usd"] + float(spent_usd), 6)
    return {"status": "recorded", "venture_id": venture_id}


def _kill(state: dict, venture_id: str, record: dict, reason: str, today: date) -> dict:
    """Apoptosis: mark dead, reclaim unspent committed capital, record the loss."""

    unspent = max(0.0, record["capital_committed_usd"] - record["capital_spent_usd"])
    if unspent > 0:
        _credit(state, unspent)
    record["phase"] = "dead"
    record["death_reason"] = reason
    record["reclaimed_usd"] = round(unspent, 6)
    record["log"].append(f"{today.isoformat()}: APOPTOSIS ({reason}) — reclaimed ${unspent:.2f}")
    record_outcome(state, record["kind"], win=False)
    state["error_log"].append(
        f"VENTURE_KILLED: {venture_id} [{record['kind']}] {reason} — reclaimed ${unspent:.2f}"
    )
    return {"deal_id": venture_id, "action": "apoptosis", "reason": reason, "reclaimed_usd": round(unspent, 6)}


def _days_alive(record: dict, today: date) -> int:
    return (today - date.fromisoformat(record["opened"])).days


def _check_apoptosis(record: dict, today: date) -> str | None:
    """Return a kill reason if any criterion trips, else None."""

    kc = record["genome"]["kill_criteria"]
    day = _days_alive(record, today)
    if "max_days_alive" in kc and day >= kc["max_days_alive"]:
        return "max_days_alive"
    if "no_revenue_by_day" in kc and day >= kc["no_revenue_by_day"] and record["revenue_usd"] <= 0:
        return "no_revenue"
    # Validation-signal floor only bites once past the first (cheap) window.
    if "min_validation_signal" in kc and day >= 7 and record["signal"] < kc["min_validation_signal"]:
        return "weak_signal"
    return None


def venture_tick(
    state: dict, today: date, config: PortfolioConfig = DEFAULT_PORTFOLIO_CONFIG
) -> list[dict]:
    """Heartbeat entrypoint: apoptosis, stage-gate advancement, portfolio scale.

    Returns the list of actions taken/surfaced this tick (also stashed in
    ``operational_flags['venture_actions_due']``).
    """

    actions: list[dict] = []
    for venture_id, record in _ventures(state).items():
        if record["phase"] not in LIVE_PHASES:
            continue

        # 1. Apoptosis first — a dying venture advances no stages.
        reason = _check_apoptosis(record, today)
        if reason:
            actions.append(_kill(state, venture_id, record, reason, today))
            continue

        # 2. Stage-gate advancement: next stage funds only if the gate passes.
        stages = record["genome"]["stage_budgets"]
        idx = record["stage_index"]
        if idx < len(stages):
            gate = stages[idx].get("gate_signal", 0.0)
            if record["signal"] >= gate and idx + 1 < len(stages):
                nxt = stages[idx + 1]
                cost = nxt["budget_usd"]
                decision = resolve_autonomy(
                    action="fund_stage",
                    cost_usd=cost,
                    treasury_usd=_treasury(state),
                    proven_score=proven_score(state, record["kind"]),
                    per_call_cap_usd=state["financials"]["spend_per_call_cap_usdc"],
                    per_day_cap_usd=state["financials"]["spend_per_day_cap_usdc"],
                    spent_today_usd=state["financials"]["spent_today_usdc"],
                    config=config,
                )
                if decision["mode"] == HUMAN_GATE:
                    if "stage_gate_pending" not in record:
                        record["stage_gate_pending"] = nxt["stage"]
                        record["log"].append(
                            f"{today.isoformat()}: stage '{nxt['stage']}' needs approval "
                            f"({decision['reason']})"
                        )
                        actions.append({"deal_id": venture_id, "action": "stage_gate",
                                        "stage": nxt["stage"], "reason": decision["reason"]})
                else:
                    _debit(state, cost)
                    record["stage_index"] = idx + 1
                    record["capital_committed_usd"] = round(record["capital_committed_usd"] + cost, 6)
                    record["phase"] = "scaling" if nxt["stage"] == "scale" else "live"
                    record.pop("stage_gate_pending", None)
                    record["log"].append(
                        f"{today.isoformat()}: advanced to '{nxt['stage']}' +${cost:.2f} ({decision['mode']})"
                    )
                    actions.append({"deal_id": venture_id, "action": "stage_advance",
                                    "stage": nxt["stage"], "cost_usd": cost})

        # 3. A revenue-positive venture past validation is a proven win — this
        # is what graduates a venture-kind toward full autonomy (rule C).
        if (record["phase"] in ("live", "scaling") and record["revenue_usd"] > 0
                and not record.get("counted_win")):
            record["counted_win"] = True
            record_outcome(state, record["kind"], win=True)
            actions.append({"deal_id": venture_id, "action": "proven_win", "kind": record["kind"]})

    if actions:
        state["operational_flags"]["venture_actions_due"] = actions
    return actions


def resolve_venture(state: dict, venture_id: str, outcome: str, today: date) -> dict:
    """Terminate a venture: ``graduated`` (success) or ``killed`` (manual)."""

    if outcome not in ("graduated", "killed"):
        raise ValueError(f"outcome must be graduated|killed, got {outcome!r}")
    record = _ventures(state).get(venture_id)
    if record is None:
        return {"status": "unknown_venture", "venture_id": venture_id}
    if outcome == "killed":
        _kill(state, venture_id, record, "manual", today)
        return {"status": "killed", "venture_id": venture_id}
    record["phase"] = "dead"
    record["death_reason"] = "graduated"
    record_outcome(state, record["kind"], win=True)
    record["log"].append(f"{today.isoformat()}: GRADUATED — venture succeeded")
    state["error_log"].append(f"VENTURE_GRADUATED: {venture_id} [{record['kind']}]")
    return {"status": "graduated", "venture_id": venture_id}
