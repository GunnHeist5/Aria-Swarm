"""graph.py — LangGraph orchestration for the autonomous evolutionary swarm.

The graph is **event-dispatched**: every invoke enters through a dispatch
router that reads ``state["event"]`` and wakes only the subgraph that trigger
needs — operations are event-driven, and the clock is reserved for the
metabolic heartbeat.

  START -> dispatch ─┬─ heartbeat/wallet_low -> metabolic_check -> dispo_check -> END
                     ├─ ideation     -> metabolic_check -> dispo_check -> visionary
                     │                          -> realist -> synthesizer -> END
                     ├─ seller_reply -> qualify_reply -> END
                     ├─ deal_closed  -> book_revenue -> metabolic_check -> ...
                     ├─ new_leads_synced -> ops_event -> END
                     ├─ contract_signed -> start_dispo (10-day clock) -> END
                     ├─ buyer_confirmed -> confirm_buyer -> END
                     └─ offer_accepted / unknown -> escalate (HITL freeze)

Three architectural mechanics live here:

  1. The **Hybrid-LLM router** — ``metabolic_check_node`` recomputes the
     metabolic ratio and hot-swaps the active model backend between the
     premium Claude specialist and the cheap Hermes 3 fleet.
  2. The **Dialectical Ideation (ICR) loop** — Visionary -> Realist ->
     Synthesizer, with hard model assignments per the routing rules in
     ``CLAUDE.md``. Runs only on an explicit ``ideation`` event, never on a
     heartbeat tick — a metabolic pulse spends no inference on ideating.
  3. **Fail-closed eventing** — a malformed payload, an unknown event type, or
     ``offer_accepted`` (contract signing = CRITICAL_GATE) freezes via HITL.

The financial formula is NOT re-derived here — it is owned by
``state.compute_metabolic_ratio`` and reused.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt  # noqa: F401 — Command re-exported for resume.py

import registry
from prompts import render_agent_prompt
from state import (
    BusinessState,
    ICRStage,
    compute_metabolic_ratio,
    evaluate_metabolic_state,
)
from tools.hitl import dispatch_hitl_alert
from tools.revenue import book_closed_deal
from tools.ventures import pipeline as ventures
from tools.ventures.genome import VentureGenome
from tools.wholesaling import dispo
from tools.wholesaling.closing import route_closing
from tools.learn.distill import distill
from tools.learn.fetch import fetch_content
from tools.learn.route import route_insight

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing configuration
# ---------------------------------------------------------------------------

# Smoothing factor for the rolling 30-day metabolic ratio (EMA) that gates the
# capital-allocation phases. ~30-window half-life.
ROLLING_RATIO_ALPHA = 0.1

# Cheap worker target the router falls back to in Saving Mode. Env-selectable so
# the VPS can point Saving Mode at the Liquid LFM ("liquid-lfm") without a deploy.
SAVING_MODE_MODEL = os.environ.get("SAVING_MODE_MODEL", "hermes-3-70b")
# Premium specialist for sensitive synthesis / financial code.
SPECIALIST_MODEL = "claude-3-5-sonnet"

# Hosted endpoints for the OpenAI-compatible Hermes fleet.
OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
TOGETHER_BASE_URL = os.environ.get(
    "TOGETHER_BASE_URL", "https://api.together.xyz/v1"
)
# Self-hosted Akash endpoint for the Immortality Protocol panic-switch.
AKASH_HERMES_BASE_URL = os.environ.get("AKASH_HERMES_BASE_URL", "")

# Liquid AI — efficient LFMs (OpenAI-compatible). Cheap worker tier / edge-local
# candidate. Host + slug env-overridable (confirm the exact API host on the VPS).
LIQUID_BASE_URL = os.environ.get("LIQUID_BASE_URL", "https://api.liquid.ai/v1")
LIQUID_SLUG = os.environ.get("LIQUID_MODEL_SLUG", "lfm-7b")
LIQUID_MODELS = {"liquid-lfm"}

# Cost accounting: an estimated per-LLM-call inference charge and a daily server
# rent give the metabolic ratio a real cost basis (revenue / operating cost).
# Env-overridable; defaults are placeholders until real usage metering lands.
INFERENCE_COST_PER_CALL_USD = float(os.environ.get("INFERENCE_COST_PER_CALL_USD", "0.01"))
SERVER_RENT_PER_DAY_USD = float(os.environ.get("SERVER_RENT_PER_DAY_USD", "1.0"))


def _charge_inference(state: BusinessState) -> None:
    """Accrue one LLM call's estimated cost into the metabolic denominator."""

    fin = state["financials"]
    fin["inference_costs_usd"] = round(
        fin["inference_costs_usd"] + INFERENCE_COST_PER_CALL_USD, 6
    )

# Hermes backend flags (validity set for fail-closed routing).
HERMES_MODELS = {"hermes-3-70b", "hermes-3-8b", "hermes-3-akash"}

# Per-provider model slugs. Provider naming conventions differ, so each provider
# gets its own slug map; the Together slugs are env-overridable because the exact
# hosted ids can shift (8B falls back to 70B when a provider doesn't host it).
OPENROUTER_HERMES_SLUGS = {
    "hermes-3-70b": "nousresearch/hermes-3-llama-3.1-70b",
    "hermes-3-8b": "nousresearch/hermes-3-llama-3.1-8b",
}
TOGETHER_HERMES_SLUGS = {
    "hermes-3-70b": os.environ.get("HERMES_70B_SLUG", "NousResearch/Hermes-3-Llama-3.1-70B"),
    "hermes-3-8b": os.environ.get("HERMES_8B_SLUG", "NousResearch/Hermes-3-Llama-3.1-70B"),
}


def _hermes_provider() -> str:
    """Resolve which OpenAI-compatible host serves the Hermes fleet.

    Explicit ``HERMES_PROVIDER`` wins; otherwise infer from whichever key is
    present (OpenRouter preferred), defaulting to OpenRouter.
    """

    explicit = os.environ.get("HERMES_PROVIDER", "").strip().lower()
    if explicit in ("openrouter", "together"):
        return explicit
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("TOGETHER_API_KEY"):
        return "together"
    return "openrouter"


