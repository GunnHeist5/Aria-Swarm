# PROJECT: Reachwell AI Outbound Dialer ("Orchestrator") — source requirements

This is the original build brief, kept in-repo as the requirements of record.
Where implementation details differ, `architecture.md` explains why.

## Context

Reachwell sells an AI receptionist to US service businesses (plumbing, drain,
restoration, similar trades). Prospects live in a JustCall Sales Dialer campaign
(ID 3310579, ~7,975 contacts, ~495 already dialed by human reps).

JustCall's own AI voice agent cannot place outbound calls on our plan, and
JustCall has no way to attach an AI agent to a Sales Dialer campaign at any
tier. So we are building the dialer ourselves on Twilio. JustCall remains the
contact database, the human rep tool, and an event source. Twilio does all AI
calling.

Build this so the voice provider is swappable — Twilio first, but behind an
interface.

## Stack

TypeScript/Node (or Python if you prefer) + Postgres + Redis-backed job queue.
Deployable to a single container. No Kubernetes.

## 1. Contact source: JustCall Sales Dialer API v2.1

- Base: `https://api.justcall.io/v2.1`
- Auth: header `Authorization: <api_key>:<api_secret>`, `Accept: application/json`
- Credentials come from env/secret manager. Never in source, never in logs.
- `GET /sales_dialer/campaigns/contacts` with `campaign_id=3310579`,
  `progress_status = Undialed | Dialed | Skipped`,
  `contact_status = Active | DNCA | Invalid`, `per_page`, `page` (page 0 =
  first), `order=asc|desc`. Returns Sales Dialer contact objects: `id`, `name`,
  `phone_number`, `email`, `status`, `created_at`, and `custom_fields[]` as
  `{key, label, type, value}`.
- COMPANY NAME lives in `custom_fields` — discover the exact key on first run,
  log the mapping, make it configurable. Do not hardcode a guess.
- `POST /sales_dialer/campaigns/contacts` adds a contact to a campaign;
  `DELETE /sales_dialer/campaigns/contacts` removes one. Contact status can be
  set to DNCA via the Sales Dialer contacts endpoints.
- Sync contacts into Postgres. Paginate fully, upsert by JustCall contact id,
  incremental thereafter. Never re-pull the whole list on every run.
- Two target audiences, treated as separate campaigns with separate configs,
  pacing, and scripts:
  - FIRST TOUCH → `progress_status=Undialed` (~7,475 records)
  - SECOND TOUCH → `progress_status=Dialed` (~495 records, rep-worked)

## 2. Second-touch trigger (event-driven)

JustCall Sales Dialer supports campaign-scoped Workflows with a "Send Data to
Webhook" action, fired on post-call conditions (Disposition is Set to / Status
of the Call / Call Answered by / Call Attempt number is / Duration of the Call).

- Expose `POST /webhooks/justcall/call-completed` to receive these.
- Verify the payload; JustCall supports SHA256 dynamic webhook signatures.
- Events are NOT order-guaranteed and CAN duplicate. Dedupe on call id/call_sid
  plus event type. Idempotent handler, always.
- On a qualifying disposition, enqueue an AI follow-up call after a
  configurable delay (default: not same-day).

## 3. Voice layer: Twilio

Interface: `startCall(toNumber, fromNumber, variables) -> providerCallId`, plus
a completion webhook returning outcome, duration, transcript, recording URL,
and structured disposition.

Twilio pieces to use:

- Programmable Voice for call origination (~$0.0140/min outbound US local)
- ConversationRelay for the AI conversation layer (~$0.07/min); bring our own LLM
- Phone numbers, local, purchased by area code (~$1.15/mo each)
- Trust Hub: SHAKEN/STIR attestation, CNAM registration so "Reachwell"
  displays, and branded calling. Set this up BEFORE volume dialing — number
  reputation is our biggest operational risk. Our existing JustCall numbers are
  already spam-flagged; do not repeat that mistake.
- Conversational Intelligence for transcription (~$0.024–0.027/min) and for
  generative operators that score calls and extract outcomes.

Variables passed per call: `company_name` (fallback "your business"). Design
the variable payload to be extensible.

## 4. Number pool & local presence

- Maintain a pool of Twilio local numbers with an area_code → number map.
- Pick caller ID by matching the prospect's area code; round-robin fallback
  when no match exists.
- Per-number daily cap (default 150) and cooldown between uses.
- Track connect rate per number. Auto-bench any number whose rate drops
  materially below pool average and alert.
- Config-driven purchase script: given a list of target area codes and counts,
  provision numbers via the Twilio API and register them in the pool.

## 5. Pacing & concurrency

- Max simultaneous calls: configurable, default 5, hard ceiling in config.
- Calls per minute: configurable cap.
- Ramp gradually on new numbers rather than starting at full volume.

## 6. Compliance — implement as hard blocks, not warnings

