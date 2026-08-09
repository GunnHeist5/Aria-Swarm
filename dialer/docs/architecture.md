# Architecture & module contract

This document is the binding contract between modules. If you change a public
signature here, update this file in the same commit. Read `SPEC.md` first for
the requirements; this file says *how* they are laid out in code.

## Big picture

```
JustCall (contact DB + rep tool + webhooks)          Twilio (voice)
      │  sync / writeback / DNCA                        │ calls, status callbacks,
      ▼                                                 ▼ ConversationRelay ws
┌─────────────────────────── orchestrator ────────────────────────────┐
│ justcall/   voice/twilio/   llm/    agent/    compliance/   pool/   │
│      \           |            |        |          |          /      │
│       └───── queueing (arq workers over Redis) ── pacing ───┘       │
│                        │                                            │
│                Postgres = source of truth                           │
│   (contacts, call_attempts, numbers, suppression, outbox, events)   │
│                                                                     │
│ server.py (FastAPI: webhooks + /ws/relay)   cli.py   dryrun/ report/│
└─────────────────────────────────────────────────────────────────────┘
```

Principles:

- **Postgres is authoritative; Redis is transport.** Any queue job re-checks
  row state before acting. Flushing Redis must never cause a wrong dial or a
  lost result.
- **Fail closed.** Missing DNC source, missing consent basis, unknown area
  code, unverifiable webhook signature — all block, never warn.
- **Sync DB + HTTP core, async edges.** `db.py`, the JustCall client, and the
  Twilio SDK are synchronous. Async contexts (FastAPI handlers, arq jobs) wrap
  them with `await asyncio.to_thread(...)`. Only the ConversationRelay
  websocket + Anthropic streaming are natively async.
- **Nothing outside `voice/` imports a provider SDK; nothing outside `llm/`
  imports the anthropic SDK.**
- **Every entry point** calls `logging_utils.setup_logging(cfg)` then
  `db.migrate(cfg)` before touching data.

## Attempt lifecycle (the reliability core)

```
planner ──▶ scheduled ──scheduler_tick──▶ queued ──dial_attempt──▶ dialing
                ▲                                       │  (gate re-check,
                │   temporal block / pacing defer       │   number lease,
                └───────────────────────────────────────┤   commit BEFORE
                                                        ▼   provider call)
     canceled ◀── permanent block          provider.start_call()
                                                        │
                                        in_progress ◀───┘──▶ failed (retry per
                                             │                schedule, capped)
                     twilio status callback  ▼
                                        completed ──▶ writeback outbox, costs,
                                                      suppression on opt-out,
              orphaned ◀── reconciler (stuck rows)    retry/followup scheduling
```

- `idempotency_key = f"contact:{contact_id}:touch:{touch}:attempt:{n}"`.
  The DB uniques on it AND on `(contact_id, touch_type, attempt_number)`.
- arq job id for a dial = the idempotency key → arq dedupes concurrent
  enqueues; the job itself exits unless the row is in `scheduled`/`queued`.
- The worker sets `status='dialing'` and **commits before** calling the
  provider. A crash between commit and provider-ack leaves a `dialing` row
  that is never redialed automatically — the reconciler resolves it against
  the provider (marks `orphaned` + alert if unresolvable).

## Module contract

Signatures below are binding. `cfg` is always `orchestrator.config.Settings`.
Types referenced are from `orchestrator.models` unless noted.

### orchestrator/justcall/  (owner: agent A)

`client.py`

```python
class JustCallError(RuntimeError): ...
class JustCallClient:
    def __init__(self, cfg: Settings): ...   # requires justcall key+secret
    def fetch_campaign_contacts(self, *, progress_status: str | None = None,
                                contact_status: str | None = None,
                                per_page: int = 100) -> Iterator[dict]: ...
    def set_contact_dnca(self, justcall_contact_id: int) -> None: ...
    def add_contact_note(self, justcall_contact_id: int, note: str) -> None: ...
```

Rate-limited (token bucket, ~2 req/s) + retry with exponential backoff on
429/5xx/network. Auth header exactly `Authorization: <key>:<secret>`. Never log
headers or the key. Base URL `https://api.justcall.io/v2.1`; endpoint paths per
SPEC §1. Pagination: `page` starts at 0; stop when a page returns fewer than
`per_page` rows (also stop on empty).

`sync.py`