def _hermes_backend_config(active_model: str) -> tuple[str, str, str | None]:
    """Return (model_slug, base_url, api_key) for a Hermes backend flag."""

    if active_model == "hermes-3-akash":  # Immortality Protocol self-host
        return "hermes-3", AKASH_HERMES_BASE_URL, (
            os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TOGETHER_API_KEY")
        )

    provider = _hermes_provider()
    if provider == "together":
        return (
            TOGETHER_HERMES_SLUGS[active_model],
            TOGETHER_BASE_URL,
            os.environ.get("TOGETHER_API_KEY"),
        )
    return (
        OPENROUTER_HERMES_SLUGS[active_model],
        OPENROUTER_BASE_URL,
        os.environ.get("OPENROUTER_API_KEY"),
    )


# ---------------------------------------------------------------------------
# Dynamic LLM factory
# ---------------------------------------------------------------------------


def _phenotype_temperature(base: float, phenotype: dict | None) -> float:
    """Apply the live phenotype's temperature scale to a model's base temp.

    Phenotypic plasticity: the genome (prompts) is untouched; only this runtime
    knob flexes with live metrics. ``None`` / missing scale → base unchanged.
    Result is clamped to ``[0.0, 1.0]`` so an aggressive scale can't produce an
    out-of-range temperature.
    """

    scale = (phenotype or {}).get("temperature_scale", 1.0)
    return round(max(0.0, min(1.0, base * scale)), 3)


def get_llm_backend(active_model: str, phenotype: dict | None = None) -> Any:
    """Return a chat model client for ``active_model``.

    Routes the premium specialist flag to ``ChatAnthropic`` and every Hermes
    variant to ``ChatOpenAI`` pointed at an OpenAI-compatible endpoint
    (OpenRouter / Together / self-hosted Akash). Provider classes are imported
    lazily so this module imports without the langchain provider packages
    installed, and so a Saving-Mode run never imports the Claude client it
    isn't using.

    ``phenotype`` (from ``state['llm']['phenotype']``) flexes the sampling
    temperature from live metrics without touching the genome; ``None`` keeps
    each model's base temperature. API keys are read from the environment —
    never hardcoded. Unknown model flags raise ``ValueError`` (fail closed).
    """

    if active_model == SPECIALIST_MODEL:
        from langchain_anthropic import ChatAnthropic

        # "claude-3-5-sonnet" is the internal routing label; the actual API model
        # id is configurable (accounts differ on which Sonnet they can access).
        return ChatAnthropic(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            temperature=_phenotype_temperature(0.2, phenotype),
        )

    if active_model in HERMES_MODELS:
        from langchain_openai import ChatOpenAI

        slug, base_url, api_key = _hermes_backend_config(active_model)
        return ChatOpenAI(
            model=slug,
            base_url=base_url or None,
            api_key=api_key,
            temperature=_phenotype_temperature(0.7, phenotype),  # exploratory brainstorming
        )

    if active_model in LIQUID_MODELS:
        from langchain_openai import ChatOpenAI

        # Liquid's efficient LFM via its OpenAI-compatible endpoint — the cheap
        # worker tier / edge-local candidate (endothermy: less API-weather-dependent).
        return ChatOpenAI(
            model=LIQUID_SLUG,
            base_url=LIQUID_BASE_URL or None,
            api_key=os.environ.get("LIQUID_API_KEY"),
            temperature=_phenotype_temperature(0.7, phenotype),
        )

    raise ValueError(f"Unknown active_model backend: {active_model!r}")


def resolve_phenotype(state: BusinessState) -> dict:
    """Adapt runtime sampling from live metrics — the phenotype, not the genome.

    * Saving Mode → conservative (scale < 1): cheaper, more deterministic output
      while the organism is unprofitable.
    * Growth but a flat/declining rolling ratio → more exploration (scale > 1) to
      break out of a local optimum.
    * Otherwise nominal (1.0).

    Returns ``{temperature_scale, rationale}``; scale clamped to ``[0.5, 1.5]``.
    """

    fin = state["financials"]
    mstate = state["metabolic_state"]
    ratio = fin["metabolic_ratio"]
    rolling = fin["rolling_30d_metabolic_ratio"]

    if state["llm"]["saving_mode_active"] or mstate == "saving":
        scale, why = 0.7, "saving_mode: conservative sampling"
    elif mstate == "growth" and rolling > 0 and ratio <= rolling * 1.02:
        # Profitable but plateaued — turn up exploration to escape the rut.
        scale, why = 1.3, "growth_plateau: raise exploration to escape local optimum"
    else:
        scale, why = 1.0, "nominal"

    return {"temperature_scale": max(0.5, min(1.5, scale)), "rationale": why}


# ---------------------------------------------------------------------------
# Event dispatch — the single entry router
# ---------------------------------------------------------------------------

# Event type -> the first node of its subgraph. Anything not listed here routes
# to the escalate node (fail-closed: an unrecognized trigger freezes for HITL
# instead of being guessed at).
_EVENT_ROUTES = {
    "heartbeat": "metabolic_check",
    "wallet_low": "metabolic_check",   # forces a saving-mode re-evaluation
    "ideation": "metabolic_check",     # metabolic first, then the full dialectic
    "seller_reply": "qualify_reply",
    "deal_closed": "book_revenue",
    "new_leads_synced": "ops_event",
    "offer_accepted": "escalate",      # contract signing = CRITICAL_GATE, always
    "contract_signed": "start_dispo",  # human signed -> open the 10-day clock
    "buyer_confirmed": "confirm_buyer",
    "venture_proposed": "propose_venture",
    "venture_approved": "approve_venture",
    "venture_validated": "venture_metrics",
    "venture_killed": "kill_venture",
    "learning_ingested": "learn",
}


