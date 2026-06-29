"""graph.py — LangGraph orchestration for the autonomous evolutionary swarm.

This module wires the first executable control flow of the organism:

  START -> metabolic_check -> visionary -> realist -> synthesizer -> END

Two architectural mechanics live here:

  1. The **Hybrid-LLM router** — ``metabolic_check_node`` recomputes the
     metabolic ratio every cycle and hot-swaps the active model backend between
     the premium Claude specialist and the cheap Hermes 3 fleet.
  2. The **Dialectical Ideation (ICR) loop** — Visionary -> Realist ->
     Synthesizer, with hard model assignments per the routing rules in
     ``CLAUDE.md`` (Visionary is unconditionally Hermes; Synthesizer is
     unconditionally Claude).

The node bodies are deterministic placeholders: they perform the routing and
state transitions correctly, but stub the actual LLM prompt invocation so the
topology is runnable and testable without network access or API keys. Real
prompt calls are marked with ``TODO(prompt)``.

The financial formula is NOT re-derived here — it is owned by
``state.compute_metabolic_ratio`` and reused.
"""

from __future__ import annotations

import os
from typing import Any

from langgraph.graph import END, START, StateGraph

from state import BusinessState, ICRStage, compute_metabolic_ratio

# ---------------------------------------------------------------------------
# Routing configuration
# ---------------------------------------------------------------------------

# Cheap open-source target the router falls back to in Saving Mode.
SAVING_MODE_MODEL = "hermes-3-70b"
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

# Map each Hermes backend flag to (hosted model slug, base URL).
HERMES_MODEL_SLUGS = {
    "hermes-3-70b": ("nousresearch/hermes-3-llama-3.1-70b", OPENROUTER_BASE_URL),
    "hermes-3-8b": ("nousresearch/hermes-3-llama-3.1-8b", OPENROUTER_BASE_URL),
    "hermes-3-akash": ("hermes-3", AKASH_HERMES_BASE_URL),
}


# ---------------------------------------------------------------------------
# Dynamic LLM factory
# ---------------------------------------------------------------------------


def get_llm_backend(active_model: str) -> Any:
    """Return a chat model client for ``active_model``.

    Routes the premium specialist flag to ``ChatAnthropic`` and every Hermes
    variant to ``ChatOpenAI`` pointed at an OpenAI-compatible endpoint
    (OpenRouter / Together / self-hosted Akash). Provider classes are imported
    lazily so this module imports without the langchain provider packages
    installed, and so a Saving-Mode run never imports the Claude client it
    isn't using.

    API keys are read from the environment — never hardcoded. Unknown model
    flags raise ``ValueError`` (fail closed; no silent default backend).
    """

    if active_model == SPECIALIST_MODEL:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model="claude-3-5-sonnet-latest",
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            temperature=0.2,
        )

    if active_model in HERMES_MODEL_SLUGS:
        from langchain_openai import ChatOpenAI

        slug, base_url = HERMES_MODEL_SLUGS[active_model]
        # OpenRouter and Together both accept the OpenAI-compatible interface;
        # pick whichever key is present (OpenRouter preferred).
        api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get(
            "TOGETHER_API_KEY"
        )
        return ChatOpenAI(
            model=slug,
            base_url=base_url or None,
            api_key=api_key,
            temperature=0.7,  # uncensored, exploratory brainstorming
        )

    raise ValueError(f"Unknown active_model backend: {active_model!r}")


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


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
    ratio = compute_metabolic_ratio(financials)
    financials["metabolic_ratio"] = ratio

    if ratio < 1.0:
        state["llm"]["active_model"] = SAVING_MODE_MODEL
        state["llm"]["saving_mode_active"] = True
        state["metabolic_state"] = "saving"
    else:
        state["llm"]["active_model"] = SPECIALIST_MODEL
        state["llm"]["saving_mode_active"] = False
        state["metabolic_state"] = "growth"

    return state


