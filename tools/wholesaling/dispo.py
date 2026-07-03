"""tools/wholesaling/dispo.py — the 10-day disposition timeline engine.

Encodes the buyer-disposition playbook as a deterministic dated state machine:

  Day 0  BLAST      — hit all buyer platforms in parallel, never sequential
  Day 1  FOLLOWUP   — re-touch every platform
  Day 3  ESCALATE   — "interested buyers only — 7 days to close"
  Day 5  MAXDISPO   — insurance gate, HARD: MaxDispo needs 5 working days;
                      contacting them on Day 7 is already too late
  Day 9  DECISION   — buyer confirmed (earnest posted) or prepare cancellation
  Day 10 DEADLINE   — resolve or kill via the option/inspection contingency

The swarm's heartbeat calls ``dispo_tick`` so a deadline can never silently
slip — humans forget Day 5; a metabolic tick doesn't. The engine TRACKS and
ALERTS; executing blasts, contracts, cancellations, and wires stays with the
human/operator layer (CRITICAL_GATE unchanged).

Pure functions over ``state`` + an injected ``today`` (no wall-clock reads —
deterministic and testable). Timeline parameters are genome, mutable by the
evolution layer like every other strategy number.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date


@dataclass(frozen=True)
class DispoConfig:
    """Genome parameters for the disposition timeline (days since signing)."""

    platforms: tuple = (
        "sheet_db",           # own Google Sheets buyer database — fastest
        "offmarket_io",       # Off-Market.io wholesale marketplace
        "housecashin",        # HouseCashin cash buyer network
        "real_estate_bees",   # Real Estate Bees investor network
    )
    followup_day: int = 1
    escalation_day: int = 3
    maxdispo_gate_day: int = 5    # HARD gate — do not delay past this day
    decision_day: int = 9
    hard_deadline_day: int = 10
    wire_alert_business_days: int = 2  # wire overdue threshold after closing

    def mutate(self, **changes) -> "DispoConfig":
        """Return a new config with overrides applied (the mutation operator)."""

        return replace(self, **changes)


DEFAULT_DISPO_CONFIG = DispoConfig()

# Phases a dispo record moves through. Terminal: closed / cancelled.
ACTIVE_PHASES = ("blast", "marketing", "wire_pending", "cancel_pending")


def _records(state: dict) -> dict:
    return state["operational_flags"].setdefault("dispo", {})


def start_dispo(
    state: dict,
    *,
    deal_id: str,
    contract_signed_date: str,
    address: str | None = None,
    arv: float | None = None,
    offer_price: float | None = None,
    assignment_fee_target: float | None = None,
) -> dict:
    """Open the 10-day clock for a signed contract. Idempotent per deal_id."""

    records = _records(state)
    if deal_id in records:
        return {"status": "duplicate", "deal_id": deal_id}

    date.fromisoformat(contract_signed_date)  # validate early, fail loudly
    records[deal_id] = {
        "signed": contract_signed_date,
        "phase": "blast",
        "buyer": None,
        "buyer_confirmed": False,
        "maxdispo_contacted": False,
        "address": address,
        "arv": arv,
        "offer_price": offer_price,
        "assignment_fee_target": assignment_fee_target,
        "actions_done": [],
        "log": [f"{contract_signed_date}: dispo opened (day 0 blast due)"],
    }
    state["error_log"].append(
        f"DISPO_OPENED: {deal_id} signed {contract_signed_date} — "
        f"blast all platforms TODAY"
    )
    return {"status": "opened", "deal_id": deal_id}


def dispo_day(record: dict, today: date) -> int:
    """Calendar days since signing (signing day = Day 0)."""

    return (today - date.fromisoformat(record["signed"])).days


def dispo_tick(
    state: dict, today: date, config: DispoConfig = DEFAULT_DISPO_CONFIG
) -> list[dict]:
    """Evaluate every active dispo deal against the timeline. Returns due actions.

    Each action fires exactly once per deal (tracked in ``actions_done``).
    A gate first evaluated AFTER its day is flagged ``*_MISSED`` — loud, not
    silent — because "contact MaxDispo on Day 7" is not a plan, it's a failure.
    """

    actions: list[dict] = []
    for deal_id, record in _records(state).items():
        if record["phase"] not in ACTIVE_PHASES or record["phase"] == "wire_pending":
            continue
        day = dispo_day(record, today)
        due = []

        if day >= 0:
            due.append(("blast", f"blast all {len(config.platforms)} platforms in parallel"))
        if day >= config.followup_day:
            due.append(("followup", "re-touch every platform (medium urgency)"))
        if day >= config.escalation_day:
            due.append(("escalate", "hard escalation: interested buyers only, 7 days to close"))
        if day >= config.maxdispo_gate_day and not record["buyer_confirmed"]:
            if not record["maxdispo_contacted"]:
                name = (
                    "maxdispo_gate" if day == config.maxdispo_gate_day
                    else "maxdispo_gate_MISSED"
                )
                due.append((name, "send MaxDispo the full deal package NOW (baseline bid in writing)"))
        if day >= config.decision_day and not record["buyer_confirmed"]:
            due.append(("decision_point", "EOD: buyer w/ earnest confirmed OR prepare cancellation notice"))
        if day >= config.hard_deadline_day and not record["buyer_confirmed"]:
            due.append(("hard_deadline", "resolve or cancel via option period TODAY — no extensions"))
            record["phase"] = "cancel_pending"

        for name, detail in due:
            base = name.replace("_MISSED", "")
            if any(done.replace("_MISSED", "") == base for done in record["actions_done"]):
                continue
            record["actions_done"].append(name)
            record["log"].append(f"{today.isoformat()} (day {day}): {name} — {detail}")
            state["error_log"].append(
                f"DISPO_{name.upper()}: {deal_id} day {day} — {detail}"
            )
            actions.append(
                {"deal_id": deal_id, "day": day, "action": name, "detail": detail}
            )

    if actions:
        state["operational_flags"]["dispo_actions_due"] = actions
    return actions


def confirm_buyer(
    state: dict, *, deal_id: str, buyer: str, earnest_posted: bool
) -> dict:
    """Record a confirmed buyer. Only earnest money makes it real."""

    record = _records(state).get(deal_id)
    if record is None:
        return {"status": "unknown_deal", "deal_id": deal_id}
    if not earnest_posted:
        record["log"].append(f"buyer {buyer!r} interested but NO earnest — not confirmed")
        state["error_log"].append(
            f"DISPO_BUYER_SOFT: {deal_id} — {buyer!r} has not posted earnest; clock still running"
        )
        return {"status": "not_confirmed", "deal_id": deal_id}

    record["buyer"] = buyer
    record["buyer_confirmed"] = True
    record["phase"] = "wire_pending"
    record["log"].append(f"buyer confirmed: {buyer} (earnest posted) — wire pending")
    state["error_log"].append(
        f"DISPO_BUYER_CONFIRMED: {deal_id} — {buyer}; send wire instructions via title company"
    )
    return {"status": "confirmed", "deal_id": deal_id, "buyer": buyer}


def resolve_dispo(state: dict, deal_id: str, outcome: str) -> dict:
    """Close out a dispo record: ``closed`` (funded) or ``cancelled`` (killed)."""

    if outcome not in ("closed", "cancelled"):
        raise ValueError(f"outcome must be closed|cancelled, got {outcome!r}")
    record = _records(state).get(deal_id)
    if record is None:
        return {"status": "unknown_deal", "deal_id": deal_id}
    record["phase"] = outcome
    record["log"].append(f"dispo resolved: {outcome}")
    state["error_log"].append(f"DISPO_RESOLVED: {deal_id} — {outcome}")
    return {"status": outcome, "deal_id": deal_id}