def dispatch_node(state: BusinessState) -> BusinessState:
    """Consume ``state['event']`` and stage it for routing.

    The pending event is copied into ``operational_flags['last_event']`` (the
    routers and event nodes read it there) and cleared from the state, so a
    persisted snapshot can never re-fire a stale trigger on the next hydrate.
    A missing/None event is a heartbeat — full back-compat with ``--cron``.
    """

    event = state.get("event") or {}
    etype = event.get("type") or "heartbeat"
    state["operational_flags"]["last_event"] = {
        "type": etype,
        "payload": event.get("payload") or {},
        "received_at": event.get("received_at"),
    }
    state["event"] = None  # consumed — never re-fires from a snapshot
    return state


def _last_event(state: BusinessState) -> dict:
    """The staged event for this invoke (set by ``dispatch_node``)."""

    return state["operational_flags"].get("last_event") or {
        "type": "heartbeat", "payload": {}, "received_at": None,
    }


def _route_event(state: BusinessState) -> str:
    """Entry router: event type -> first node of its subgraph (else escalate)."""

    return _EVENT_ROUTES.get(_last_event(state)["type"], "escalate")


def _route_after_metabolic(state: BusinessState) -> str:
    """After the metabolic gate: only an ``ideation`` event runs the dialectic.

    A heartbeat (or wallet_low / deal_closed joining the metabolic path) ends
    the invoke here — the tick spends no inference on ideating.
    """

    return "ideate" if _last_event(state)["type"] == "ideation" else "done"


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _freeze_event(state: BusinessState, reason: str, note: str) -> None:
    """Raise the fail-closed HITL flags for a bad/undeliverable event.

    Shared by the event nodes so a malformed payload OR a well-formed event that
    targets a nonexistent record (a typo'd deal_id/venture_id) freezes for a
    human instead of completing silently — the gate then fires the alert.
    """

    hitl = state["hitl"]
    hitl["hitl_pending"] = True
    hitl["requires_auth"] = True
    hitl["hitl_reason"] = reason
    state["error_log"].append(note)


def metabolic_check_node(state: BusinessState) -> BusinessState:
    """Recompute the metabolic ratio and route the active model backend.

    Reads revenue and the full cost stack from ``state['financials']``,
    recomputes the ratio via the canonical helper, and hot-swaps the backend:

      * ratio < 1.0  -> Saving Mode: drop to the cheap Hermes fleet.
      * ratio >= 1.0 -> healthy: keep/upgrade to the Claude specialist.

    A fresh genesis swarm has no cost basis yet (ratio 0.0), which counts as
    ``< 1.0`` and therefore defaults to the cheap backend — the correct
    capital-preserving behavior before revenue exists.
    """

    financials = state["financials"]

    # Accrue a daily server-rent charge so the swarm has a real cost basis even
    # in cycles with no LLM calls — otherwise the cost stack stays 0 and
    # metabolic_ratio = revenue/0 is always 0.0, freezing the whole economy in
    # Saving Mode forever. Once per calendar day (like the daily spend reset).
    flags = state["operational_flags"]
    day = _event_date(state).isoformat()
    if flags.get("rent_day") != day:
        flags["rent_day"] = day
        financials["server_rent_usd"] = round(
            financials["server_rent_usd"] + SERVER_RENT_PER_DAY_USD, 6
        )

    ratio = compute_metabolic_ratio(financials)
    financials["metabolic_ratio"] = ratio

    # Roll the smoothed 30-day ratio (EMA) that gates the capital phases. Seed it
    # with the first real reading rather than decaying up from zero.
    prev = financials["rolling_30d_metabolic_ratio"]
    financials["rolling_30d_metabolic_ratio"] = round(
        ROLLING_RATIO_ALPHA * ratio + (1 - ROLLING_RATIO_ALPHA) * prev, 6
    ) if prev > 0 else ratio

    # Lifecycle label (extinction/replication/saving/growth) — drives the
    # lifecycle transitions in main.py and the extinction short-circuit below.
    state["metabolic_state"] = evaluate_metabolic_state(financials)

    # Model routing is an independent, ratio-based concern: an unprofitable
    # swarm drops to the cheap Hermes fleet regardless of its lifecycle label.
    if ratio < 1.0:
        state["llm"]["active_model"] = SAVING_MODE_MODEL
        state["llm"]["saving_mode_active"] = True
    else:
        state["llm"]["active_model"] = SPECIALIST_MODEL
        state["llm"]["saving_mode_active"] = False

    # Phenotypic plasticity: flex runtime sampling from live metrics (genome
    # untouched). Consumed by get_llm_backend on every downstream agent call.
    state["llm"]["phenotype"] = resolve_phenotype(state)

    return state


def _route_lifecycle(state: BusinessState) -> str:
    """Route after the metabolic check: a dead swarm skips the dialectic.

    On extinction the cycle short-circuits straight to END — no inference is
    spent ideating for a swarm that is terminating. main.py then runs the
    graceful self-termination sequence.

    Exception: if a breach flag was raised earlier in this invoke (e.g. a
    malformed deal_closed payload on a depleted wallet), route to the gate even
    on extinction so the HITL alert fires and the interrupt is recorded — a
    frozen breach must never be silently swallowed by the extinction short-cut.
    """

    hitl = state["hitl"]
    if hitl["hitl_pending"] or hitl["requires_auth"]:
        return "continue"
    return "extinct" if state["metabolic_state"] == "extinction" else "continue"


def _agent_context(state: BusinessState) -> dict:
    """Build the dynamic injection context for an `agents/*.md` template.

    One comprehensive dict covering every injection variable used by any persona
    template — extra keys are harmless to ``str.format``. ``debate_transcript``
    is flattened to text; ``active_blueprint`` gets a cold-start placeholder.
    """

    fin = state["financials"]
    dialectic = state["dialectic"]
    return {
        "metabolic_ratio": fin["metabolic_ratio"],
        "active_model": state["llm"]["active_model"],
        "capital_phase": state["capital"]["capital_phase"],
        "wallet_balance_usdc": fin["wallet_balance_usdc"],
        "active_blueprint": state.get("active_blueprint") or "(none — cold start)",
        "visionary_output": dialectic["visionary_output"] or "",
        "realist_critique": dialectic["realist_critique"] or "",
        "debate_transcript": "\n".join(dialectic["debate_transcript"]) or "(empty)",
    }


