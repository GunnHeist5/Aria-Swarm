# Runbook — operating the Reachwell Orchestrator

Phases ship in order and the code enforces it (`PHASE` in `.env`). Do not skip
Phase 1.

## One-time setup

1. `make install` (or `docker compose up -d postgres redis` + the app image).
2. Copy `.env.example` → `.env`. Fill JustCall + Twilio credentials and
   `DATABASE_URL`/`REDIS_URL`. Run `orchestrator readiness` — it prints a
   masked checklist and never shows values.
3. Decide and set the consent bases (`CONSENT_BASIS_FIRST_TOUCH`,
   `CONSENT_BASIS_SECOND_TOUCH`). There is no default on purpose: contacts
   without a consent basis are never dialable. See `docs/compliance.md`.
4. Point `DNC_LIST_PATH` at the federal DNC scrub file for your area codes
   (one number per line). Without it, every dial is blocked — fail closed.

## Phase 1 — dry run (zero calls)

```
orchestrator sync            # pull the campaign into Postgres (paginated,
                             # incremental after the first run)
orchestrator dry-run         # writes out/dryrun_<ts>.csv + a summary table
```

- `sync` logs the discovered company-name custom-field mapping and stores it;
  override with `JUSTCALL_COMPANY_FIELD_KEY` if it guessed wrong.
- The CSV shows, per contact: the caller ID it would get (area-code match or
  round-robin), when it would be dialed (prospect-local window + pacing), and
  every compliance block with its reason.
- Validate the CSV against the real list before spending anything. Expect
  `no_number_available` until numbers are purchased — that is accurate.

## Between Phase 1 and 2

- Purchase local numbers: edit `config/area_code_targets.example.json` (copy
  to your own file, set `AREA_CODE_TARGETS_PATH`), then
  `orchestrator numbers purchase`. New numbers ramp per `RAMP_SCHEDULE` —
  do not expect full volume on day one; that is deliberate.
- Work through `docs/trusthub.md` (Trust Hub / SHAKEN-STIR / CNAM). Phase 3
  refuses to run without `TRUST_HUB_CONFIRMED=true`.
- In JustCall, create the campaign Workflow: action **Send Data to Webhook** →
  `https://<PUBLIC_BASE_URL>/webhooks/justcall/call-completed`, fired on the
  post-call conditions you care about (disposition set, call status, attempt
  number, duration). Set the same secret in `JUSTCALL_WEBHOOK_SECRET` —
  unsigned deliveries are rejected.

## Phase 2 — live, small

```
PHASE=2  # in .env; concurrency is clamped to PHASE2_MAX_CONCURRENCY (2)
orchestrator serve    # webhooks + ConversationRelay socket (public HTTPS)
orchestrator worker   # dial worker + crons (separate process/container)
orchestrator plan --touch second --limit 50
```

- Phase 2 refuses the first-touch audience entirely and clamps live
  second-touch attempts to `PHASE2_MAX_CONTACTS` (default 50).
- Watch `orchestrator report` after each session: connect rate, cost per
  conversation, transcripts on the `call_attempts` rows. Tune the script in
  `prompts/` (opener variants are tracked per call for the A/B test).

## Phase 3 — full

- Requires `TRUST_HUB_CONFIRMED=true` (readiness fails otherwise).
- `PHASE=3`, then `orchestrator plan --touch both` on whatever cadence you
  want (cron it). The webhook keeps feeding second-touch follow-ups; the
  worker crons handle retries, writeback, reconciliation, and pool health.

## The kill switch

```
orchestrator kill --reason "spam complaints spiking"   # halts ALL dialing now
orchestrator kill --release
```

Engage works even with Postgres down (Redis-only, loudly logged); release
requires Postgres — a release that can't be persisted didn't happen. Workers
check the switch before every dial and the scheduler stops moving rows.

## Things that page you

- **Orphaned attempts** (alert): a dial reserved but unresolvable against
  Twilio. Investigate in Twilio console; the row is terminal and never
  auto-redialed.
- **Benched numbers** (alert): connect rate collapsed vs pool average —
  usually spam-flagging. Replace the number; don't unbench without a reason.
- **Writeback outbox `failed` rows**: JustCall rejected 8 retries; the
  campaign no longer reflects reality until fixed.

## Data hygiene

- Suppression is permanent and has no delete path in code. That is the point.
- `RECORDING_RETENTION_DAYS` / `TRANSCRIPT_RETENTION_DAYS` are your retention
  knobs; enforcement of purges is an operator cron (`psql` delete) for now.
