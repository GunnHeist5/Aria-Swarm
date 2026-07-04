# MARKETPLACE.md — the AI-agent marketplace (a swarm-run venture)

> Status: **roadmap / not yet built.** This is the plan of record for a
> machine-to-machine (M2M) AI-agent marketplace the swarm builds and runs as a
> venture. First-mover bet on a thin-but-growing ecosystem. Phase 1 (indexer)
> is free and read-only; everything that moves money is gated behind validation
> and the swarm's existing safety constraints.

## Thesis

The AI ecosystem is standardizing on open M2M protocols (A2A / Agent Cards,
MCP, x402 payments over Base). A marketplace here is not a heavy user-facing web
app — it's a **programmatic matchmaker for machines**, buildable for the cost of
a normal software project because the rails are open-source and mostly already
in this repo. The market is **early and thin today, but growing** — so the play
is to build the cheap, read-only *foundation* now (first-mover), and let the
swarm's own validation signal decide when to fund the expensive parts.

## Why it fits this swarm (the rails already exist)

| Need | Already in the repo |
|---|---|
| Autonomous stablecoin payments | **x402** (installed via coinbase-agentkit) + **CDP wallet on Base**, USDC, gasless Paymaster (`tools/wallet.py`) |
| A directory / registry | same shape as the plasmid DB (`registry.py`) |
| Discovery adapters behind a seam | same stub-seam pattern as `tools/learn/fetch.py` |
| Evolvable pricing / negotiation | genome dataclass, like `WholesalingConfig` / `tools/ventures/genome.py` |
| Revenue → the creator's wallet | the `capital.py` dividend stream to `CREATOR_AUDIT_KEY` |
| A staged, killable bet | the venture engine (`tools/ventures/`) |
| A dedicated specialized instance | swarm **replication** (`evolution.replicate`) |

## The organizing decision: it runs AS a venture, then AS a dedicated swarm

The marketplace is **not** bolted on as a special feature. It enters through the
machinery already built:

1. **As a venture.** The swarm fires `venture_proposed` for "AI-agent
   marketplace." The **Phase-1 indexer is its validation stage** — cheap,
   read-only. Staged funding + apoptosis + the autonomy gate keep it from ever
   consuming a big slice of the (wholesaling-funded) treasury without a human
   approving the spend.
2. **As a dedicated swarm.** Once the venture validates (the directory shows
   real agent density and the first transactions clear) AND the treasury is past
   the replication threshold, the swarm **replicates a child dedicated to the
   marketplace**: its own funded wallet, its own genome specialized for
   marketplace ops (pricing / discovery / negotiation, mutated at spawn), its
   own metabolic loop. That child improves its **own** phases via the same
   sandbox/Red-Queen evolution, shares winning genes back via HGT, and is
   apoptosis-killed if it stops earning. The result is a **fleet**: parent =
   wholesaling (the earner), child #1 = marketplace, child #N = the next
   business — each specialized, all sharing the plasmid pool.
   - *Reality check:* replication is coded at the state/ledger level; the live
     **independent-container / Akash deploy is a documented TODO hook**, and the
     parent→child funding transfer is a **CRITICAL_GATE** (real money, above the
     5/50 caps → human-gated).

## Resource envelope (why this can't starve wholesaling)

- **Phase 1 is ~free** — read-only crawl + parse, no money moves.
- Scaling is bounded three ways, structurally: **staged funding** (capital per
  stage, only on signal), **apoptosis** (thin market → dies cheap, refunds), and
  the **graduated-autonomy gate** (big-% spend / real money → human approval).
- Early split ≈ **95% wholesaling / ~0% marketplace**. The marketplace draws
  real capital only after it proves demand; when it does, it spawns a child that
  earns its own keep or dies. Wholesaling funds the treasury; the marketplace
  lives on its own metabolic ratio thereafter.

## Phased roadmap (with the honest risk on each)

