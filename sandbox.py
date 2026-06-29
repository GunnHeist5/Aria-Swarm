"""sandbox.py — the Red Queen adversarial co-evolution arena.

Before a mutated prompt (gene) is cleared for live deployment, it must survive a
cynical **Adversarial Agent** that simulates competitive friction and extreme
buyer pushback. This module is the evolutionary loop:

    pull baseline (registry) -> mutate -> evaluate vs adversary -> log score
    -> promote the champion if it beats the baseline and is structurally stable.

It is the *consumer* of `registry.py`: it reads baselines via
``get_active_plasmids``, stores variants via ``register_plasmid`` (with full
lineage), records fitness via ``log_evaluation``, and promotes winners via
``clear_for_production``. It can run out-of-band as an async worker without ever
touching the main execution loop.

All LLM calls (the mutation operator and the adversarial judge) route through
``graph.get_llm_backend`` and are isolated behind small seams, so the whole
co-evolution pipeline is unit-testable offline with deterministic stubs.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import registry
from graph import SAVING_MODE_MODEL, SPECIALIST_MODEL, get_llm_backend
from registry import AGENT_ROLES, DB_PATH

logger = logging.getLogger(__name__)

DEFAULT_ROUNDS = 3
STABILITY_THRESHOLD = 0.75  # min adversarial score to be deemed structurally stable

# ---------------------------------------------------------------------------
# Personas (sandbox-internal genome)
# ---------------------------------------------------------------------------

# The cynical adversary. Must emit a strict, parseable verdict.
ADVERSARY_SYSTEM = """\
You are the Red Queen — a ruthless adversarial market force. Your job is to break
the agent prompt under review by simulating maximal competitive friction, hostile
buyers, eroding margins, and every failure vector a real market would throw at it.

You are handed an agent persona prompt (its role is "{role}"). Judge how well it
would withstand sustained real-world adversarial pressure. Be cynical; reward only
genuine structural resilience, not optimistic language.

Respond in EXACTLY this format, nothing else:
SCORE: <float 0.0-1.0, higher = more resilient>
SURVIVED: <true|false>
CRITIQUE: <one sentence naming the weakest point>"""

# The mutation operator. Must preserve the prompt's structure so the mutated gene
# stays a valid template for prompts.render_agent_prompt.
MUTATION_META = """\
You are a prompt mutation operator evolving a "{role}" agent gene for higher
fitness against adversarial market pressure. Rewrite the prompt below to be more
resilient, sharper, and harder to break — a single targeted mutation, not a
rewrite from scratch.

HARD RULES (violating these kills the gene):
- Preserve every Markdown "## " section header.
- Preserve every {{placeholder}} token EXACTLY (they are runtime injection slots).
- Output ONLY the revised Markdown prompt — no preamble, no commentary.

--- CURRENT GENE ---
{prompt}
--- END GENE ---"""

CROSSOVER_META = """\
You are a genetic crossover operator for "{role}" agent genes. Blend the two
parent prompts below into one offspring that inherits the strongest, most
resilient elements of each.

HARD RULES (violating these kills the gene):
- Preserve every Markdown "## " section header.
- Preserve every {{placeholder}} token EXACTLY.
- Output ONLY the resulting Markdown prompt — no preamble, no commentary.

