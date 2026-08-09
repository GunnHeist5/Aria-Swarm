-- 0001_init.sql — Reachwell Orchestrator schema.
--
-- Postgres is the source of truth for everything durable: contacts, attempts,
-- numbers, suppression, webhook dedupe, writeback outbox. Redis (arq) is only
-- transport — every queue job re-derives its authority from these tables, so a
-- flushed Redis loses nothing but in-flight timing.

-- ---------------------------------------------------------------------------
-- Small durable KV for operational state: discovered company-field mapping,
-- sync cursors, kill-switch persistence across restarts.
CREATE TABLE kv_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Contacts synced from the JustCall Sales Dialer campaign. Upserted by
-- justcall_contact_id; never re-pulled wholesale after the first full sync.
CREATE TABLE contacts (
    id                  BIGSERIAL PRIMARY KEY,
    justcall_contact_id BIGINT NOT NULL UNIQUE,
    campaign_id         BIGINT NOT NULL,
    name                TEXT,
    phone_e164          TEXT NOT NULL,
    email               TEXT,
    company_name        TEXT,
    -- As reported by JustCall: Active | DNCA | Invalid
    contact_status      TEXT,
    -- As reported by JustCall: Undialed | Dialed | Skipped
    progress_status     TEXT,
    -- first  = Undialed audience, second = rep-worked (Dialed) audience.
    touch_type          TEXT NOT NULL CHECK (touch_type IN ('first', 'second')),
    -- REQUIRED to dial, NO default anywhere. NULL = never dialable.
    consent_basis       TEXT,
    custom_fields       JSONB NOT NULL DEFAULT '[]',
    justcall_created_at TIMESTAMPTZ,
    first_synced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX contacts_phone_idx ON contacts (phone_e164);
CREATE INDEX contacts_audience_idx ON contacts (touch_type, contact_status, progress_status);

-- ---------------------------------------------------------------------------
-- Permanent do-not-contact ledger. Inserted on any opt-out, any "stop calling",
-- any rep-marked disqualification, and any JustCall DNCA status. There is
-- deliberately NO delete path in application code.
CREATE TABLE suppression (
    id         BIGSERIAL PRIMARY KEY,
    phone_e164 TEXT NOT NULL UNIQUE,
    reason     TEXT NOT NULL,   -- opt_out | rep_disqualified | dnca | complaint | manual | invalid
    source     TEXT NOT NULL,   -- e.g. webhook:<call_id>, relay:<call_sid>, sync, cli
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Caller-ID pool: local Twilio numbers, picked by prospect area code.
CREATE TABLE numbers (
    id            BIGSERIAL PRIMARY KEY,
    phone_e164    TEXT NOT NULL UNIQUE,
    area_code     TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT 'twilio',
    provider_sid  TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'benched', 'retired')),
    benched_reason TEXT,
    -- NULL means "use the configured PER_NUMBER_DAILY_CAP".
    daily_cap     INTEGER,
    purchased_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);
CREATE INDEX numbers_area_code_idx ON numbers (area_code) WHERE status = 'active';

-- Per-number per-day usage counters; the atomic gate for daily caps and the
-- data behind connect-rate health checks.
CREATE TABLE number_usage (
    number_id  BIGINT NOT NULL REFERENCES numbers(id) ON DELETE CASCADE,
    usage_date DATE NOT NULL,
    dials      INTEGER NOT NULL DEFAULT 0,
    connects   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (number_id, usage_date)
);

-- ---------------------------------------------------------------------------
-- One row per planned/executed dial. The idempotency_key unique index is the
-- guarantee that a crash or duplicate webhook can never produce a second dial:
-- reserve the row first, only then talk to the provider.
CREATE TABLE call_attempts (
    id               BIGSERIAL PRIMARY KEY,
    -- contact:{contact_id}:touch:{touch}:attempt:{n}
    idempotency_key  TEXT NOT NULL UNIQUE,
    contact_id       BIGINT NOT NULL REFERENCES contacts(id),
    touch_type       TEXT NOT NULL CHECK (touch_type IN ('first', 'second')),
    attempt_number   INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN
        ('scheduled',   -- planned; scheduler will enqueue when due
         'queued',      -- handed to the queue
         'dialing',     -- worker committed to dialing (pre-provider-ack)
         'in_progress', -- provider accepted; call live
         'completed',   -- terminal: finished with an outcome
         'failed',      -- terminal: provider/transport error
         'canceled',    -- terminal: compliance gate or operator canceled
         'orphaned')),  -- terminal: stuck 'dialing' with no provider ack; needs reconciliation
    provider         TEXT,
    provider_call_id TEXT UNIQUE,
    from_number_id   BIGINT REFERENCES numbers(id),
    to_phone_e164    TEXT NOT NULL,
    opener_variant   TEXT,
    consent_basis    TEXT,          -- snapshot at dial time, for the audit trail
    scheduled_for    TIMESTAMPTZ,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    -- connected | no_answer | busy | voicemail | failed | canceled
    outcome          TEXT,
    -- structured result: booked | interested | not_interested | opt_out |
    -- disqualified | callback | wrong_number | no_answer | voicemail | failed
    disposition      TEXT,
    duration_sec     INTEGER,
    transcript       TEXT,
    recording_url    TEXT,
    cost_voice_usd        NUMERIC(10,5),
    cost_relay_usd        NUMERIC(10,5),
    cost_intelligence_usd NUMERIC(10,5),
    cost_llm_usd          NUMERIC(10,5),
    cost_total_usd        NUMERIC(10,5),
    error            TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (contact_id, touch_type, attempt_number)
);
CREATE INDEX call_attempts_due_idx ON call_attempts (status, scheduled_for);
CREATE INDEX call_attempts_contact_idx ON call_attempts (contact_id);
CREATE INDEX call_attempts_phone_idx ON call_attempts (to_phone_e164);

-- ---------------------------------------------------------------------------
-- Inbound webhook dedupe. Events are not order-guaranteed and CAN duplicate;
-- the unique constraint makes every handler idempotent by construction.
CREATE TABLE webhook_events (
    id           BIGSERIAL PRIMARY KEY,
    source       TEXT NOT NULL,        -- justcall | twilio
    external_id  TEXT NOT NULL,        -- JustCall call id / Twilio CallSid
    event_type   TEXT NOT NULL,
    payload      JSONB NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    UNIQUE (source, external_id, event_type)
);

-- ---------------------------------------------------------------------------
-- Reliable writeback (outbox pattern): results pushed to JustCall, DNCA moves,
-- booked meetings to calendar/CRM. Rows are drained by a worker cron with
-- retry/backoff so the campaign always converges to reality.
CREATE TABLE writeback_outbox (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,   -- justcall_disposition | justcall_dnca | crm_booking
    attempt_id BIGINT REFERENCES call_attempts(id),
    payload    JSONB NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    tries      INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ
);
CREATE INDEX writeback_outbox_pending_idx ON writeback_outbox (status, created_at)
    WHERE status = 'pending';