```python
@dataclass
class SyncStats: fetched: int; created: int; updated: int; suppressed: int; company_field_key: str | None
def run_sync(cfg: Settings, *, full: bool = False) -> SyncStats: ...
def discover_company_field(sample_contacts: list[dict], override: str | None) -> str | None: ...
def extract_company_name(contact: dict, field_key: str | None) -> str | None: ...
```

- Maps `progress_status` Undialed→`TouchType.FIRST`, Dialed→`TouchType.SECOND`;
  Skipped contacts are stored but belong to no audience (touch by progress at
  plan time — store as `first` with progress_status='Skipped'; planner filters).
- Normalizes phones to E.164 (`+1XXXXXXXXXX`); unparseable → keep row, planner
  blocks with INVALID_PHONE.
- Stamps `consent_basis` from `consent_basis_first_touch` /
  `consent_basis_second_touch` (leave NULL when unset — never invent one). Do
  not overwrite a non-NULL consent basis on re-sync.
- Contact status DNCA ⇒ `compliance.suppression.suppress(phone, "dnca", "sync")`.
- Company field: use `cfg.justcall_company_field_key` if set, else
  `discover_company_field()` over the first synced page — score custom_fields
  whose key/label matches `company|business` (case-insensitive); persist the
  choice in `kv_state['justcall.company_field']`, log it loudly. Ambiguous or
  no match → None (company_name stays NULL; script falls back to
  "your business").
- Incremental: store max `justcall_created_at` seen per audience in kv_state,
  but always upsert whatever pages are fetched; `--full` re-pulls everything.

`webhook.py`

```python
def verify_signature(raw_body: bytes, headers: Mapping[str, str], secret: str) -> bool: ...
def handle_call_completed(cfg: Settings, payload: dict) -> str: ...  # returns "duplicate"|"ignored"|"scheduled"|"suppressed"
```

- Signature: HMAC-SHA256 of the raw body with `justcall_webhook_secret`,
  compared constant-time (`hmac.compare_digest`) against a hex/base64 digest
  taken from the first present header among `x-justcall-signature`,
  `x-webhook-signature`, `x-signature`. No secret configured ⇒ verification
  fails (fail closed). Server returns 401 on failure.
- Dedupe: insert into `webhook_events (source='justcall', external_id=<call id
  or call_sid>, event_type)` — `ON CONFLICT DO NOTHING`; conflict ⇒ return
  "duplicate" without side effects.
