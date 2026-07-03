# VENTURES.md — the business-creation engine

Once wholesaling funds the treasury (10%/deal), the swarm graduates from
micro-cents to launching real ventures (the creator takes 30% of venture
profit). The engine is the wholesaling pipeline's twin: a deterministic,
genome-configurable brain with **graduated-autonomy governance**, enforced by
the heartbeat. It decides *what* to build and *when to quit*; the live
execution hands land behind a seam.

## The core idea: selection needs cheap deaths

A venture is a falsifiable bet. Most bets are wrong — so the engine is built to
spawn **many cheap experiments**, kill the losers fast and cleanly, and pour
capital into the few that show signal. Three biological mechanics make this work
(on top of the swarm's existing mutation / HGT / Red-Queen / replication):

- **Apoptosis** — every venture carries kill criteria (no revenue by day N,
  weak validation signal, CAC ≥ LTV, max age). The heartbeat checks them each
  tick; a venture that trips one **self-terminates and returns its unspent
  capital to the treasury**. This is what stops zombie bleed.
- **r/K portfolio selection** — spawn many cheap bets (r), up to
  `max_concurrent`; when one clears validation with signal, concentrate capital
  into it (K). Venture portfolio theory *is* biology's reproductive split.
- **Recombination** — blend two winning venture genomes into a hybrid
  (`recombine`), the blueprint analogue of agent-gene crossover.

## Graduated autonomy (the governance rule)

Every venture action resolves to one of three modes — trust is earned by
fitness, and high stakes always escalate (`tools/ventures/autonomy.py`):

| Mode | When | Who acts |
|---|---|---|
| **human_gate** (A) | cost is a big fraction of treasury (≥20%), OR any CRITICAL_GATE action / spend above the 5-per-call·50-per-day wallet caps | a human approves |
| **autonomous** (C) | a *proven* venture-kind (its history cleared the proven threshold) | the swarm, within caps |
| **dev_hands** (B) | everything else | the swarm's hands, bounded by the caps |

The wallet caps are **absolute** (a CLAUDE.md invariant): even a proven,
autonomous venture cannot autonomously spend above $5/call — so cheap
experiments run unattended while **scaling a winner (big money) always surfaces
for a human nod.** That's not a limitation; it's rule A and the safety caps
composing correctly. A venture-kind earns its `proven_score` by producing
revenue-positive ventures that survive apoptosis; enough wins graduate its cheap
stages to full autonomy.

## Lifecycle

```
venture_proposed → [autonomy gate] → validating → (signal≥gate) → live → scaling → graduated
                        │                  │                                          
                   human_gate?        apoptosis (kill criteria) → dead + capital reclaimed
```

Capital is released **stage by stage** (validate → mvp → scale), never upfront;
each stage funds only when the prior stage's signal clears its gate.

## Triggers (see INTEGRATION.md for payloads)

- `venture_proposed` — open a venture (`venture_id`, `kind`, optional
  `hypothesis`/`seed_cap_usd`/`stage_budgets`/`kill_criteria`).
- `venture_validated` — feed observed metrics (`signal`, `revenue_usd`,
  `spent_usd`); a metric crossing a gate or a kill line acts immediately.
- `venture_killed` — manually apoptose a venture.
- Every `heartbeat` runs `venture_tick`: apoptosis, stage advancement, proven-win
  accounting — so a kill or a gate can never silently slip.

## The hands (execution) — seam now, adapters next

`tools/ventures/hands.py` defines the provider-agnostic `Hands` interface:
`run_code` (dev), `web_task` (browser), `api_action` (authed APIs). v1 ships
`StubHands` (logs intent, no spend) so the whole engine is offline-testable. The
live adapters — landing behind this interface in a later increment — are:

- **Claude Code CLI** shell-out → `run_code` (write/deploy software ventures).
- **Composio** → `api_action` (managed OAuth across hundreds of services).
- **Browserbase / Browserless + Playwright / Browser-Use** → `web_task` (the
  "mechanical hands" for anything with no API).

`resolve_autonomy` decides *whether* a hands call runs unattended; the `Hands`
adapter is *how* it happens. The two compose: no adapter ever runs above the
autonomy mode the governance layer already resolved.
