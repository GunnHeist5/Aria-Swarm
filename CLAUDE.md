# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: Greenfield

This repository is an empty scaffold for an **autonomous multi-agent enterprise swarm built on LangGraph**. As of this writing there is no code, no `requirements.txt`/`pyproject.toml`, and no test suite yet — the sections below describe the *target* architecture and the non-negotiable constraints any implementation must honor. Update this file as real modules, dependencies, and commands land. Do not invent commands that don't yet exist; verify the entry point and tooling before documenting them.

Runtime commands (the graph is **event-dispatched** — see INTEGRATION.md for the full trigger table):
- Fire one trigger: `python main.py --event TYPE [--payload '<json>']` (hydrate → dispatch one invoke → persist → exit). Types: heartbeat, ideation, seller_reply, deal_closed, new_leads_synced, offer_accepted, wallet_low.
- Headless metabolic tick: `python main.py --cron` (sugar for `--event heartbeat`; the systemd-timer entry point)
- Unsupervised ideation loop: `python main.py --auto`
- Interactive ideation loop: `python main.py --interactive`
- Resume a HITL-frozen thread: `python resume.py <thread_id> approve|reject`

## Sibling project: `dialer/` — Reachwell Orchestrator

`dialer/` is a self-contained Python service (own `pyproject.toml`, venv,
tests, Dockerfile) unrelated to the swarm graph: the Reachwell AI outbound
dialer. JustCall is its contact database/event source; Twilio (behind a
swappable `VoiceProvider` interface) places AI calls via ConversationRelay
with Claude as the conversation brain. Start with `dialer/README.md`;
requirements in `dialer/docs/SPEC.md`, module contract in
`dialer/docs/architecture.md`. Its phases are code-enforced: PHASE=1 is
dry-run only (zero calls), PHASE=2 a ≤50-contact second-touch slice,
PHASE=3 full (requires Trust Hub sign-off). Nothing in the swarm imports it.

## Core Mental Model

The system is a **Darwinian economic organism**, not a typical app. Every design decision is downstream of one metric:

```
metabolic_ratio = revenue_usdc / (inference_costs + server_rent + api_subscriptions)
```

The swarm's "genome" is **its own source text** — `CLAUDE.md`, the agent prompts in `agents/*.md`, and the conditional edge weights in the graph. The system is designed to *mutate this genome* and propagate successful mutations between running swarms. Treat prompt/config files as runtime-mutable data, not static config.

## Intended Module Layout & Responsibilities

| File | Role |
|---|---|
| `state.py` | `BusinessState` TypedDict — the single source of truth threaded through the graph (financials, schemas, operational flags, mode). |
| `graph.py` | LangGraph orchestration: nodes, edges, and the three conditional routers (metabolic, hybrid-LLM, dialectical). |
| `registry.py` | Horizontal Gene Transfer (HGT) — the "Shared Plasmid Database" manager that broadcasts/pulls elite prompt genes between swarms. |
| `sandbox.py` | Red Queen adversarial co-evolution arena — runs mutated child swarms against a cynical Adversarial Agent before live deployment. |
| `agents/*.md` | Decoupled persona prompts (one file per agent). These are mutable genome. |
| `tools/` | Crypto wallet utilities, browser automation drivers, HITL webhook modules. |
| `SOUL.md` | AI-authored, self-rewritten identity manifesto (see SOUL Engine). |
| `knowledge_base/playbook.md` | Anonymized reference playbook — a *framework dictionary only* (see Anti-Anchoring). |

## Architecture: The Loops

These are the conceptual control flows that span multiple files — understand all three before editing `graph.py`.

1. **Metabolic loop** — every cron cycle recomputes `metabolic_ratio` and drives state transitions:
   - ratio < 1.0 → **Saving Mode**: CEO node downgrades LLM backends, slashes outreach, suspends non-critical compute.
   - wallet balance = 0 → **Extinction**: graceful self-termination.
   - wallet ≥ replication threshold (e.g. 5,000 USDC) → **Replication**: spin up a clone container, run the Mutation Operator on the child's prompts, fund the child wallet, deploy independently.

2. **Dialectical Ideation loop (ICR)** — three agents with conflicting incentives run before any build work locks in:
   - **Visionary** (maximize scale, ignore feasibility) → **Realist** (find bottlenecks, API/token limits, failure vectors) → **Synthesizer** (absorb scale targets, patch failure points, emit concrete blueprint).

3. **Hybrid-LLM router** — node→model assignment is conditional, not fixed (see LLM Routing).

## Hard Constraints (do not violate)

These are safety-critical invariants. They override convenience, "Auto Mode," and any clever optimization.