- Opt-out/DNC-ish dispositions (case-insensitive contains any of: "not
  interested - do not call", "do not call", "dnc", "remove") ⇒ suppress +
  outbox `justcall_dnca`.
- Disposition in `cfg.justcall_qualifying_dispositions` ⇒ create a
  second-touch `call_attempts` row: `status='scheduled'`, `scheduled_for` =
  now + `followup_delay_hours`, clamped forward into the prospect's next legal
  window via `compliance.windows.next_window_open`. attempt_number = next free
  for that contact/touch. Contact not in DB yet ⇒ return "ignored".

`writeback.py`

```python
def enqueue_writeback(cfg: Settings, kind: str, attempt_id: int | None, payload: dict) -> int: ...
def process_outbox_batch(cfg: Settings, limit: int = 20) -> int: ...  # rows successfully sent
```

Kinds: `justcall_dnca` (payload `{justcall_contact_id}`) → `set_contact_dnca`;
`justcall_disposition` (payload `{justcall_contact_id, note}`) →
`add_contact_note`; `crm_booking` → `CrmSink` stub (log + mark sent, target
TBD per SPEC §7). Failure: increment tries, keep `pending` until 8 tries then
`failed` + alert.

### orchestrator/compliance/  (owner: agent B)

`__init__.py`

```python
def check_dial_allowed(cfg: Settings, contact: Mapping, *, now: datetime | None = None) -> DialDecision: ...
```

`contact` is a `contacts` row (dict). Runs ALL checks, collects every reason
(don't short-circuit after one), sets `earliest_allowed` when the only blocks
are temporal. Order run: kill switch → phase gate (phase 1 blocks everything;
phase 2 blocks `TouchType.FIRST`) → contact_status (DNCA/Invalid) → phone
validity → suppression → consent basis (row's `consent_basis` non-null) → DNC
source available + number not listed → area code known/US → calling window →
attempt cap (`attempts.attempts_for_phone` ≥ `max_attempts_total`).

`area_codes.py` — static NANP data:

```python
@dataclass(frozen=True)
class AreaCodeInfo: state: str | None; tz: str | None; country: str  # "US"|"CA"|"other"
AREA_CODES: dict[str, AreaCodeInfo]
def info_for_phone(phone_e164: str) -> AreaCodeInfo | None: ...
```

Cover all active US area codes with their primary IANA timezone; for area
codes spanning timezones pick the more restrictive/eastern zone and note it in
a comment. Canadian + Caribbean NANP codes map to country != "US" (blocked
unless `allow_non_us_nanp`). Unknown area code → None → block.

`windows.py`

```python
def window_for_state(cfg: Settings, state: str | None) -> tuple[time, time]: ...
def is_within_window(cfg: Settings, phone_e164: str, now: datetime) -> bool: ...
def next_window_open(cfg: Settings, phone_e164: str, now: datetime) -> datetime | None: ...
```

Uses `zoneinfo`; state overrides load from `state_windows.json`
(`{"FL": {"start": "08:00", "end": "20:00"}, ...}` — ship a starter table with
the well-known stricter states and comments). `next_window_open` returns None
when the zone is unknown. Weekends: allowed by default (config could narrow
later); document it.

`dnc.py`

```python
def get_dnc_checker(cfg: Settings) -> DncChecker: ...
class FileDncChecker: ...      # loads DNC_LIST_PATH: one number/line, 10-digit or E.164, '#' comments
class MissingDncChecker: ...   # available() -> False; gate emits DNC_SOURCE_MISSING
```

`suppression.py` (DB-backed, permanent)

```python
def is_suppressed(cfg: Settings, phone_e164: str) -> bool: ...
def suppress(cfg: Settings, phone_e164: str, reason: str, source: str) -> bool: ...  # False if already present
```

`attempts.py`

```python
def attempts_for_phone(cfg: Settings, phone_e164: str) -> int: ...  # non-canceled attempts, both touches
```

`consent.py`

```python
def consent_basis_for(cfg: Settings, touch: TouchType) -> str | None: ...
def is_two_party_state(cfg: Settings, state: str | None) -> bool: ...  # from two_party_states.json
```

Also owns `config/state_windows.json` and `config/two_party_states.json`
(two-party/all-party consent states list: CA, CT, DE, FL, IL, MD, MA, MI, MT,
NV, NH, OR, PA, WA — cite "verify with counsel" in a comment field).

Phone normalization helper (shared): `compliance.phones.normalize_phone(raw:
str) -> str | None` (E.164 US-biased: strips punctuation, handles leading 1 /
+1; 10-digit → `+1...`; returns None if unparseable). Sync (A) imports this.

### orchestrator/pool/ + orchestrator/pacing/  (owner: agent D)

`pool/selector.py`

```python
def pick_number(cfg: Settings, to_phone_e164: str, *, now: datetime | None = None) -> NumberLease | None: ...
def release_unused(cfg: Settings, lease: NumberLease) -> None: ...   # decrement if dial never happened
def record_connect(cfg: Settings, number_id: int) -> None: ...
```

Selection: active numbers with same area code first (round-robin by
`last_used_at` asc), else any active number round-robin. Enforced atomically
(row lock on `numbers` + upsert `number_usage`): per-day cap = min(row
daily_cap or cfg cap, ramp allowance by age in days per `ramp_schedule`;
beyond the schedule ⇒ full cap), cooldown `number_cooldown_seconds` via
`last_used_at`. Increments `number_usage.dials` and `last_used_at` inside the
same transaction (that *is* the lease).

`pool/health.py`

```python
@dataclass
class BenchAction: number_id: int; phone_e164: str; connect_rate: float; pool_avg: float
def auto_bench(cfg: Settings, *, window_days: int = 7) -> list[BenchAction]: ...
```

Benches (status='benched', reason) any number with ≥ `bench_min_dials` dials
in the window whose connect rate < `bench_relative_threshold` × pool average;
sends one alert per action via `queueing.alerts.send_alert`.

`pacing/limiter.py`

```python
def try_acquire_call_slot(cfg: Settings) -> bool: ...   # CPM token bucket in Redis
```

Fixed-window counter `INCR pacing:cpm:{unix_minute}` + EXPIRE 120s; allowed
while count ≤ `calls_per_minute`. Concurrency is enforced by arq
`max_jobs=cfg.effective_concurrency()` — do not build a second semaphore.

`queueing/killswitch.py`

```python
def engage(cfg: Settings, reason: str) -> None: ...   # Redis flag + kv_state (survives restart)
def release(cfg: Settings) -> None: ...
def is_engaged(cfg: Settings) -> bool: ...            # True on Redis errors (fail closed) unless kv says released
```

`queueing/alerts.py`

```python
def send_alert(cfg: Settings, text: str) -> None: ...  # POST {"text": ...} to alert_webhook_url; always logs
```

`queueing/planner.py`

```python
@dataclass
class PlanStats: considered: int; scheduled: int; blocked: dict[str, int]
def plan_touch(cfg: Settings, touch: TouchType, *, limit: int | None = None,
               now: datetime | None = None) -> PlanStats: ...
```

Selects audience rows (first: progress Undialed; second: progress Dialed) with
no live attempt (`scheduled|queued|dialing|in_progress`) and attempt count <
cap. Runs `check_dial_allowed` per contact: permanent block ⇒ skip (tally);
temporal ⇒ schedule at `earliest_allowed`; allowed ⇒ schedule at max(now,
window open), spreading `scheduled_for` so ≤ `calls_per_minute` share any
minute. Assigns `attempt_number`, `opener_variant`
(`agent.script.pick_opener_variant`), `consent_basis` snapshot, idempotency
key. Phase 2: hard-clamp total live second-touch attempts to
`phase2_max_contacts`; refuse `TouchType.FIRST` entirely.

`queueing/worker.py` — arq jobs (async wrappers; sync work through
`asyncio.to_thread`):

```python
async def dial_attempt(ctx, attempt_id: int) -> str: ...
async def scheduler_tick(ctx) -> int: ...    # cron every 30s: due scheduled → queued + enqueue dial jobs (job_id=idempotency_key)
async def reconcile_tick(ctx) -> int: ...    # cron 10 min: dialing >5min → provider lookup → finalize or orphan+alert; in_progress > max_call_minutes+5 → finalize via provider
async def writeback_tick(ctx) -> int: ...    # cron 1 min: justcall.writeback.process_outbox_batch
async def pool_health_tick(ctx) -> int: ...  # cron 30 min: pool.health.auto_bench
class WorkerSettings: ...                    # functions, cron_jobs, redis from cfg, max_jobs=effective_concurrency, on_startup runs migrate
```

`dial_attempt` sequence (each step documented in code): load row FOR UPDATE
(status must be scheduled/queued else exit "stale") → killswitch → gate
re-check (permanent ⇒ canceled; temporal ⇒ back to scheduled at
earliest_allowed) → `try_acquire_call_slot` else defer 60s → `pick_number`
else defer to tomorrow's window via decision → mark `dialing` + commit →
`get_voice_provider(cfg).start_call(...)` (to_thread) → success: store
provider_call_id, status `in_progress`, started_at → provider exception:
outcome failed, schedule retry via `queueing.retries.schedule_retry`.

`queueing/retries.py`

```python
def schedule_retry(cfg: Settings, attempt_row: Mapping, *, now: datetime | None = None) -> int | None: ...
```

Next attempt_number, delay from `cfg.retry_delays()[n-1]` (exhausted ⇒ None),
clamped into the window, respecting `max_attempts_total`. Used on
no_answer/busy/failed outcomes.

`queueing/completion.py` — shared completion path (called from Twilio status
callback handler and reconciler):

```python
def finalize_attempt(cfg: Settings, attempt_row: Mapping, result: CallResult) -> None: ...
```

Persists CallResult fields + costs (`report.costs.estimate_costs`), sets
status completed/failed, `number_usage.connects` via `pool.selector
.record_connect` when connected, disposition side effects: opt_out/
disqualified ⇒ suppress + outbox dnca; booked ⇒ outbox crm_booking + alert;
no_answer/busy/failed ⇒ `schedule_retry`; always outbox
`justcall_disposition` note. Idempotent: skip if already terminal.

### orchestrator/voice/ + orchestrator/llm/ + orchestrator/agent/  (owner: agent C)

`voice/__init__.py`

```python
def get_voice_provider(cfg: Settings) -> VoiceProvider: ...  # registry keyed by cfg.voice_provider
```

`voice/twilio/provider.py` — `class TwilioProvider` implementing the
`VoiceProvider` protocol.

- `start_call`: `client.calls.create(to=..., from_=..., twiml=<Connect><ConversationRelay ...>,
  status_callback=f"{public_base_url}/webhooks/twilio/status", status_callback_event=[...],
  time_limit=cfg.max_call_minutes*60, machine_detection when amd_enabled)`.
  TwiML built in `voice/twilio/twiml.py`:
  `build_relay_twiml(cfg, *, variables: Mapping[str, str]) -> str` — websocket
  url `wss://<public_base_url host>/ws/relay`, `<Parameter>` per variable
  (attempt_id, company_name, touch_type, opener_variant, two_party_disclose).
- `parse_status_callback(form: Mapping) -> StatusUpdate` (module function):
  maps Twilio CallStatus (`queued|ringing|in-progress|completed|busy|no-answer|
  failed|canceled`) → Outcome (terminal only), CallDuration, AnsweredBy,
  RecordingUrl.
- `fetch_call_details` / `fetch_call_cost_usd` via the REST API (price field;
  absolute value — Twilio reports negative).
- `validate_twilio_signature(cfg, url: str, params: Mapping, signature: str)
  -> bool` using `twilio.request_validator.RequestValidator` (fail closed when
  auth token missing).

`voice/twilio/relay.py`

```python
async def handle_relay_socket(websocket, cfg: Settings) -> None: ...
```

ConversationRelay message protocol (verify against Twilio docs via WebFetch if
reachable; else implement to this shape and mark assumptions): inbound JSON
`{"type": "setup", ...customParameters}` / `{"type": "prompt", "voicePrompt":
str, "last": bool}` / `{"type": "interrupt"}` / `{"type": "dtmf"}` /
`{"type": "error"}`. Outbound `{"type": "text", "token": str, "last": bool}`
streamed from the LLM, `{"type": "end"}` to hang up. Keep a per-connection
transcript; on socket close persist transcript + LLM-extracted `Disposition`
onto the attempt (by attempt_id custom parameter) — do NOT finalize the
attempt (status callback owns that); store `relay:{call_sid}` dedupe row.
Track LLM token usage → `cost_llm_usd` estimate.

`voice/twilio/numbers.py`

```python
def purchase_numbers(cfg: Settings, plan: list[dict]) -> list[str]: ...  # [{"area_code": "614", "count": 2}]
def import_number(cfg: Settings, phone_e164: str) -> None: ...           # register an existing Twilio number
```

Purchases via AvailablePhoneNumbers local search + IncomingPhoneNumbers
create; inserts into `numbers` (ramp starts at purchased_at). Idempotent per
number.

`llm/__init__.py` + `llm/anthropic_client.py`

```python
def get_llm_client(cfg: Settings) -> LlmClient: ...
class AnthropicLlm: ...  # AsyncAnthropic; streams messages.stream tokens; effort via output_config; handles refusal stop_reason by yielding a polite close
def extract_disposition(transcript: str, cfg: Settings) -> Disposition: ...  # small non-streaming classification call; falls back to heuristics offline
```

`agent/script.py`

```python
def build_system_prompt(cfg: Settings, *, touch: TouchType, company_name: str | None,
                        opener_variant: str, two_party_disclose: bool) -> str: ...
def pick_opener_variant(cfg: Settings, contact_id: int) -> str: ...  # stable hash → cfg.opener_variants
```

Prompt assembled from `prompts/agent_core.md` (SPEC §11 verbatim ROLE/
PERSONALITY/STYLE/GUARDRAILS/CALL FLOW), `prompts/openers.md` (named variant
placeholders awaiting Justin's real A/B copy), `prompts/second_touch.md`
(stub: must acknowledge prior contact). Company name defaults to "your
business". AI disclosure is non-negotiable in every variant; recording
disclosure line appended when `two_party_disclose`.

### server + dryrun + report + cli  (owner: agent E)

`server.py`

```python
def create_app(cfg: Settings | None = None) -> FastAPI: ...
```

Routes: `GET /healthz` (db+redis ping); `POST /webhooks/justcall/call-completed`
(401 unless `justcall.webhook.verify_signature`; body → `handle_call_completed`
in to_thread; always 200 with `{"result": ...}` after verification);
`POST /webhooks/twilio/status` (403 unless `validate_twilio_signature`;
`parse_status_callback` → dedupe via webhook_events (source `twilio`,
event_type=CallStatus) → terminal statuses call
`queueing.completion.finalize_attempt` with transcript/disposition already
stored by relay); `WS /ws/relay` → `voice.twilio.relay.handle_relay_socket`.
`python -m orchestrator.cli serve` runs uvicorn.

`dryrun/plan.py`

```python
@dataclass
class DryRunRow:
    contact_id: int; justcall_contact_id: int; name: str | None; company_name: str | None
    phone_e164: str; touch_type: str; state: str | None; local_timezone: str | None
    consent_basis: str | None; caller_id: str | None; caller_id_match: str | None  # "area_code"|"round_robin"|None
    would_dial_at: datetime | None; attempt_number: int
    allowed: bool; blocked_reasons: str  # comma-joined BlockReason values
def build_dry_run(cfg: Settings, touches: list[TouchType], *, now: datetime | None = None,
                  limit: int | None = None) -> list[DryRunRow]: ...
```

Pure simulation — zero writes to numbers/attempts, zero provider calls. Uses
`check_dial_allowed` (with phase gate SKIPPED — a phase-1 dry run must show
what live phases would do; document this as the one deliberate gate
difference), simulated pool state (in-memory copy of `numbers` honoring caps/
ramp/cooldown; empty pool ⇒ caller_id None + NO_NUMBER_AVAILABLE note), and
pacing spread (CPM + concurrency) to project `would_dial_at` per contact in
its local window.

`dryrun/csv_out.py`: `def write_csv(rows: list[DryRunRow], path: Path) -> None`
— header exactly the DryRunRow field names.

`report/costs.py`

```python
def load_rates(cfg: Settings) -> dict: ...   # config/rates.json (E owns the file; per-minute rates + per-call fees)
def estimate_costs(cfg: Settings, *, duration_sec: int | None, provider_voice_usd: float | None,
                   llm_usd: float | None) -> CallCosts: ...
```

`report/metrics.py`

```python
def gather_metrics(cfg: Settings, *, since: datetime | None = None) -> dict: ...
def render_report(metrics: dict) -> str: ...
```

Covers SPEC §9: dials attempted, connect rate, avg duration, cost per dial /
conversation / booked meeting, bookings, per-number health, error rates,
blocked-reason tallies.

`cli.py` — argparse, `main(argv=None) -> int`; every subcommand loads
settings, sets up logging, migrates. Subcommands: `migrate`, `sync [--full]`,
`dry-run [--touch first|second|both] [--limit N] [--out PATH]` (prints
summary + writes CSV), `plan --touch ... [--limit N]`, `serve`, `worker`
(exec arq), `numbers purchase|import|list|bench|unbench`, `report [--since]`,
`kill [--release] [--reason]`, `suppress add|check PHONE`, `readiness`
(secret/name presence, masked — mirrors tools/integrations/secrets.py style).

Also owns `config/rates.json` (voice 0.014, relay 0.07, intelligence 0.025
per minute, per-number monthly 1.15, llm per-Mtok in/out for the default
model) and `config/area_code_targets.example.json`, plus test fixtures
`tests/fixtures/justcall_contacts_page*.json` (realistic Sales Dialer shape).

### packaging & docs  (owner: agent F)

Dockerfile (multi-stage, python:3.12-slim, installs `.`, default CMD
`orchestrator serve`; worker via `orchestrator worker`), docker-compose.yml
(postgres:16, redis:7, app, worker — env from `.env`), Makefile (`install`,
`test`, `sync`, `dry-run`, `serve`, `worker`, `report`), docs/runbook.md
(phase-by-phase operating guide incl. kill switch and JustCall Workflow
setup), docs/trusthub.md (manual Trust Hub/SHAKEN/CNAM checklist gating
phase 3), docs/compliance.md (how each SPEC §6 requirement maps to code; TCPA
notes: AI voice = artificial/prerecorded voice, consent basis is load-bearing,
not legal advice).

## Testing rules

- `tests/` at `dialer/tests/`; plain pytest; **no network, no live Postgres/
  Redis required**. DB-touching tests: mock the `db` functions or accept a
  `cfg` + fake rows; integration tests requiring a real DB must
  `pytest.importorskip`/skip unless `TEST_DATABASE_URL` is set.
- Every owner ships tests for their pure logic (signature verification, window
  math incl. DST + state overrides, DNC file parsing, phone normalization,
  ramp/cap arithmetic, retry schedule, CSV shape, prompt assembly, Twilio
  status mapping, dedupe behavior).
- `tests/conftest.py` (already written) provides `make_settings()`.

## Runtime commands (once built)

```
orchestrator migrate | sync | dry-run | plan | serve | worker |
             numbers ... | report | kill | suppress ... | readiness
```