def _content(response: Any) -> str:
    """Coerce an ``llm.invoke`` return (``AIMessage``) to a plain string."""

    content = getattr(response, "content", response)
    return content if isinstance(content, str) else str(content)


class PromptResolutionError(Exception):
    """A role's prompt could not be resolved into a runnable, formatted string."""

    def __init__(self, reason: str, original: Exception | None = None):
        super().__init__(reason)
        self.reason = reason
        self.original = original


def _resolve_prompt(role: str, ctx: dict, swarm_id: str | None) -> str:
    """Resolve a role's prompt, registry-first with the genome file as fallback.

    The production genome lives in the Plasmid Registry: the highest-fitness,
    production-cleared gene per role (promoted by the sandbox). We run that when
    present, falling back to the baseline ``agents/<role>.md`` file otherwise.

    Fail-closed: a registry gene whose ``{placeholders}`` are broken (a bad
    mutation) raises rather than running unformatted; a missing file likewise.
    Registry *unavailability* (not a bad gene) is non-fatal — we fall back to the
    file so a transient DB issue never halts the swarm.
    """

    text = None
    if swarm_id:
        try:
            # Pass DB_PATH explicitly (resolved now) so it's runtime-overridable.
            text = registry.get_active_genome(swarm_id, registry.DB_PATH).get(role)
        except Exception as exc:  # registry down -> fall back to file, don't halt
            logger.warning("registry unavailable for %s, using file: %s", role, exc)
            text = None

    if text is not None:
        try:
            return text.format(**ctx)
        except (KeyError, IndexError, ValueError) as exc:
            raise PromptResolutionError(f"malformed_genome:{role}", exc)

    try:
        return render_agent_prompt(role, ctx)
    except (FileNotFoundError, ValueError) as exc:
        raise PromptResolutionError(f"missing_prompt:{role}", exc)


def _freeze_prompt(state: BusinessState, reason: str, exc: Exception) -> None:
    """Fail-closed: flag an unresolvable prompt so the next gate freezes.

    Records the explicit error and raises the HITL flags WITHOUT writing any
    output or advancing the dialectic — preventing an unformatted/generic run.
    The following ``hitl_gate_node`` sees the flags and interrupts.
    """

    hitl = state["hitl"]
    hitl["requires_auth"] = True
    hitl["hitl_pending"] = True
    hitl["hitl_reason"] = reason
    state["error_log"].append(f"PROMPT_LOAD_FAILED: {reason}: {exc}")


def _run_agent(
    state: BusinessState,
    agent_name: str,
    model: str,
    output_key: str,
    label: str,
    next_stage: ICRStage,
) -> BusinessState:
    """Resolve the agent's prompt, invoke its LLM, and record the turn.

    The prompt is resolved registry-first (the evolved production genome) with
    the ``agents/<role>.md`` file as fallback. Fail-closed: an unresolvable or
    malformed prompt freezes via HITL instead of running on an unformatted
    prompt — the dialectic stage is left unadvanced.
    """

    ctx = _agent_context(state)
    try:
        prompt = _resolve_prompt(agent_name, ctx, state["evolution"]["swarm_id"])
    except PromptResolutionError as exc:
        _freeze_prompt(state, exc.reason, exc.original or exc)
        return state

    llm = get_llm_backend(model, state["llm"].get("phenotype"))
    output = _content(llm.invoke(prompt))
    _charge_inference(state)

    dialectic = state["dialectic"]
    dialectic[output_key] = output
    dialectic["debate_transcript"].append(f"{label}: {output}")
    dialectic["icr_stage"] = next_stage
    return state


def visionary_node(state: BusinessState) -> BusinessState:
    """The Visionary (Catalyst) — maximal-scale, uncensored ideation.

    Hardcoded to Hermes regardless of the routed backend, per the routing
    rules: the Visionary always runs on the uncensored open-source fleet.
    """

    return _run_agent(
        state, "visionary", SAVING_MODE_MODEL,
        "visionary_output", "VISIONARY", ICRStage.CRITIQUE,
    )


def realist_node(state: BusinessState) -> BusinessState:
    """The Realist (Antagonist) — cynical feasibility critique.

    Uses the currently routed backend (Claude in growth, Hermes in Saving Mode)
    so the critique cost tracks the swarm's metabolic state.
    """

    return _run_agent(
        state, "realist", state["llm"]["active_model"],
        "realist_critique", "REALIST", ICRStage.REVISION,
    )


def synthesizer_node(state: BusinessState) -> BusinessState:
    """The Synthesizer (Architect) — concrete upgraded blueprint.

    Forced to the Claude specialist regardless of Saving Mode, per the routing
    rules: final synthesis and sensitive blueprint construction always use the
    premium backend.
    """

    return _run_agent(
        state, "synthesizer", SPECIALIST_MODEL,
        "synthesized_blueprint", "SYNTHESIZER", ICRStage.COMPLETE,
    )


# ---------------------------------------------------------------------------
# Event nodes — one per operational trigger
# ---------------------------------------------------------------------------


