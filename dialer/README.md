# Reachwell Orchestrator — AI outbound dialer

Reachwell sells an AI receptionist to US service businesses. Prospects live in
a JustCall Sales Dialer campaign; JustCall cannot attach an AI agent to that
campaign, so this service is the dialer: **JustCall stays the contact
database, rep tool, and event source; Twilio (behind a swappable provider
interface) places every AI call** via ConversationRelay with our own LLM
(Anthropic Claude) as the conversation brain.

Requirements of record: `docs/SPEC.md`. Module contract: `docs/architecture.md`.
Operating guide: `docs/runbook.md`. Compliance map: `docs/compliance.md`.

## Stack

Python 3.11+ · Postgres (source of truth) · Redis + arq (job queue — pure
transport) · FastAPI (webhooks + relay websocket) · Twilio SDK · Anthropic SDK.
Deploys as a single container (`Dockerfile`, `docker-compose.yml`).

## Quick start (Phase 1 — the dry run)

```bash
cd dialer
make install                      # venv + editable install
cp .env.example .env              # fill in JustCall creds at minimum
.venv/bin/orchestrator readiness  # masked config checklist
.venv/bin/orchestrator sync       # campaign → Postgres (incremental after 1st)
.venv/bin/orchestrator dry-run    # → out/dryrun_<ts>.csv, ZERO calls placed
```

The CSV lists, per contact: caller ID it would get (area-code match /
round-robin from the number pool), the projected dial time in the prospect's
local window under the configured pacing, and every compliance block by
reason. Validate it against the real list before spending anything.

## The three phases (enforced in code, not convention)

| Phase | What runs | Gate |
|---|---|---|
| 1 | `sync` + `dry-run` only — the dial worker refuses to dial | `PHASE=1` (default) |
| 2 | Live, small: second-touch audience only, ≤50 live attempts, concurrency clamped to 2 | `PHASE=2` |
| 3 | Full: both audiences, pacing, rotation, webhook-driven second touch, writeback | `PHASE=3` **and** `TRUST_HUB_CONFIRMED=true` (see `docs/trusthub.md`) |

## Hard compliance blocks (never warnings)

Suppression ledger (permanent, no delete path) · federal DNC scrub (missing
list ⇒ all dials blocked) · consent basis required per contact with **no
default** · prospect-local calling windows with per-state overrides · attempt
caps across both touch types · AI disclosure in every opener · two-party-state
recording handling · unknown/non-US area codes blocked. Details:
`docs/compliance.md`.

## Reliability model

Postgres owns all state; Redis can be flushed without a wrong dial or lost
result. One attempt = one row with a unique idempotency key; the worker
commits a `dialing` reservation **before** any provider traffic, so a crash or
duplicate webhook can never double-dial. A reconciler resolves rows stuck
between us and Twilio. `orchestrator kill` halts everything immediately
(fail-closed reads).

## Layout

```
orchestrator/
  justcall/     sync, SHA256-verified webhooks, writeback outbox (→ DNCA, notes)
  compliance/   the gate: windows, DNC, suppression, consent, area codes
  voice/        VoiceProvider interface; twilio/ = calls, TwiML, ConversationRelay
  llm/          Anthropic streaming bridge + disposition extraction
  agent/        system-prompt assembly; prompts/ = SPEC §11 script + opener A/B
  pool/         caller-ID pool: area-code match, caps, ramp, cooldown, auto-bench
  pacing/       calls-per-minute limiter (concurrency = arq max_jobs)
  queueing/     planner, dial worker, retries, completion, kill switch, alerts
  dryrun/       Phase 1 simulation + CSV
  report/       cost model + the SPEC §9 report
  server.py     FastAPI: /webhooks/justcall/call-completed, /webhooks/twilio/status,
                /ws/relay, /healthz
  cli.py        orchestrator <migrate|sync|dry-run|plan|serve|worker|numbers|
                report|kill|suppress|readiness>
```

## Still TODO (needs Justin)

- Real opener variant copy + objection handling + pricing (`prompts/` has
  named placeholders; variants are tracked per call for the A/B test).
- Second-touch script copy (must acknowledge the prior rep contact).
- CRM/calendar target for booked meetings (outbox rows are written;
  `crm_booking` sink is a logging stub until the target is confirmed).
- Federal DNC list subscription (SAN) → `DNC_LIST_PATH`.
- Consent-basis decision with counsel → `CONSENT_BASIS_*`.

## Tests

```bash
make test   # 338 tests; no network, no live Postgres/Redis needed
```
