# Aria Pharma

AI pharma-consulting platform: an analyst agent that works on a client's
uploaded commercial data, guided by a human-curated methods library, behind a
simple web dashboard — with hard per-client isolation.

Product blueprint: [`../PHARMA.md`](../PHARMA.md). This directory is
**self-contained** (own `pyproject.toml`, zero imports from the parent repo)
and is designed to be extracted into its own repository
(`git subtree split -P pharma`).

## Quickstart

```sh
cd pharma
make venv                       # python3 -m venv .venv && pip install -e '.[dev]'
cp .env.example .env            # set ANTHROPIC_API_KEY and PHARMA_SECRET_KEY
make seed                       # demo tenant + client/trainer/admin keys (printed once) + sample data + seed methods
make dev                        # http://127.0.0.1:8100
```

Sign in at `/login` with one of the printed tokens. Clients land on the analyst
chat; trainers on the review console; admins on tenant/key management.

## Demo script

1. `make seed` prints three tokens. Sign in with the **client** token.
2. Datasets → `demo_sales.csv` is preloaded (18 months of synthetic TRx/NRx/sales
   for two brands across five regions — Cardiflow/Southwest declines sharply
   from Jan 2026).
3. Analyst → ask: *"Which region's TRx declined fastest recently, and why might
   that be?"* The agent retrieves the approved "Regional TRx decline triage"
   method, writes pandas code, runs it in the sandbox, saves a chart, and
   answers. (Requires `ANTHROPIC_API_KEY`; without it, the loop is exercised
   offline by the test suite's scripted stub.)
4. Deliverables → download the chart.
5. Sign in with the **trainer** token → file a correction against the analysis
   (lands in that client's private learnings), or ingest a PDF → run the
   anonymization scan → edit → approve. Only approved+anonymized methods are
   ever retrievable.

## Tests

```sh
make test    # offline — no API key, no network
```

Isolation tests are first-class: cross-tenant deliverable access, path
traversal, role escalation, draft-method leakage, sandbox env scrubbing.

## Security posture (v0, honestly stated)

- **Tenant isolation**: one SQLite DB + directory per tenant (`var/tenants/…`,
  mode 0700). `tenant_db()` is the only opener and validates the id against a
  regex and the control-plane table. Agent tools are closures with the tenant
  baked in — the model's tool schemas have no tenant parameter to inject.
- **Auth**: sha256-hashed keys (raw shown once), roles client/trainer/admin,
  fail-closed (unknown → 401, wrong role → 403, unconfigured → 503). Signed
  session cookies re-resolve the key on every request, so revocation and
  tenant suspension take effect immediately.
- **Knowledge gate**: ingestion is open; retrieval requires `approved` +
  `anonymized=1`, published to the search index only at approval time.
  Client-specific corrections stay in that client's workspace; promotion to
  the shared library is a separate explicit act through the same gate.
- **Sandbox**: agent analysis code runs in `python -I` subprocesses with CPU/
  memory rlimits, wall timeout, scrubbed env (no API keys, no proxy vars →
  no network egress path), cwd pinned to the tenant workspace. This is
  **process-level containment, not a VM** — same-user filesystem reads are not
  blocked; container/gVisor isolation is the pilot-phase upgrade (PHARMA.md
  roadmap) and a prerequisite for hostile-tenant threat models.
- **LLM**: Anthropic API. For real client data, configure the org for
  zero-data-retention (contractual, org-level) before onboarding.
- **Audit log**: append-only, ids/hashes only, never content.

## Deferred at v0

Stripe billing (tier limits enforced, payment manual), pptx text extraction
(stored + flagged for manual extraction), deck-export deliverables, embeddings
retrieval (FTS5 today, same `search_methods()` seam), per-tenant encryption
keys, container sandbox, the `pharma_lead` swarm event.