def qualify_reply_node(state: BusinessState) -> BusinessState:
    """``seller_reply`` — qualify an inbound seller response, right now.

    Muffin's Seller Response Monitor is the sensor; this node is the brain. The
    ``qualifier`` genome role (registry-first, evolvable like every gene) reads
    the reply + lead context and returns a triage verdict. The result is only
    recorded — responding/offering stays with the operator behind its gates.
    """

    event = _last_event(state)
    payload = event["payload"]
    lead_id = str(payload.get("lead_id") or "unknown")
    reply_text = str(payload.get("reply_text") or "(empty reply)")
    reply_hash = hashlib.sha256(reply_text.encode("utf-8")).hexdigest()[:16]

    # Idempotency: Muffin's monitor re-lists the same inbox replies every cron
    # run, so skip a lead+reply we've already assessed — no duplicate LLM spend.
    qualified = state["operational_flags"].setdefault("qualified_replies", {})
    prior = qualified.get(lead_id)
    if prior and prior.get("reply_hash") == reply_hash:
        state["error_log"].append(
            f"SKIP: seller reply for lead {lead_id} already qualified"
        )
        return state

    ctx = _agent_context(state)
    ctx["reply_text"] = reply_text
    ctx["lead_context"] = json.dumps(
        {k: v for k, v in payload.items() if k != "reply_text"}, default=str
    )

    try:
        prompt = _resolve_prompt("qualifier", ctx, state["evolution"]["swarm_id"])
    except PromptResolutionError as exc:
        _freeze_prompt(state, exc.reason, exc.original or exc)
        return state

    llm = get_llm_backend(state["llm"]["active_model"], state["llm"].get("phenotype"))
    assessment = _content(llm.invoke(prompt))
    _charge_inference(state)

    qualified[lead_id] = {
        "assessment": assessment,
        "reply_hash": reply_hash,
        "received_at": event.get("received_at"),
        "contact": payload.get("contact"),
    }
    state["error_log"].append(f"QUALIFIED: seller reply for lead {lead_id}")
    return state