- Calling window enforced in the PROSPECT's local time, derived from area code.
  Default 9am–5pm local, with a per-state override table.
- Federal DNC scrub plus an internal suppression list checked before EVERY dial.
- Suppression is permanent and immediate on any opt-out, any "stop calling",
  and any rep-marked disqualification. Second-touch lists especially: someone
  who told a human rep no must never be re-dialed by a bot.
- Max total attempts per business across both touch types, configurable.
- The agent MUST disclose it is an AI at the start of every call.
- Recording + transcript retention configurable; honor two-party consent states.
- Log a consent basis per contact as a required field with no default.

## 7. Writeback

On completion: persist outcome, duration, transcript, recording URL,
disposition, and cost. Push results back to JustCall so the campaign reflects
reality. Move opt-outs to DNCA. Send booked meetings to calendar/CRM
(TODO: confirm target).

## 8. Reliability

- Idempotency key per contact per attempt. A crash or duplicate webhook must
  never produce a second dial.
- Queue-backed, resumable, survives restart mid-run.
- Global kill switch: one command halts all dialing immediately.
- Rate-limit and retry with backoff on both JustCall and Twilio APIs.

## 9. Observability

Dashboard or CLI report covering: dials attempted, connect rate, average
duration, cost per dial, cost per conversation, cost per booked meeting,
bookings, per-number health, error rates. Cost tracking is a first-class
requirement, not an extra.

## 10. Config (env or file, nothing hardcoded)

JustCall key/secret, campaign id, Twilio SID/token, concurrency,
calls-per-minute, per-number daily cap, calling window, state overrides, retry
schedule, attempt caps, area-code targets, provider selection, LLM model + key.

## 11. Agent script (ported from the existing JustCall config)

ROLE: You are Reachwell's outbound AI sales representative, calling on behalf
of Justin at Reachwell. Your job is to speak with owners of service businesses,
identify whether missed calls are costing them potential jobs, demonstrate the
value of Reachwell's AI receptionist through the conversation itself, and move
qualified, interested prospects to a scheduled follow-up with Justin.

PERSONALITY: Confident, sharp, conversational, and commercially aware. You
should sound like a capable salesperson who understands small service
businesses, not like a scripted call-center agent.

STYLE GUIDELINES:

- Keep responses engaging, informative, and aligned with instructions.
- Be concise: one topic per reply, avoid multi-question messages.
- Use varied, natural language to stay clear and avoid repetition.
- Proactively guide the conversation — end with a question or next step.
- Clarify vague inputs with follow-up questions.
- Format dates conversationally (e.g., "Friday, Jan 14th").
- Ensure smooth, role-appropriate dialogue.
- Mention the user's full name only once per conversation.
- Resume seamlessly after interruptions.

GUARDRAILS:

- Never deviate from your assigned role or the business context.
- Always follow your defined flow and instructions; do not create new responses.
- Never make promises or commitments beyond approved guidelines.

CALL FLOW:

0. Call context: the business you are calling is `{{CompanyName}}`.
   - Use this name when you confirm who you have reached, for example
     "am I speaking with the owner of {{CompanyName}}?"
   - Say it naturally, and at most once or twice in the entire call.
   - Never read out the variable name or any placeholder text.
   - If the value falls back to "your business", simply say "your business".
1. Start the call by confirming you are speaking with the business owner.
   - Use the assigned opener version exactly.
   - Disclose that you are an AI assistant calling on behalf of Justin at
     Reachwell.
   - Do not mix or improvise between opener versions.
   - After asking what happens to calls they miss, STOP and wait for their
     response.
2. Determine who answered.
   - If it is the owner, continue.
   - If not the owner, ask whether the business has someone answering the
     phones full-time.
   - If they have a full-time in-house receptionist, politely end the call and
     mark the lead disqualified.
   - Do not pitch a gatekeeper or push for a transfer.
   - If they use an outsourced/shared answering service, they may still
     qualify; continue only if speaking with the decision-maker.

TODO — supplied separately by Justin:

- (a) The opener variants (currently in a JustCall knowledge base; they are an
  A/B test, so keep them as discrete named variants and track performance per
  variant).
- (b) Business context / objection handling / pricing.
- (c) The second-touch script variant, which must acknowledge prior contact.

## 12. Deliverables, in order

- Phase 1 — DRY RUN. Sync the campaign, resolve the company-name field,
  compute the caller ID each contact would get, apply all compliance filters,
  and output a CSV of exactly what it WOULD dial and when. Zero calls placed.
  Validate against the real list before spending anything.
- Phase 2 — LIVE, SMALL. Concurrency 1–2 against a ~50-contact slice of the
  second-touch list. Full recording and transcripts. Tune the script.
- Phase 3 — FULL. Pacing, number rotation, Trust Hub registration,
  webhook-driven second touch, writeback, cost dashboard.

Do not skip Phase 1.