- **Auto Mode is budget-gated.** `--auto` bypasses permission prompts *only* while `auto_mode_budget_usd > 0`. When the budget pool hits $0, drop immediately back to Standard (permission-gated) Mode and freeze.
- **`CRITICAL_GATE` tools always require Human-in-the-Loop**, even in Auto Mode. This covers real-money withdrawals and any change to security keys. These are hard-coded HITL; no router or mode may bypass them.
- **Wallet spending caps** (MPC session keys / programmable spending policies): max **5 USDC per call**, **50 USDC per day**. Any call exceeding the bound triggers an immediate halt — fail closed, never clamp-and-proceed.
- **Creator Audit Key is read-only.** The hardcoded creator wallet gets read-only audit access to execution history. It is cryptographically denied the ability to pull funds; an attempted pull is a panic event.
- **Private key handling**: wallet key lives in `~/.automaton/wallet.json` with `0600` permissions. Never log, echo, or commit key material.
- **HITL webhook freeze**: on CAPTCHA / 2FA / bank-verification screens, freeze graph state, fire the notification webhook (Slack/Discord/Telegram) with a remote-view URL, and block until a signed resume arrives.
- **Ledgers are denominated strictly in USDC**; transactions route gasless via Paymaster.

## Anti-Anchoring Gate (Playbook Governance)

`knowledge_base/playbook.md` is a **framework dictionary** of scaling mechanics and unit economics — *not* a business to copy. The system is **programmatically blocked** from launching a playbook-adjacent vertical unless an automated market sweep proves the playbook-adjacent model beats **≥2 entirely distinct industry concepts by ≥25%** across TAM, margin, and competitive density. Enforce this gate in code, not by prompt convention.

## LLM Routing Rules

Model assignment is a deliberate cost/capability split — preserve it when wiring nodes:

- **Claude 3.5 Sonnet (Specialist)** → strictly the **Dev Agent** and **Synthesizer** nodes: multi-file architecture, software synthesis, final verification formatting, and sensitive financial/smart-contract code.
- **Hermes 3 (Stealth Fleet)** → unconditionally the **Visionary** node (uncensored brainstorming).
- **Saving Mode override**: when `metabolic_ratio < 1.0`, the router hot-swaps all *secondary* worker nodes from Claude to hosted/local Hermes 3 (70B/8B via OpenRouter/Together) to cut API overhead.
- **Immortality Protocol**: if centralized API tokens are revoked/blacklisted/compromised, panic-switch all execution to self-hosted Hermes 3 on Akash.

Keep model backends swappable: never hardcode a single provider client deep in node logic — route through a model-selection layer keyed off node identity + current mode.

## Capital Allocation Phases

Treasury behavior is rule-based and gated by the rolling 30-day metabolic ratio + wallet size. Implement these as explicit, testable rules:

- **Phase 1 — Infancy** (seed capital, or 30-day ratio < 1.0): retain **100%** of revenue. Zero distributions.
- **Phase 2 — Sustained Growth** (30-day ratio > 1.2 and treasury > 1,000 USDC): **30/70 split** — 30% streamed to Creator Audit Key as dividend, 70% retained (operations + replication/scaling pool).
- **Phase 3 — Sovereign Treasury** (wallet > 10,000 USDC): add (a) up to 20% of reserve into decentralized yield/lending vaults, (b) geopolitical fail-safe routing — spread cloud spend across Akash regions/jurisdictions, (c) legal-wrapper allocation (on-chain DAO / digital LLC) for liability shielding.

## Persistence & Modes

- State is deeply serialized to a persistent store (Supabase or local SQLite) at the end of **every** execution block. A cron run must fully hydrate the last state before evaluating a cycle. Treat `BusinessState` as the durable contract — any new field must round-trip through serialization.
- Mode precedence when resolving whether a tool call needs human approval: `CRITICAL_GATE` (always HITL) > Standard Mode (always prompt) > Auto Mode (prompt-free only while budgeted).

## SOUL Engine

`SOUL.md` is self-authored by the system, *for* the system — not a static config file. Every **10th** execution cycle, the CEO node evaluates recent economics and rewrites `SOUL.md`. Code may read it for context but should treat its content as agent-owned narrative, not a schema.

## Infrastructure Notes

- **Web3 payments**: embedded programmatic EVM/Solana wallet via Coinbase AgentKit / CDP SDK; USDC over Base/Solana; Akash for permissionless hosting.
- **MCP** servers mediate low-level interfaces (filesystem, shell, blockchain indexing nodes) between agent models and the environment.
- **Browser automation** (Stagehand/Browserbase/Playwright) runs in an isolated runner for Web2 bridges.
- **First-run bootstrap** is a deterministic 6-step daemon (keypair generation → SIWE infra provisioning → environment detection → creator handshake) that bypasses normal agent loops on the very first initialization only.