def book_revenue_node(state: BusinessState) -> BusinessState:
    """``deal_closed`` — book the swarm's cut of a closed deal into the treasury.

    Delegates to ``tools.revenue.book_closed_deal`` (idempotent per deal_id),
    then flows into ``metabolic_check`` so a big close can flip the capital
    phase in the same invoke. A malformed payload freezes instead of guessing
    at money numbers.
    """

    payload = _last_event(state)["payload"]
    try:
        result = book_closed_deal(
            state,
            deal_id=str(payload["deal_id"]),
            assignment_fee_usd=float(payload["assignment_fee_usd"]),
            swarm_cut_pct=float(payload.get("swarm_cut_pct", 0.10)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        hitl = state["hitl"]
        hitl["hitl_pending"] = True
        hitl["requires_auth"] = True
        hitl["hitl_reason"] = "malformed_event:deal_closed"
        state["error_log"].append(f"BOOKING_FAILED: bad deal_closed payload: {exc}")
        return state

    state["operational_flags"]["last_booking"] = result
    return state


def ops_event_node(state: BusinessState) -> BusinessState:
    """``new_leads_synced`` — pipeline bookkeeping after a lead sync."""

    event = _last_event(state)
    pipeline = state["operational_flags"].setdefault("lead_pipeline", {})
    pipeline["last_sync"] = {"received_at": event.get("received_at"), **event["payload"]}
    pipeline["total_syncs"] = pipeline.get("total_syncs", 0) + 1
    state["error_log"].append(f"LEADS_SYNCED: {event['payload'] or '(no payload)'}")
    return state


def escalate_node(state: BusinessState) -> BusinessState:
    """``offer_accepted`` / unknown events — raise the HITL flags and record why.

    An accepted offer means a contract is about to be signed: CRITICAL_GATE,
    never autonomous, regardless of mode. Unknown event types land here too —
    fail-closed beats guessing. The following gate fires the alert + interrupt.
    """

    event = _last_event(state)
    etype = event["type"]
    hitl = state["hitl"]
    hitl["hitl_pending"] = True
    hitl["requires_auth"] = True

    if etype == "offer_accepted":
        hitl["hitl_reason"] = "critical_gate:contract_signing"
        hitl["pending_critical_gate_tool"] = "contract_signing"
        state["operational_flags"]["pending_offer"] = event["payload"]
        state["error_log"].append(
            f"CRITICAL_GATE: offer accepted, awaiting human contract sign-off "
            f"({event['payload'] or 'no payload'})"
        )
    else:
        hitl["hitl_reason"] = f"unknown_event:{etype}"
        state["error_log"].append(f"UNKNOWN_EVENT: {etype!r} — frozen fail-closed")
    return state


def _event_date(state: BusinessState):
    """The invoke's reference date: the event's received_at day (else today).

    Keeps the dispo clock deterministic under test (events carry injected
    timestamps) while a live tick with no timestamp still works.
    """

    from datetime import date

    received = _last_event(state).get("received_at")
    if received:
        try:
            return date.fromisoformat(str(received)[:10])
        except ValueError:
            pass
    return date.today()


def start_dispo_node(state: BusinessState) -> BusinessState:
    """``contract_signed`` — open the 10-day disposition clock.

    Fired by the human/operator AFTER the HITL-gated signing that
    ``offer_accepted`` freezes for. Runs an immediate tick so the Day-0
    "blast all platforms" action is emitted in the same invoke.
    """

    payload = _last_event(state)["payload"]
    try:
        deal_id = str(payload["deal_id"])
        signed = str(payload.get("signed_date") or _event_date(state).isoformat())
        result = dispo.start_dispo(
            state,
            deal_id=deal_id,
            contract_signed_date=signed,
            address=payload.get("address"),
            arv=payload.get("arv"),
            offer_price=payload.get("offer_price"),
            assignment_fee_target=payload.get("assignment_fee_target"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        hitl = state["hitl"]
        hitl["hitl_pending"] = True
        hitl["requires_auth"] = True
        hitl["hitl_reason"] = "malformed_event:contract_signed"
        state["error_log"].append(f"DISPO_OPEN_FAILED: bad contract_signed payload: {exc}")
        return state

    if result["status"] == "opened":
        # Route the closing by property state (default TX, the live market).
        # An unreviewed/unknown state fails closed: record kept, loud HITL
        # escalation, no invented vendor — legality gets reviewed by a human.
        routing = route_closing(
            str(payload.get("state") or "TX"),
            overrides=state["operational_flags"].get("closing_rules_overrides"),
        )
        record = state["operational_flags"]["dispo"][deal_id]
        record["closing"] = routing
        if routing["status"] == "routed":
            state["error_log"].append(
                f"DISPO_CLOSING_ROUTED: {deal_id} [{routing['state']}] -> "
                f"{routing['closer_type']}: {routing['vendor']} "
                f"(backup: {routing['backup']})"
            )
        else:
            hitl = state["hitl"]
            hitl["hitl_pending"] = True
            hitl["requires_auth"] = True
            hitl["hitl_reason"] = f"unreviewed_state:{routing['state']}"
            state["error_log"].append(
                f"DISPO_CLOSING_BLOCKED: {deal_id} [{routing['state']}] — "
                f"{routing['notes']}"
            )
        dispo.dispo_tick(state, _event_date(state))  # emit the Day-0 blast now
    return state


def confirm_buyer_node(state: BusinessState) -> BusinessState:
    """``buyer_confirmed`` — lock the dispo buyer (earnest money required)."""

    payload = _last_event(state)["payload"]
    try:
        result = dispo.confirm_buyer(
            state,
            deal_id=str(payload["deal_id"]),
            buyer=str(payload["buyer"]),
            earnest_posted=bool(payload.get("earnest_posted", False)),
            confirmed_date=_event_date(state).isoformat(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _freeze_event(state, "malformed_event:buyer_confirmed",
                      f"DISPO_CONFIRM_FAILED: bad buyer_confirmed payload: {exc}")
        return state
    if result.get("status") == "unknown_deal":
        _freeze_event(state, f"unknown_target:buyer_confirmed:{result['deal_id']}",
                      f"DISPO_CONFIRM_UNKNOWN: no dispo record for {result['deal_id']!r} "
                      "— a confirmed buyer was dropped; check the deal_id")
    return state


def dispo_check_node(state: BusinessState) -> BusinessState:
    """Heartbeat enforcement of the dispo deadlines — Day 5 can never slip.

    Runs on every metabolic tick: evaluates all open dispo deals against the
    timeline and surfaces due/overdue actions (loud error_log lines plus
    ``operational_flags['dispo_actions_due']`` for the operator layer). Alerts,
    never freezes — a dispo deadline must not paralyze reply qualification.
    """

    actions = dispo.dispo_tick(state, _event_date(state))
    critical = [a for a in actions if a["action"] in (
        "maxdispo_gate", "maxdispo_gate_MISSED", "decision_point", "hard_deadline",
    )]
    if critical:
        # Fire the notification webhook (no interrupt): the human must act on
        # the dispo clock, but the swarm keeps operating.
        try:
            dispatch_hitl_alert(state)
        except Exception as exc:  # alerting is best-effort
            logger.warning("dispo alert dispatch failed: %s", exc)
    return state


def venture_check_node(state: BusinessState) -> BusinessState:
    """Heartbeat enforcement of the venture portfolio — apoptosis can't slip.

    Runs the venture pipeline tick on every metabolic pulse: kills ventures
    that trip their criteria (reclaiming capital to the treasury), advances
    validated ones through their staged budgets, and records proven wins. A
    stage that needs human approval surfaces (no freeze) — the portfolio keeps
    running while the human decides on the one gated bet.
    """

    actions = ventures.venture_tick(state, _event_date(state))
    if any(a["action"] in ("apoptosis", "stage_gate") for a in actions):
        try:
            dispatch_hitl_alert(state)
        except Exception as exc:  # alerting is best-effort
            logger.warning("venture alert dispatch failed: %s", exc)
    return state


def propose_venture_node(state: BusinessState) -> BusinessState:
    """``venture_proposed`` — open a new venture under graduated autonomy.

    Payload: ``venture_id``, ``kind`` (+ optional ``hypothesis``,
    ``seed_cap_usd``, ``stage_budgets``, ``kill_criteria``). A big-resource or
    critical proposal comes back ``gated`` → HITL freeze (a human funds the big
    bet); cheap/proven proposals fund stage 1 and start validating.
    """

    payload = _last_event(state)["payload"]
    try:
        genome = VentureGenome(
            kind=str(payload["kind"]),
            hypothesis=str(payload.get("hypothesis", "")),
            **{k: payload[k] for k in ("seed_cap_usd", "stage_budgets", "kill_criteria")
               if k in payload},
        )
        result = ventures.propose_venture(
            state, genome,
            venture_id=str(payload["venture_id"]),
            today=_event_date(state),
        )
    except (KeyError, TypeError, ValueError) as exc:
        hitl = state["hitl"]
        hitl["hitl_pending"] = True
        hitl["requires_auth"] = True
        hitl["hitl_reason"] = "malformed_event:venture_proposed"
        state["error_log"].append(f"VENTURE_PROPOSE_FAILED: bad payload: {exc}")
        return state

    if result["status"] == "gated":
        hitl = state["hitl"]
        hitl["hitl_pending"] = True
        hitl["requires_auth"] = True
        hitl["hitl_reason"] = f"venture_gate:{result['venture_id']}"
    return state


def approve_venture_node(state: BusinessState) -> BusinessState:
    """``venture_approved`` — a human funds a gated (proposed) venture.

    Completes the graduated-autonomy loop: the big/critical proposal that froze
    for review is funded and moved into validation once the human fires this.
    """

    payload = _last_event(state)["payload"]
    try:
        result = ventures.approve_venture(
            state, str(payload["venture_id"]), today=_event_date(state)
        )
    except (KeyError, TypeError, ValueError) as exc:
        _freeze_event(state, "malformed_event:venture_approved",
                      f"VENTURE_APPROVE_FAILED: bad payload: {exc}")
        return state
    if result.get("status") in ("unknown_venture", "not_gated"):
        _freeze_event(state, f"{result['status']}:venture_approved",
                      f"VENTURE_APPROVE_REJECTED: {result}")
    return state


def venture_metrics_node(state: BusinessState) -> BusinessState:
    """``venture_validated`` — feed observed metrics into a venture.

    Payload: ``venture_id`` + any of ``signal``, ``revenue_usd``, ``spent_usd``.
    Then run a tick so a metric that crosses a gate (or a kill line) acts now.
    """

    payload = _last_event(state)["payload"]
    try:
        result = ventures.record_metrics(
            state, str(payload["venture_id"]),
            signal=payload.get("signal"),
            revenue_usd=payload.get("revenue_usd"),
            spent_usd=payload.get("spent_usd"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        _freeze_event(state, "malformed_event:venture_validated",
                      f"VENTURE_METRICS_FAILED: bad payload: {exc}")
        return state
    if result.get("status") in ("unknown_venture", "invalid_metric"):
        _freeze_event(state, f"{result['status']}:venture_validated",
                      f"VENTURE_METRICS_REJECTED: {result} — metric not applied")
        return state
    ventures.venture_tick(state, _event_date(state))
    return state


def kill_venture_node(state: BusinessState) -> BusinessState:
    """``venture_killed`` — manually apoptose a venture (reclaim its capital)."""

    payload = _last_event(state)["payload"]
    try:
        result = ventures.resolve_venture(
            state, str(payload["venture_id"]), "killed", _event_date(state)
        )
    except (KeyError, TypeError, ValueError) as exc:
        _freeze_event(state, "malformed_event:venture_killed",
                      f"VENTURE_KILL_FAILED: bad payload: {exc}")
        return state
    if result.get("status") == "unknown_venture":
        _freeze_event(state, f"unknown_target:venture_killed:{result['venture_id']}",
                      f"VENTURE_KILL_UNKNOWN: no venture {result['venture_id']!r} — kill dropped")
    return state


def learn_node(state: BusinessState) -> BusinessState:
    """``learning_ingested`` — learn from a link, safely.

    fetch (stub here; live VPS adapter behind the seam) → distill into a typed
    insight → route it. The routing is fully gated: knowledge is only stored, a
    venture idea goes through the autonomy-gated venture path, and a genome
    tweak must survive the Red Queen AND clear the autonomy gate (structural /
    big-% changes escalate to a human). Untrusted input never auto-mutates.
    """

    payload = _last_event(state)["payload"]
    try:
        content = fetch_content(str(payload["url"]))
    except (KeyError, TypeError, ValueError) as exc:
        hitl = state["hitl"]
        hitl["hitl_pending"] = True
        hitl["requires_auth"] = True
        hitl["hitl_reason"] = "malformed_event:learning_ingested"
        state["error_log"].append(f"LEARN_FETCH_FAILED: bad payload/url: {exc}")
        return state

    llm = get_llm_backend(state["llm"]["active_model"], state["llm"].get("phenotype"))
    insight = distill(content, llm=llm)
    _charge_inference(state)

    # The genome-tweak path needs the mutation + adversary seams (imported here to
    # keep sandbox's import of graph acyclic at module load).
    from sandbox import adversarial_evaluate, mutate_prompt

    def _directed_mutate(role, baseline, directive):
        return mutate_prompt(role, f"{baseline}\n<!-- learn directive: {directive} -->")

    result = route_insight(
        state, insight, today=_event_date(state),
        adversary=adversarial_evaluate, mutate=_directed_mutate,
    )
    state["operational_flags"]["last_learning"] = {"insight_kind": insight["kind"], **result}
    return state


# ---------------------------------------------------------------------------
# HITL circuit breaker
# ---------------------------------------------------------------------------


def hitl_gate_node(state: BusinessState) -> BusinessState:
    """Fail-closed circuit breaker run after every working node.

    If a breach flag is set (``hitl_pending`` or ``requires_auth`` — raised e.g.
    by ``tools.wallet`` on a spend-cap violation, or by a CAPTCHA/2FA wall), fire
    the HITL alert and freeze the thread with a dynamic ``interrupt``. The
    checkpointer persists the frozen state under the thread id; nothing
    downstream runs until a human resumes via ``resume.py``.

    On resume the flags have been cleared (by ``resume.py``'s ``update_state``),
    so this node re-executes, skips the interrupt, and passes through — the alert
    therefore fires exactly once per freeze.
    """

    hitl = state["hitl"]
    if hitl["hitl_pending"] or hitl["requires_auth"]:
        payload = dispatch_hitl_alert(state)
        # Freeze: blocks until a human resumes this thread (resume.py clears the
        # breach flags and records the decision before signalling Command resume,
        # so this gate re-runs flag-free and falls through — alert fires once).
        interrupt(payload)
    return state


# ---------------------------------------------------------------------------
# Graph topology
# ---------------------------------------------------------------------------

# Every working node is guarded by its own HITL gate (the same fail-closed
# function registered under distinct names), so a breach raised inside ANY
# node freezes immediately after it.
_NODE_GATES = {
    "metabolic_check": "gate_metabolic",
    "dispo_check": "gate_dispo",
    "venture_check": "gate_venture",
    "visionary": "gate_visionary",
    "realist": "gate_realist",
    "synthesizer": "gate_synthesizer",
    "qualify_reply": "gate_qualify",
    "ops_event": "gate_ops",
    "escalate": "gate_escalate",
    "start_dispo": "gate_start_dispo",
    "confirm_buyer": "gate_confirm_buyer",
    "propose_venture": "gate_propose_venture",
    "approve_venture": "gate_approve_venture",
    "venture_metrics": "gate_venture_metrics",
    "kill_venture": "gate_kill_venture",
    "learn": "gate_learn",
}


def build_graph(checkpointer=None):
    """Construct and compile the event-dispatched swarm graph.

    Topology (gates interleaved after every working node):

      START -> dispatch ─┬ heartbeat/wallet_low -> metabolic_check -> gate ->
                         │     dispo_check -> gate -> END
                         ├ ideation -> (metabolic+dispo path) -> visionary ->
                         │     gate -> realist -> gate -> synthesizer -> gate -> END
                         ├ seller_reply -> qualify_reply -> gate -> END
                         ├ deal_closed -> book_revenue -> metabolic_check -> ...
                         ├ new_leads_synced -> ops_event -> gate -> END
                         ├ contract_signed -> start_dispo -> gate -> END
                         ├ buyer_confirmed -> confirm_buyer -> gate -> END
                         └ offer_accepted / unknown -> escalate -> gate (interrupt)

    Extinction short-circuits the metabolic path straight to END (a dead swarm
    spends nothing). Compiled with a checkpointer (``MemorySaver`` by default)
    so every step's full state is serialized and any interrupt is resumable by
    thread id.
    """

    if checkpointer is None:
        checkpointer = MemorySaver()

    g = StateGraph(BusinessState)

    g.add_node("dispatch", dispatch_node)
    g.add_node("metabolic_check", metabolic_check_node)
    g.add_node("dispo_check", dispo_check_node)
    g.add_node("venture_check", venture_check_node)
    g.add_node("visionary", visionary_node)
    g.add_node("realist", realist_node)
    g.add_node("synthesizer", synthesizer_node)
    g.add_node("qualify_reply", qualify_reply_node)
    g.add_node("book_revenue", book_revenue_node)
    g.add_node("ops_event", ops_event_node)
    g.add_node("escalate", escalate_node)
    g.add_node("start_dispo", start_dispo_node)
    g.add_node("confirm_buyer", confirm_buyer_node)
    g.add_node("propose_venture", propose_venture_node)
    g.add_node("approve_venture", approve_venture_node)
    g.add_node("venture_metrics", venture_metrics_node)
    g.add_node("kill_venture", kill_venture_node)
    g.add_node("learn", learn_node)
    for gate_name in _NODE_GATES.values():
        g.add_node(gate_name, hitl_gate_node)

    # Entry: dispatch consumes the event and routes to its subgraph.
    g.add_edge(START, "dispatch")
    g.add_conditional_edges(
        "dispatch", _route_event,
        {
            "metabolic_check": "metabolic_check",
            "qualify_reply": "qualify_reply",
            "book_revenue": "book_revenue",
            "ops_event": "ops_event",
            "escalate": "escalate",
            "start_dispo": "start_dispo",
            "confirm_buyer": "confirm_buyer",
            "propose_venture": "propose_venture",
            "approve_venture": "approve_venture",
            "venture_metrics": "venture_metrics",
            "kill_venture": "kill_venture",
            "learn": "learn",
        },
    )

    # Metabolic path: extinction short-circuits; otherwise gate, then the
    # dispo deadline check + venture portfolio tick run on EVERY heartbeat
    # (Day 5 and apoptosis can never slip), and the dialectic runs ONLY for an
    # ideation event (heartbeats end after the venture check).
    g.add_conditional_edges(
        "metabolic_check", _route_lifecycle,
        {"extinct": END, "continue": "gate_metabolic"},
    )
    g.add_edge("gate_metabolic", "dispo_check")
    g.add_edge("dispo_check", "gate_dispo")
    g.add_edge("gate_dispo", "venture_check")
    g.add_edge("venture_check", "gate_venture")
    g.add_conditional_edges(
        "gate_venture", _route_after_metabolic,
        {"ideate": "visionary", "done": END},
    )

    # Dialectic chain (ideation events only).
    g.add_edge("visionary", "gate_visionary")
    g.add_edge("gate_visionary", "realist")
    g.add_edge("realist", "gate_realist")
    g.add_edge("gate_realist", "synthesizer")
    g.add_edge("synthesizer", "gate_synthesizer")
    g.add_edge("gate_synthesizer", END)

    # Event subgraphs.
    g.add_edge("qualify_reply", "gate_qualify")
    g.add_edge("gate_qualify", END)
    g.add_edge("book_revenue", "metabolic_check")  # a close re-runs metabolism
    g.add_edge("ops_event", "gate_ops")
    g.add_edge("gate_ops", END)
    g.add_edge("escalate", "gate_escalate")  # gate fires the alert + interrupt
    g.add_edge("gate_escalate", END)
    g.add_edge("start_dispo", "gate_start_dispo")
    g.add_edge("gate_start_dispo", END)
    g.add_edge("confirm_buyer", "gate_confirm_buyer")
    g.add_edge("gate_confirm_buyer", END)
    g.add_edge("propose_venture", "gate_propose_venture")
    g.add_edge("gate_propose_venture", END)
    g.add_edge("approve_venture", "gate_approve_venture")
    g.add_edge("gate_approve_venture", END)
    g.add_edge("venture_metrics", "gate_venture_metrics")
    g.add_edge("gate_venture_metrics", END)
    g.add_edge("kill_venture", "gate_kill_venture")
    g.add_edge("gate_kill_venture", END)
    g.add_edge("learn", "gate_learn")
    g.add_edge("gate_learn", END)

    return g.compile(checkpointer=checkpointer)


# Compiled application graph (LangGraph convention: a module-level handle).
app = build_graph()


if __name__ == "__main__":
    # Smoke test: dispatch routing on a fresh genesis state. A heartbeat (the
    # default when no event is set) must run ONLY the metabolic path — no
    # dialectic inference — and with zero revenue the router should drop to
    # Hermes/Saving Mode. An ideation event then runs the full dialectic.
    from state import new_business_state

    genesis = new_business_state(swarm_id="genesis", creator_audit_key="0x0")
    config = {"configurable": {"thread_id": "smoke"}}
    result = app.invoke(genesis, config)  # event None -> heartbeat

    print("-- heartbeat --")
    print("active_model      :", result["llm"]["active_model"])
    print("saving_mode_active:", result["llm"]["saving_mode_active"])
    print("metabolic_ratio   :", result["financials"]["metabolic_ratio"])
    print("icr_stage         :", result["dialectic"]["icr_stage"], "(dialectic untouched)")

    result["event"] = {"type": "ideation", "payload": {}, "received_at": None}
    result = app.invoke(result, config)

    print("-- ideation --")
    print("icr_stage         :", result["dialectic"]["icr_stage"])
    print("transcript lines  :", len(result["dialectic"]["debate_transcript"]))