def visionary_node(state: BusinessState) -> BusinessState:
    """The Visionary (Catalyst) — maximal-scale, uncensored ideation.

    Hardcoded to Hermes regardless of the routed backend, per the routing
    rules: the Visionary always runs on the uncensored open-source fleet.
    """

    llm = get_llm_backend(SAVING_MODE_MODEL)  # noqa: F841 — wired for TODO(prompt)
    # TODO(prompt): invoke llm with agents/visionary.md against active_blueprint.
    output = "[visionary stub] maximal-scale concept pending live LLM call"

    dialectic = state["dialectic"]
    dialectic["visionary_output"] = output
    dialectic["debate_transcript"].append(f"VISIONARY: {output}")
    dialectic["icr_stage"] = ICRStage.CRITIQUE
    return state


def realist_node(state: BusinessState) -> BusinessState:
    """The Realist (Antagonist) — cynical feasibility critique.

    Uses the currently routed backend (Claude in growth, Hermes in Saving Mode)
    so the critique cost tracks the swarm's metabolic state.
    """

    llm = get_llm_backend(state["llm"]["active_model"])  # noqa: F841 — TODO(prompt)
    # TODO(prompt): invoke llm with agents/realist.md against visionary_output.
    critique = "[realist stub] bottlenecks/API limits/failure vectors pending"

    dialectic = state["dialectic"]
    dialectic["realist_critique"] = critique
    dialectic["debate_transcript"].append(f"REALIST: {critique}")
    dialectic["icr_stage"] = ICRStage.REVISION
    return state


def synthesizer_node(state: BusinessState) -> BusinessState:
    """The Synthesizer (Architect) — concrete upgraded blueprint.

    Forced to the Claude specialist regardless of Saving Mode, per the routing
    rules: final synthesis and sensitive blueprint construction always use the
    premium backend.
    """

    llm = get_llm_backend(SPECIALIST_MODEL)  # noqa: F841 — wired for TODO(prompt)
    # TODO(prompt): invoke llm with agents/synthesizer.md against the transcript.
    blueprint = "[synthesizer stub] patched, concrete blueprint pending"

    dialectic = state["dialectic"]
    dialectic["synthesized_blueprint"] = blueprint
    dialectic["debate_transcript"].append(f"SYNTHESIZER: {blueprint}")
    dialectic["icr_stage"] = ICRStage.COMPLETE
    return state


# ---------------------------------------------------------------------------
# Graph topology
# ---------------------------------------------------------------------------


def build_graph():
    """Construct and compile the swarm graph.

    Deterministic path: START -> metabolic_check -> visionary -> realist ->
    synthesizer -> END. The metabolic check runs first so every dialectical
    cycle ideates under the correct (possibly downgraded) backend.
    """

    g = StateGraph(BusinessState)

    g.add_node("metabolic_check", metabolic_check_node)
    g.add_node("visionary", visionary_node)
    g.add_node("realist", realist_node)
    g.add_node("synthesizer", synthesizer_node)

    g.add_edge(START, "metabolic_check")
    g.add_edge("metabolic_check", "visionary")
    g.add_edge("visionary", "realist")
    g.add_edge("realist", "synthesizer")
    g.add_edge("synthesizer", END)

    return g.compile()


# Compiled application graph (LangGraph convention: a module-level handle).
app = build_graph()


if __name__ == "__main__":
    # Smoke test: run one full cycle on a fresh genesis state. With zero
    # revenue the metabolic router should drop to Hermes and Saving Mode, and
    # the dialectic should advance all the way to COMPLETE.
    from state import new_business_state

    genesis = new_business_state(swarm_id="genesis", creator_audit_key="0x0")
    result = app.invoke(genesis)

    print("active_model      :", result["llm"]["active_model"])
    print("saving_mode_active:", result["llm"]["saving_mode_active"])
    print("metabolic_ratio   :", result["financials"]["metabolic_ratio"])
    print("icr_stage         :", result["dialectic"]["icr_stage"])
    print("transcript lines  :", len(result["dialectic"]["debate_transcript"]))