--- PARENT A ---
{prompt_a}
--- PARENT B ---
{prompt_b}
--- END ---"""


# ---------------------------------------------------------------------------
# LLM seam
# ---------------------------------------------------------------------------


def _invoke(model: str, prompt: str) -> str:
    """Invoke the LLM factory and return text (the single LLM seam)."""

    response = get_llm_backend(model).invoke(prompt)
    content = getattr(response, "content", response)
    return content if isinstance(content, str) else str(content)


# ---------------------------------------------------------------------------
# Mutation operators
# ---------------------------------------------------------------------------


def mutate_prompt(role: str, parent_prompt: str, *, model: str = SAVING_MODE_MODEL) -> str:
    """Produce a single mutated variant of a gene (uses the creative Hermes fleet)."""

    return _invoke(model, MUTATION_META.format(role=role, prompt=parent_prompt)).strip()


def crossover_prompts(
    role: str, prompt_a: str, prompt_b: str, *, model: str = SAVING_MODE_MODEL
) -> str:
    """Blend two parent genes into one offspring (Genetic Crossover)."""

    meta = CROSSOVER_META.format(role=role, prompt_a=prompt_a, prompt_b=prompt_b)
    return _invoke(model, meta).strip()


# ---------------------------------------------------------------------------
# Adversarial evaluation
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    score: float
    survived: bool
    critique: str = ""


def _parse_verdict(text: str) -> Verdict:
    """Parse the adversary's strict SCORE/SURVIVED/CRITIQUE output, fail-closed.

    A malformed/unscored response yields a 0.0 non-survival verdict — an agent we
    cannot confirm survived is treated as failed, never promoted by default.
    """

    score_m = re.search(r"SCORE:\s*([0-9]*\.?[0-9]+)", text, re.IGNORECASE)
    surv_m = re.search(r"SURVIVED:\s*(true|false|yes|no)", text, re.IGNORECASE)
    crit_m = re.search(r"CRITIQUE:\s*(.+)", text, re.IGNORECASE)

    if score_m is None:
        return Verdict(0.0, False, "unparseable adversary verdict")

    score = max(0.0, min(1.0, float(score_m.group(1))))
    if surv_m is not None:
        survived = surv_m.group(1).lower() in ("true", "yes")
    else:
        survived = score >= 0.5
    critique = crit_m.group(1).strip() if crit_m else ""
    return Verdict(score, survived, critique)


def adversarial_evaluate(
    role: str, candidate_prompt: str, *, model: str = SPECIALIST_MODEL
) -> Verdict:
    """Run a candidate gene against the Red Queen and return its fitness verdict."""

    prompt = f"{ADVERSARY_SYSTEM.format(role=role)}\n\n--- AGENT PROMPT ---\n{candidate_prompt}"
    return _parse_verdict(_invoke(model, prompt))


# ---------------------------------------------------------------------------
# Red Queen trial
# ---------------------------------------------------------------------------


@dataclass
class TrialResult:
    role: str
    baseline_hash: str
    baseline_fitness: float
    champion_hash: str
    champion_fitness: float
    promoted: bool
    rounds: list = field(default_factory=list)


def run_red_queen_trial(
    role: str,
    swarm_id: str,
    *,
    rounds: int = DEFAULT_ROUNDS,
    stability_threshold: float = STABILITY_THRESHOLD,
    db_path: Path = DB_PATH,
    mutate_model: str = SAVING_MODE_MODEL,
    adversary_model: str = SPECIALIST_MODEL,
) -> TrialResult:
    """Evolve one role's gene against the adversary; promote a stable champion.

    Pulls the role's production baseline from the registry, then iteratively
    mutates -> evaluates -> logs. Each mutant is registered with lineage back to
    the current champion. If the best mutant beats the baseline AND clears the
    stability threshold, it is cleared for production (hot-swappable genome).
    """

    active = registry.get_active_plasmids(swarm_id, db_path)
    base = active.get(role)
    if base is None:
        raise LookupError(
            f"no production baseline for role {role!r} — run seed_genesis_genome first"
        )

    base_record = registry.get_plasmid(base["plasmid_hash"], db_path)
    base_verdict = adversarial_evaluate(role, base["prompt_text"], model=adversary_model)
    registry.log_evaluation(
        base["plasmid_hash"], base_verdict.score, swarm_id=swarm_id,
        adversarial_survived=base_verdict.survived, notes="baseline", db_path=db_path,
    )

    champ_id = base_record["id"]
    champ_hash = base["plasmid_hash"]
    champ_prompt = base["prompt_text"]
    champ_fitness = base_verdict.score
    base_gen = base_record["generation_id"]

    history = []
    for i in range(1, rounds + 1):
        mutant_text = mutate_prompt(role, champ_prompt, model=mutate_model)
        reg = registry.register_plasmid(
            role, mutant_text, parent_id=champ_id,
            swarm_id=swarm_id, mutation_type=registry.MUTATION_POINT,
            generation_id=base_gen + i, db_path=db_path,
        )
        verdict = adversarial_evaluate(role, mutant_text, model=adversary_model)
        registry.log_evaluation(
            reg["plasmid_hash"], verdict.score, swarm_id=swarm_id,
            adversarial_survived=verdict.survived,
            notes=f"round {i}", db_path=db_path,
        )
        history.append({
            "round": i, "plasmid_hash": reg["plasmid_hash"],
            "score": verdict.score, "survived": verdict.survived,
        })
        logger.info("round %d: score=%.3f survived=%s", i, verdict.score, verdict.survived)

        if verdict.score > champ_fitness:
            champ_id = reg["id"]
            champ_hash = reg["plasmid_hash"]
            champ_prompt = mutant_text
            champ_fitness = verdict.score

    promoted = (
        champ_hash != base["plasmid_hash"]
        and champ_fitness > base_verdict.score
        and champ_fitness >= stability_threshold
    )
    if promoted:
        registry.clear_for_production(champ_hash, db_path)
        logger.info("promoted champion %s (fitness %.3f)", champ_hash[:12], champ_fitness)

    return TrialResult(
        role=role,
        baseline_hash=base["plasmid_hash"],
        baseline_fitness=base_verdict.score,
        champion_hash=champ_hash,
        champion_fitness=champ_fitness,
        promoted=promoted,
        rounds=history,
    )


def run_sandbox_cycle(
    swarm_id: str,
    roles=AGENT_ROLES,
    *,
    rounds: int = DEFAULT_ROUNDS,
    db_path: Path = DB_PATH,
) -> dict:
    """Run a Red Queen trial for each role; return per-role results."""

    return {
        role: run_red_queen_trial(role, swarm_id, rounds=rounds, db_path=db_path)
        for role in roles
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sandbox.py", description="Red Queen adversarial co-evolution arena."
    )
    parser.add_argument("--swarm-id", default="swarm_production_v1")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--seed", action="store_true",
                        help="Seed the genesis genome from agents/*.md first.")
    args = parser.parse_args(argv)

    if args.seed:
        registry.seed_genesis_genome(args.swarm_id)
        print(f"[sandbox] seeded genesis genome for {args.swarm_id}")

    try:
        results = run_sandbox_cycle(args.swarm_id, rounds=args.rounds)
    except Exception as exc:  # graceful when no baseline / no LLM credentials
        print(f"[sandbox] cycle aborted: {exc}")
        return 1

    for role, r in results.items():
        verb = "PROMOTED" if r.promoted else "held"
        print(
            f"[{role:<12}] baseline={r.baseline_fitness:.3f} "
            f"champion={r.champion_fitness:.3f} -> {verb} ({r.champion_hash[:12]})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
