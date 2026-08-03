# PHARMA.md — the AI pharma-consulting platform (standalone product, swarm-adjacent)

> Status: **blueprint + runnable v0 scaffold in `pharma/`.** This is the plan of
> record for a subscription analytics product sold to pharma companies. It is
> deliberately **not** a swarm module: client data never touches swarm state,
> and the scaffold is self-contained so it can be extracted into its own repo.
> The swarm's only relationship to it is (optionally) selling it.

## Thesis

Pharma commercial teams pay consulting firms $50k–$500k per engagement and wait
weeks for deliverables that are, mechanically, data analysis plus pattern-matched
frameworks. This product is an **AI analyst team**: a client uploads their
commercial data (sales, CRM exports, market data), asks questions in a chat, and
gets analyses, charts, and consulting-grade reports in minutes — for a monthly
subscription in the $2k–$10k range.

The moat is **not the model** (everyone has Claude). The moat is the **curated
methods library**: a working pharma consultant (the founder's father — the
"trainer") feeds the system his frameworks, past-project methodology, and
corrections, through a human-approved pipeline. The product gets better at
*pharma consulting specifically* in a way a generic chatbot cannot copy.

**Non-goals at v0** (say no early, in writing): no PHI or patient-level clinical
data, no regulatory submissions, no medical or promotional claims generation
(MLR risk). Commercial analytics only — brand performance, field force, market
access / pricing, competitive intelligence.

## Why standalone (not a swarm module)

| The product needs | The swarm has |
|---|---|
| Hard multi-tenant isolation | One global JSON state blob every node can read |
| Client-facing web app + real auth | No frontend; single shared-secret keys |
| A knowledge base with human-gated writes | A self-mutating genome designed to rewrite itself |
| Boring, auditable, sellable-to-pharma infra | An autonomous economic organism |

Conclusion: **imitate the repo's proven patterns, import nothing.**

Pattern lineage (imitated, never imported):

- `tools/dealdesk/api.py` — fail-closed HTTP auth: missing key → refuse all
  requests; unknown token → 401 with no detail.
- `tools/dealflow/agent.py` — the agentic loop with a scoped `Toolbox`; the
  model only ever sees tools already bound to its context.
- `registry.py` — stdlib `sqlite3`, WAL, short-lived connections, explicit
  schema, a scoping column discipline.
- `tools/learn/route.py` — the untrusted-input contract: **ingestion is open,
  adoption is human-gated.**
