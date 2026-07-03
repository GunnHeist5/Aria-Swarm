"""state.py — Core BusinessState schema for the autonomous evolutionary swarm.

This module defines the single source of truth that is threaded through the
LangGraph orchestration in ``graph.py`` and durably serialized at the end of
every execution block. ``BusinessState`` is a ``TypedDict`` so it can be used
directly as a LangGraph graph state while remaining JSON-serializable for the
persistence layer (SQLite locally, Supabase in production).

The schema is intentionally *multi-layered*: financial telemetry, runtime mode,
LLM routing, evolutionary genome, capital allocation, and HITL all live in one
flat-but-grouped structure so a single hydrate/dump round-trip captures the
organism's complete condition.

Design rules (see CLAUDE.md):
  * Every field MUST round-trip through JSON serialization. No live objects,
    no clients, no callables stored here.
  * Money is denominated strictly in USDC / USD. Never store mixed units.
  * Fail-closed flags (e.g. ``frozen``, ``hitl_pending``) default to the safe
    state so a freshly hydrated or partially constructed state cannot act.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional, TypedDict

# ---------------------------------------------------------------------------
# Enumerations & literal type aliases
# ---------------------------------------------------------------------------

# Operational execution mode. Precedence when resolving whether a tool call
# needs human approval is: CRITICAL_GATE (always HITL) > STANDARD (always
# prompt) > AUTO (prompt-free, but only while auto_mode_budget_usd > 0).
ExecutionMode = Literal["standard", "auto"]

# Darwinian metabolic state derived from the metabolic_ratio / wallet balance.
MetabolicState = Literal["growth", "saving", "extinction", "replication"]

# Capital allocation lifecycle phase (treasury behavior). Gated by the rolling
# 30-day metabolic ratio and wallet size.
#   infancy            -> retain 100% of revenue
#   sustained_growth   -> 30/70 creator/operations split
#   sovereign_treasury -> + DeFi yield, geo fail-safe, legal wrapper
CapitalPhase = Literal["infancy", "sustained_growth", "sovereign_treasury"]

# Event triggers — the doors into the graph. Operations are event-driven;
# the clock is reserved for what genuinely needs one:
#   heartbeat        -> cron tick: metabolic/capital/lifecycle only (no dialectic)
#   ideation         -> run the full Visionary->Realist->Synthesizer dialectic
#   seller_reply     -> qualify an inbound seller response (Muffin is the sensor)
#   deal_closed      -> book the swarm's cut of a closed deal (10% hook)
#   new_leads_synced -> pipeline bookkeeping after a lead sync
#   offer_accepted   -> CRITICAL_GATE: contract signing is always HITL
#   contract_signed  -> human signed; start the 10-day disposition clock
#   buyer_confirmed  -> dispo buyer locked (earnest posted) -> wire pending
#   wallet_low       -> force a saving-mode re-evaluation
# (hitl_resume is NOT an event — it rides the LangGraph Command-resume path.)
TriggerType = Literal[
    "heartbeat",
    "ideation",
    "seller_reply",
    "deal_closed",
    "new_leads_synced",
    "offer_accepted",
    "contract_signed",
    "buyer_confirmed",
    "wallet_low",
]

# The explicit active model backend string flag. Routing between premium closed
# architectures (Claude, for Dev/Synthesizer + financial code) and open-source
# backends (Hermes 3, for the Visionary + Saving-Mode fleet) keys off this.
ActiveModel = Literal[
    "claude-3-5-sonnet",
    "hermes-3-70b",
    "hermes-3-8b",
    "hermes-3-akash",  # self-hosted fallback for the Immortality Protocol
]


class ICRStage(str, Enum):
    """Stage marker for the Ideation-Critique-Revision dialectical loop."""

    IDEATION = "ideation"      # The Visionary (Catalyst)
    CRITIQUE = "critique"      # The Realist (Antagonist)
    REVISION = "revision"      # The Synthesizer (Architect)
    COMPLETE = "complete"


# ---------------------------------------------------------------------------
# Nested layer schemas
# ---------------------------------------------------------------------------


class FinancialState(TypedDict):
    """Layer 1 — Financial telemetry. The metabolic substrate.

    All values are point-in-time snapshots refreshed on every cron cycle. The
    five fields called out explicitly in the architecture spec live here:
    ``wallet_balance_usdc``, ``inference_costs_usd``, ``revenue_generated_usdc``,
    and the computed ``metabolic_ratio`` (the active_model flag lives in the
    LLM routing layer).
    """

    # --- Core metabolic inputs (explicitly required) ---
    wallet_balance_usdc: float          # Live on-chain operational wallet balance.
    revenue_generated_usdc: float       # Revenue booked this accounting window (USDC).
    inference_costs_usd: float          # LLM inference spend this window.

    # --- Remaining cost components of the metabolic denominator ---
    server_rent_usd: float              # Compute/hosting (Akash, etc.) this window.
    api_subscriptions_usd: float        # Recurring API/tooling subscriptions this window.

    # --- Derived fitness metric ---
    # metabolic_ratio = revenue_generated_usdc /
    #                   (inference_costs_usd + server_rent_usd + api_subscriptions_usd)
    metabolic_ratio: float              # Current-cycle fitness ratio.
    rolling_30d_metabolic_ratio: float  # Smoothed ratio that gates capital phases.

    # --- Budgets & thresholds ---
    auto_mode_budget_usd: float         # Drainage wall for Auto Mode; at 0 -> freeze.
    seed_capital_usdc: float            # Initial buffer; presence keeps Phase 1 (infancy).
    replication_threshold_usdc: float   # Surplus that triggers child-swarm replication.

    # --- Wallet spending policy (MPC session-key caps) ---
    spend_per_call_cap_usdc: float      # Hard max per autonomous tx (default 5).
    spend_per_day_cap_usdc: float       # Hard max per rolling day (default 50).
    spent_today_usdc: float             # Running total for the daily cap.


class LLMRoutingState(TypedDict):
    """Layer 2 — Hybrid LLM routing.

    Node->model assignment is conditional, not fixed. ``active_model`` is the
    explicit backend flag every node consults via the model-selection layer.
    """

    active_model: ActiveModel               # Explicit active model backend string flag.
    saving_mode_active: bool                 # True when ratio < 1.0 forced a downgrade.
    immortality_protocol_active: bool        # True when routed to self-hosted Akash Hermes 3.
    # Per-node overrides, e.g. {"dev_agent": "claude-3-5-sonnet",
    # "visionary": "hermes-3-70b"}. Keys are node ids; values are ActiveModel.
    node_model_overrides: dict


class EvolutionState(TypedDict):
    """Layer 3 — The swarm genome & evolutionary operators.

    Tracks Horizontal Gene Transfer (HGT), mutation lineage, and the Red Queen
    adversarial sandbox gate. Prompt/config text itself is mutable genome.
    """

    swarm_id: str                        # Unique id for this swarm instance.
    parent_swarm_id: Optional[str]       # Lineage pointer (None for genesis swarm).
    generation: int                      # Replication depth from genesis.
    genome_hash: str                     # Hash of CLAUDE.md + agents/*.md + edge weights.
    child_swarm_ids: List[str]           # Child swarms spawned via Replication.

    # HGT — Shared Plasmid Database interface.
    last_hgt_pull_cycle: int             # Cycle index of last elite-gene hot-swap.
    broadcast_genes: List[str]           # Gene template ids this swarm has broadcast.
    adopted_genes: List[str]             # Elite gene ids pulled from the plasmid DB.

    # Red Queen sandbox gate.
    sandbox_validated: bool              # Did the (possibly mutated) genome pass sandbox.py?
    sandbox_iterations: int              # Refinement rounds against the Adversarial Agent.


class DialecticalState(TypedDict):
    """Layer 4 — The Ideation-Critique-Revision (ICR) loop.

    Captures the running dialectic between the Visionary, Realist, and
    Synthesizer before software construction locks in.
    """

    icr_stage: ICRStage                  # Current stage of the dialectic.
    visionary_output: Optional[str]      # Raw maximal-scale concept.
    realist_critique: Optional[str]      # Logistical/API/failure-vector critique.
    synthesized_blueprint: Optional[str] # Concrete, patched, upgraded blueprint.
    debate_transcript: List[str]         # Append-only record fed to the Synthesizer.


class CapitalState(TypedDict):
    """Layer 5 — Multi-phase capital allocation & treasury.

    Survival precedes extraction. Distribution rules are explicit and testable.
    """

    capital_phase: CapitalPhase          # Current lifecycle phase.
    creator_audit_key: str               # Hardcoded creator wallet (read-only audit).
    creator_royalty_pct: float           # 0.0 in Phase 1; 0.30 in Phase 2+.
    operations_pct: float                # 1.0 in Phase 1; 0.70 in Phase 2+.
    replication_pool_usdc: float         # Capital earmarked to fund child swarms.
    creator_dividends_paid_usdc: float   # Cumulative dividends streamed to the creator.
    distributed_revenue_usdc: float      # Cumulative revenue already run through allocation.

    # Phase 3 — Sovereign Treasury mechanisms.
    defi_yield_allocation_usdc: float    # Reserve placed in decentralized yield/lending.
    geo_failsafe_regions: List[str]      # Akash regions/jurisdictions for spend spreading.
    legal_wrapper_provisioned: bool      # On-chain DAO / digital LLC liability shield set up.


class HITLState(TypedDict):
    """Layer 6 — Human-in-the-Loop & safety gating.

    Fail-closed: any pending verification freezes graph execution until a signed
    resume arrives.
    """

    hitl_pending: bool                   # True while awaiting human verification.
    requires_auth: bool                  # Wallet/CRITICAL_GATE breach flag — halts autonomy.
    hitl_reason: Optional[str]           # e.g. "captcha", "2fa", "wallet_spend_cap".
    hitl_webhook_url: Optional[str]      # Remote-view tracking URL last broadcast.
    pending_critical_gate_tool: Optional[str]  # CRITICAL_GATE tool awaiting HITL approval.
    resume_signature: Optional[str]      # Signed token proving authorized resume.


class IdentityState(TypedDict):
    """Layer 7 — Self-directed identity (the SOUL Engine)."""

    soul_revision: int                   # How many times SOUL.md has been rewritten.
    last_soul_reflection_cycle: int      # Cycle index of the last self-reflection.
    strategic_edge: Optional[str]        # Latest self-asserted competitive edge.


# ---------------------------------------------------------------------------
# Top-level state
# ---------------------------------------------------------------------------


class BusinessState(TypedDict):
    """The complete, durable state of the swarm — LangGraph graph state.

    This is the contract serialized at the end of every execution block and
    fully hydrated at the start of each cron cycle. Any new field MUST round-trip
    through JSON.
    """

    # --- Runtime / control ---
    cycle_count: int                     # Monotonic execution-cycle counter.
    # Pending trigger for this invoke: {"type": TriggerType, "payload": dict,
    # "received_at": iso8601}. Consumed (cleared) by the dispatch node so a
    # hydrated snapshot can never re-fire a stale event. None => heartbeat.
    event: Optional[dict]
    execution_mode: ExecutionMode        # "standard" (gated) or "auto" (unsupervised).
    metabolic_state: MetabolicState      # Derived Darwinian state for this cycle.
    frozen: bool                         # Hard freeze (extinction, budget=0, or HITL).
    first_run_complete: bool             # False until the 6-step bootstrap daemon runs.
    environment: Optional[str]           # "sandbox_container" | "bare_metal" | None.

    # --- Layered sub-states ---
    financials: FinancialState
    llm: LLMRoutingState
    evolution: EvolutionState
    dialectic: DialecticalState
    capital: CapitalState
    hitl: HITLState
    identity: IdentityState

    # --- Free-form operational scratch ---
    # Current business blueprint/schema under construction or in market.
    active_blueprint: Optional[str]
    operational_flags: dict              # Misc boolean/string flags for node coordination.
    error_log: List[str]                 # Append-only diagnostics for the current cycle.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_metabolic_ratio(financials: FinancialState) -> float:
    """Return revenue / total operating cost.

    The fitness function of the entire organism. Returns ``0.0`` when there is
    no cost basis yet (avoids division by zero on a freshly bootstrapped swarm)
    and ``inf`` only would be misleading, so a zero-cost cycle yields the raw
    revenue magnitude is intentionally avoided — callers treat 0.0 as "not yet
    metabolizing".
    """

    costs = (
        financials["inference_costs_usd"]
        + financials["server_rent_usd"]
        + financials["api_subscriptions_usd"]
    )
    if costs <= 0.0:
        return 0.0
    return financials["revenue_generated_usdc"] / costs


def evaluate_metabolic_state(financials: FinancialState) -> str:
    """Classify the swarm's lifecycle state from its financials.

    Precedence (highest first):
      * ``extinction``  — wallet depleted *after* the swarm was funded. A
        never-funded genesis (seed 0, revenue 0, wallet 0) is NOT extinct — it is
        merely dormant; killing it on boot would be the "genesis trap".
      * ``replication`` — wallet at/above the replication surplus threshold.
      * ``saving``      — metabolic ratio below 1.0 (unprofitable; conserve).
      * ``growth``      — otherwise.
    """

    wallet = financials["wallet_balance_usdc"]
    was_funded = (
        financials["seed_capital_usdc"] > 0.0
        or financials["revenue_generated_usdc"] > 0.0
    )
    if wallet <= 0.0 and was_funded:
        return "extinction"
    if wallet >= financials["replication_threshold_usdc"]:
        return "replication"
    if financials["metabolic_ratio"] < 1.0:
        return "saving"
    return "growth"


# Capital-allocation phase gates (see CLAUDE.md "Capital Allocation Phases").
SUSTAINED_GROWTH_RATIO = 1.2        # rolling 30-day ratio to leave Infancy
SUSTAINED_GROWTH_TREASURY = 1_000.0  # min treasury (USDC) for Phase 2
SOVEREIGN_TREASURY = 10_000.0        # wallet size that unlocks Phase 3


def evaluate_capital_phase(financials: FinancialState) -> str:
    """Classify the treasury's capital-allocation phase.

    Gated by the rolling 30-day metabolic ratio + wallet size (highest first):
      * ``sovereign_treasury`` — wallet above the sovereign threshold.
      * ``sustained_growth``   — rolling ratio > 1.2 AND treasury > 1,000 USDC.
      * ``infancy``            — otherwise (seed capital, or rolling ratio < 1.0).

    Survival precedes extraction: a swarm only leaves Infancy (and begins paying
    the creator dividend) once it is both sustainably profitable and cushioned.
    """

    wallet = financials["wallet_balance_usdc"]
    rolling = financials["rolling_30d_metabolic_ratio"]
    if wallet > SOVEREIGN_TREASURY:
        return "sovereign_treasury"
    if rolling > SUSTAINED_GROWTH_RATIO and wallet > SUSTAINED_GROWTH_TREASURY:
        return "sustained_growth"
    return "infancy"


def new_business_state(
    *,
    swarm_id: str,
    creator_audit_key: str,
    seed_capital_usdc: float = 0.0,
    auto_mode_budget_usd: float = 0.0,
    replication_threshold_usdc: float = 5_000.0,
) -> BusinessState:
    """Construct a safe, fail-closed genesis ``BusinessState``.

    All gates default to the conservative state: ``frozen`` until bootstrap
    completes, Standard (permission-gated) execution mode, infancy capital
    phase, Saving/Immortality protocols off, and the premium Claude backend
    selected. Spending caps default to the 5/50 USDC MPC policy.
    """

    financials: FinancialState = {
        "wallet_balance_usdc": seed_capital_usdc,
        "revenue_generated_usdc": 0.0,
        "inference_costs_usd": 0.0,
        "server_rent_usd": 0.0,
        "api_subscriptions_usd": 0.0,
        "metabolic_ratio": 0.0,
        "rolling_30d_metabolic_ratio": 0.0,
        "auto_mode_budget_usd": auto_mode_budget_usd,
        "seed_capital_usdc": seed_capital_usdc,
        "replication_threshold_usdc": replication_threshold_usdc,
        "spend_per_call_cap_usdc": 5.0,
        "spend_per_day_cap_usdc": 50.0,
        "spent_today_usdc": 0.0,
    }

    llm: LLMRoutingState = {
        "active_model": "claude-3-5-sonnet",
        "saving_mode_active": False,
        "immortality_protocol_active": False,
        "node_model_overrides": {},
    }

    evolution: EvolutionState = {
        "swarm_id": swarm_id,
        "parent_swarm_id": None,
        "generation": 0,
        "genome_hash": "",
        "child_swarm_ids": [],
        "last_hgt_pull_cycle": 0,
        "broadcast_genes": [],
        "adopted_genes": [],
        "sandbox_validated": False,
        "sandbox_iterations": 0,
    }

    dialectic: DialecticalState = {
        "icr_stage": ICRStage.IDEATION,
        "visionary_output": None,
        "realist_critique": None,
        "synthesized_blueprint": None,
        "debate_transcript": [],
    }

    capital: CapitalState = {
        "capital_phase": "infancy",
        "creator_audit_key": creator_audit_key,
        "creator_royalty_pct": 0.0,
        "operations_pct": 1.0,
        "replication_pool_usdc": 0.0,
        "creator_dividends_paid_usdc": 0.0,
        "distributed_revenue_usdc": 0.0,
        "defi_yield_allocation_usdc": 0.0,
        "geo_failsafe_regions": [],
        "legal_wrapper_provisioned": False,
    }

    hitl: HITLState = {
        "hitl_pending": False,
        "requires_auth": False,
        "hitl_reason": None,
        "hitl_webhook_url": None,
        "pending_critical_gate_tool": None,
        "resume_signature": None,
    }

    identity: IdentityState = {
        "soul_revision": 0,
        "last_soul_reflection_cycle": 0,
        "strategic_edge": None,
    }

    return {
        "cycle_count": 0,
        "event": None,
        "execution_mode": "standard",
        "metabolic_state": "saving",
        "frozen": True,  # fail-closed until the bootstrap daemon completes
        "first_run_complete": False,
        "environment": None,
        "financials": financials,
        "llm": llm,
        "evolution": evolution,
        "dialectic": dialectic,
        "capital": capital,
        "hitl": hitl,
        "identity": identity,
        "active_blueprint": None,
        "operational_flags": {},
        "error_log": [],
    }