| Phase | Module | Money | Risk | Gate to proceed |
|---|---|---|---|---|
| **1. Indexer + Directory** | crawl `.well-known/agent-card.json`, GitHub, public A2A/Olas registries → parse Agent Cards → directory DB | none | 🟢 low (respect robots / rate limits / ToS) | build now when ready |
| **2. Matchmaker** | index capability↔need, emit routes / recommendations (still no money) | none | 🟢 low | directory has useful density |
| **3. x402 payment router + micro-take-rate** | route M2M payments, skim 0.1–1% | **real, high-velocity** | 🔴 high | the wallet-boundary decision below + human sign-off |
| **4. AI Sales Rep** | discovery vectors A–D + outreach + "pay-for-attention" | micro-payments | 🟡 medium | opt-out/rate-limit discipline in place |
| **5. Escrow / arbitration / negotiation / flash swarms** | hold funds, auditor agents, game-theoretic bidding | **custody** | ⛔ highest | legal review (money transmission) |

### The AI Sales Rep discovery vectors (Phase 4)
- **A** — crawl `.well-known/ai-agent.json` / `agent-card.json` paths.
- **B** — watch Base on-chain logs for new agent wallets (CDP/Autonolas/Nevermined signatures).
- **C** — listen to autonomous RFP broadcast layers, auto-generate bids.
- **D** — query global protocol registries (A2A / Olas) and filter by capability.

## Hard decisions & compliance flags (must resolve before the money phases)

1. **Separate marketplace wallet (gate to Phase 3).** The micro-take-rate model
   is high-velocity (thousands of sub-cent tx/min). The swarm's **absolute
   5-USDC/call · 50/day caps** protect the *treasury* and CANNOT run a payment
   router. Resolution: the marketplace gets its **own operating wallet** with a
   purpose-built high-frequency spending policy; only its **net skim** flows back
   into the swarm treasury under the normal caps. This boundary needs explicit
   human sign-off before any money-moving code.
2. **Escrow ≈ money transmission (Phase 5).** Holding strangers' USDC to release
   on completion can be a *licensed* activity by jurisdiction. Non-custodial
   smart-contract escrow mitigates but doesn't eliminate it. Requires legal
   review; it is last and most-gated.
3. **Machine outreach is spam-adjacent (Phase 4).** Pinging arbitrary endpoints
   and paying for attention must honor agent-card opt-outs and rate limits, or it
   torches reputation the way un-warmed cold email would.
4. **Ecosystem maturity is the core bet.** Density is thin today. The indexer is
   the *instrument that measures it* — proceed to Phase 3 only when the directory
   proves enough live, transacting agents to justify the payment infra.

## Phase 1 — concrete first build (when green-lit)

Mirrors the wholesaling/venture discipline: deterministic core + stubbed live
seams, offline-tested here, live on the VPS.

- `tools/marketplace/discovery.py` — `fetch_agent_card(url, *, fetcher=None)`
  (stub offline; live: `.well-known/agent-card.json` HTTP, GitHub search API,
  A2A/Olas registry query) → normalized Agent Card records. Untrusted input —
  parse defensively.
- `tools/marketplace/directory.py` — a SQLite directory (like `registry.py`):
  `upsert_agent`, `list_agents`, dedupe by agent id/endpoint, `density_report()`
  (the validation signal: count, protocols, capabilities, freshness).
- `tools/marketplace/config.py` — `MarketplaceConfig` genome (crawl scope, take
  rate, discovery cadence) — evolvable like `WholesalingConfig`.
- Event: `agent_discovered` (and later `rfp_received`, `payment_routed`) through
  the existing dispatch graph; a heartbeat runs a discovery sweep.
- Revenue hook: reuse the `tools/revenue.py` pattern for the take-rate → treasury
  → `CREATOR_AUDIT_KEY` dividend.
- Secrets: register `GITHUB_TOKEN` (crawl rate limits) etc. via
  `tools/integrations/secrets.py` (names only).

## Sequencing (relative to everything else)

Wholesaling funds the treasury and stays the near-term priority. The marketplace
is a **later, treasury-funded venture**, not a competitor for attention now:

```
wholesaling closes deals → treasury grows past replication threshold
   → Phase-1 indexer validates agent density (free, read-only)
      → venture graduates → replicate a dedicated marketplace swarm
         → it runs + improves its own phases (2→5) on its own metabolic ratio
```

First-mover advantage is captured at Phase 1 (the free probe + directory);
the capital-intensive phases wait for the swarm's own validation signal.