- `DEALDESK.md` response shaping — deliberate output redaction ("the response
  never includes ARV, comps, or margin") as the precedent for the
  never-crosses-tenants list below.

## The core design: three-layer knowledge architecture

```
Layer 1  GLOBAL METHODS LIBRARY   Anonymized frameworks, playbooks, templates.
                                  Written ONLY through the trainer approval
                                  gate. Read by every tenant's agent.

Layer 2  CLIENT WORKSPACE         One per tenant: datasets, conversations,
                                  analyses, deliverables, client-specific
                                  learnings and corrections. Read/write only
                                  within the tenant.

Layer 3  ENGAGEMENT CONTEXT       One conversation/analysis: retrieved methods
                                  + tenant data + chat history. Ephemeral;
                                  persisted only into Layer 2.
```

**The invariant, verbatim, everywhere:** data flows *down* (global → client →
engagement) freely; flows *up* only through the human-approved anonymization
gate; **never sideways between clients.**

## Tenant isolation model

- **Database-per-tenant SQLite.** Each tenant gets
  `var/tenants/{tenant_id}/` (mode `0700`) containing `tenant.db`, `uploads/`,
  `workspace/`, `deliverables/`. Cross-tenant queries are impossible by
  construction — there is no shared table with a `tenant_id` column to forget
  in a WHERE clause.
- **App-level enforcement.** Auth token → sha256 → key row → `tenant_id`.
  `tenant_db(tenant_id)` is the *only* function that opens a tenant database;
  it validates the id against a strict regex **and** the control-plane tenants
  table before touching the filesystem. Unknown/invalid → refuse, never guess.
- **Agent tools are tenant-bound closures.** The tool schemas the model sees
  take no tenant parameter at all — a prompt-injected model *cannot address*
  another tenant.
- **LLM calls** go to the Anthropic API under a **zero-data-retention
  arrangement**. Note honestly: ZDR is a contractual/org-level configuration,
  not an API parameter — set it up with Anthropic before onboarding real
  clients. API-submitted data is not used for training; ZDR removes retention.
- **Encryption at rest, v0 honestly stated:** host full-disk encryption plus
  `0700` directory permissions. Per-tenant encryption keys (SQLCipher or
  file-level) are a pilot-phase upgrade, tracked below.
- **Audit log** records actions, actors, and resource ids/hashes — never
  content.

**What NEVER crosses tenants** (the redaction list): raw data; derived
statistics; chart images; deliverables; learnings and corrections;
conversation text; dataset *schemas*; even the tenant's *name* in any shared
context or prompt.

## The trainer loop (how the expert "trains" it)

Three ingest channels, one gated pipeline:

```
ingest ──────────────┐
  • deck/doc upload  │   draft method      anonymization scan     trainer review
  • trainer chat     ├─→ (status=draft) ─→ (flags names, brands, ─→ & edit (human)
    distillation     │                      figures, emails)          │
  • output           │                                                ▼
    correction ──────┘                              approve (requires anonymized=1)
                                                          │
                                                          ▼
                                              published to search index
                                              (ONLY approved+anonymized rows
                                               are ever retrievable)
```

- A correction filed on a *client's* output lands, by default, in **that
  client's Layer-2 learnings**. Promoting a generalized version to the global
  library is a **separate, explicit act** that goes through the same scan +
  approval gate.
- The automated anonymization scan (regex + one LLM pass) only *flags* — the
  human decides. Ingestion is open; adoption is gated. This is
  `tools/learn/route.py`'s contract applied to knowledge.
- Retrieval v0 is SQLite FTS5 (bm25, top 3 methods) plus the tenant's recent
  learnings (top 5). Embeddings come later behind the same
  `search_methods()` seam.

## Dashboard

Server-rendered (Jinja2 + htmx), boring, fast. No build step.

**Client screens:** Chat (the analyst), Datasets (upload/list CSVs), Deliverables
(gallery + download), History.
**Staff screens:** Trainer console (draft queue, review/approve, trainer chat,
per-tenant correction filing), Admin (tenants, API keys, usage).

## Business model

Monthly subscription per company, metered by analyses.

| Tier | Price | Analyses/mo | Notes |
|---|---|---|---|
| Starter | $2,000 | 20 | single brand/team |
| Growth | $5,000 | 75 | multi-brand |
| Enterprise | custom | custom | SSO, DPA addenda, dedicated support |

Usage is metered in `usage_events` (analyses, tokens, estimated cost); the app
soft-blocks at the tier limit with an upgrade prompt. Stripe billing is
post-v0 — design partners invoice manually.

## Roadmap

| Phase | What | Gate to next |
|---|---|---|
| 0. Scaffold | this build: runnable single-node demo, seeded methods, isolation tests | done in-session |
| 1. Design partner | 1–2 friendly clients from the trainer's network; methods library seeded from his real decks; manual invoicing; DPA template | a design partner uses it weekly |
| 2. Pilot | 3–5 paying tenants; Postgres+RLS migration if load demands; Stripe; deck-export (pptx) deliverables; per-tenant encryption keys; container-sandboxed analysis | design-partner retention + revenue |
| 3. GA | SOC 2 Type I trajectory; SSO; embeddings retrieval; packaged capability modules (market access, competitive intelligence); self-serve onboarding | pilot revenue |

## Compliance posture

- **DPA per client**, from day one — even design partners.
- Commercial pharma data (TRx/NRx, CRM, market share) is usually **not PHI**,
  but handle it as if sensitive: same isolation, same audit trail.
- **GxP / 21 CFR Part 11 are out of scope** and stated as such in contracts.
- **No promotional or medical claims generation** — analytics only. This keeps
  the product outside MLR review workflows.
- SOC 2 down payments already in the scaffold: access control, audit logging,
  fail-closed auth, key hashing.

## Relationship to Aria-Swarm

- **The swarm may sell it, never touch it.** Optionally fire
  `venture_proposed {kind: "pharma_consulting", hypothesis: ...}` through the
  existing trigger path (INTEGRATION.md) so the venture engine can run
  outreach/sales experiments under its normal staged budgets and kill criteria.
  Zero new swarm code required.
- A future one-directional event `pharma_lead` (swarm outreach surfaces a
  prospect → a row in the pharma control plane) is named here but **not built**.
- **Hard rule: no swarm node, tool, or state may ever read `pharma/var/` or any
  pharma database.** `pharma/` is also excluded from any swarm self-modification
  surface — it is not genome.
- **Extraction plan:** `pharma/` has its own `pyproject.toml` and imports
  nothing from the parent repo. Extraction = `git subtree split -P pharma` (or
  plain copy) into a fresh private repo.

## What's real vs. deferred at v0

Real and runnable: fail-closed auth (client/trainer/admin roles), per-tenant
DBs + accessors, methods library with the full draft→scan→approve→publish gate,
subprocess-sandboxed pandas/matplotlib analysis, the analyst agent loop with
tenant-bound tools, CSV upload, deliverables gallery, trainer console, seeded
demo, offline test suite (isolation tests first-class).

Deferred (tracked, not hidden): Stripe billing; pptx ingest (uploads stored,
flagged `needs_manual_extraction`) and pptx deck-export deliverables;
embeddings retrieval; per-tenant encryption keys; container/gVisor sandbox
(v0 is process-level rlimits + env scrub, honestly documented); `pharma_lead`
swarm event.

See `pharma/README.md` for quickstart and the demo script.
