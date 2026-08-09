"""arq worker: dial jobs + the scheduler/reconcile/writeback/pool-health crons.

Reliability core (see architecture.md "Attempt lifecycle"):

- The arq job id for a dial IS the attempt's idempotency key, so concurrent
  enqueues collapse into one job; the job itself still exits unless the row is
  `scheduled`/`queued` — Redis is transport, Postgres is authority.
- `keep_result = 0`: a kept result would block re-enqueueing the same job id
  after a deferral (paced / out-of-window / no number), and Postgres — not
  Redis — is the record of what happened.
- `dial_attempt` commits `status='dialing'` BEFORE the provider call. A crash
  in the gap leaves a `dialing` row that is never redialed automatically; the
  reconciler resolves it against the provider (or orphans it + alerts).

Jobs are async wrappers; all real work is sync and runs via
`asyncio.to_thread` (the repo's sync-core/async-edge rule).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from arq import cron
from arq.connections import RedisSettings

from .. import db
from ..config import Settings, load_settings
from ..logging_utils import get_logger, setup_logging
from ..models import CallResult, Outcome
from ..pacing import limiter
from ..pool import health, selector
from . import alerts, completion, killswitch, retries

log = get_logger(__name__)

# Defer intervals (seconds). Paced-out dials retry next minute; blocks with no
# computable reopen time (kill switch, unknown zone) back off a few minutes so
# a stuck row cannot busy-loop through the queue.
_DEFER_PACED_S = 60
_DEFER_NO_EARLIEST_S = 300
_DIALING_STUCK_MINUTES = 5

_LOAD_ATTEMPT_SQL = "SELECT * FROM call_attempts WHERE id = %(id)s FOR UPDATE"
_LOAD_CONTACT_SQL = "SELECT * FROM contacts WHERE id = %(id)s"

_DEFER_SQL = """
UPDATE call_attempts SET status = 'scheduled', scheduled_for = %(when)s, updated_at = now()
WHERE id = %(id)s
"""

_CANCEL_SQL = """
UPDATE call_attempts SET status = 'canceled', error = %(error)s, updated_at = now()
WHERE id = %(id)s
"""

_MARK_DIALING_SQL = """
UPDATE call_attempts
SET status = 'dialing', from_number_id = %(number_id)s, provider = %(provider)s,
    updated_at = now()
WHERE id = %(id)s
"""

_MARK_IN_PROGRESS_SQL = """
UPDATE call_attempts
SET status = 'in_progress', provider_call_id = %(pcid)s, started_at = now(),
    updated_at = now()
WHERE id = %(id)s
"""

_MARK_FAILED_SQL = """
UPDATE call_attempts
SET status = 'failed', outcome = 'failed', error = %(error)s, ended_at = now(),
    updated_at = now()
WHERE id = %(id)s
"""

# SKIP LOCKED: overlapping scheduler ticks claim disjoint rows.
_CLAIM_DUE_SQL = """
UPDATE call_attempts
SET status = 'queued', updated_at = now()
WHERE id IN (
    SELECT id FROM call_attempts
    WHERE status = 'scheduled' AND (scheduled_for IS NULL OR scheduled_for <= now())
    ORDER BY scheduled_for NULLS FIRST
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
RETURNING id, idempotency_key
"""

# Rows stuck in 'queued' (enqueue lost, worker died pre-start): re-enqueue.
# arq's job-id dedupe makes doubles harmless; bumping updated_at rate-limits
# the re-offer to once per stale window.
_REQUEUE_STALE_SQL = """
UPDATE call_attempts SET updated_at = now()
WHERE status = 'queued' AND updated_at < now() - interval '5 minutes'
RETURNING id, idempotency_key
"""

_UNQUEUE_SQL = """
UPDATE call_attempts SET status = 'scheduled', updated_at = now()
WHERE id = %(id)s AND status = 'queued'
"""

_STUCK_DIALING_SQL = """
SELECT * FROM call_attempts
WHERE status = 'dialing' AND updated_at < now() - interval '5 minutes'
ORDER BY id
"""

_OVERTIME_IN_PROGRESS_SQL = """
SELECT * FROM call_attempts
WHERE status = 'in_progress'
  AND started_at < now() - make_interval(mins => %(minutes)s)
ORDER BY id
"""

_ORPHAN_SQL = """
UPDATE call_attempts SET status = 'orphaned', error = %(error)s, updated_at = now()
WHERE id = %(id)s AND status IN ('dialing', 'in_progress')
"""

_RECOVER_IN_PROGRESS_SQL = """
UPDATE call_attempts
SET status = 'in_progress', started_at = COALESCE(started_at, now()), updated_at = now()
WHERE id = %(id)s AND status = 'dialing'
"""


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _cfg_from(ctx: Mapping[str, Any]) -> Settings:
    cfg = ctx.get("cfg")
    return cfg if isinstance(cfg, Settings) else load_settings()


# ---------------------------------------------------------------------------
# dial_attempt — the reserve-before-dial sequence


def _two_party_disclose(cfg: Settings, phone: str) -> bool:
    """Should the agent read the recording-disclosure line on this call?"""
    if not cfg.recording_enabled or cfg.two_party_policy != "disclose":
        return False
    try:
        from ..compliance import area_codes, consent  # lazy: sibling package
        info = area_codes.info_for_phone(phone)
        return consent.is_two_party_state(cfg, info.state if info else None)
    except Exception:
        # Unsure ⇒ disclose. Over-disclosing costs a sentence; under-disclosing
        # in a two-party state is illegal recording.
        log.exception("two-party lookup failed — disclosing (fail closed)")
        return True


def _call_variables(cfg: Settings, row: Mapping, contact: Mapping) -> dict[str, str]:
    # Extensible per SPEC §3 — add keys here, never positional args.
    return {
        "attempt_id": str(row["id"]),
        "company_name": str(contact.get("company_name") or "your business"),
        "touch_type": str(row["touch_type"]),
        "opener_variant": str(row.get("opener_variant") or ""),
        "two_party_disclose": (
            "true" if _two_party_disclose(cfg, str(row["to_phone_e164"])) else "false"
        ),
    }


def _tomorrow_window(cfg: Settings, phone: str, now: datetime) -> datetime:
    """Where a pool-exhausted dial goes: tomorrow, inside the legal window."""
    try:
        from ..compliance import windows  # lazy: sibling package
        when = windows.next_window_open(cfg, phone, now + timedelta(days=1))
    except Exception:
        when = None
    return when or now + timedelta(days=1)


def _dial_attempt_sync(cfg: Settings, attempt_id: int, *, now: datetime | None = None) -> str:
    """The dial sequence. Returns a short outcome tag for the arq log.

    Sequence (each step is a hard ordering requirement):
      1. load row FOR UPDATE — must be scheduled/queued, else exit "stale"
      2. kill switch
      3. compliance gate re-check (permanent ⇒ canceled; temporal ⇒ reschedule)
      4. CPM slot, else defer 60s
      5. number lease, else defer to tomorrow's window
      6. mark 'dialing' + COMMIT  ← the reservation, durable before any
      7. provider.start_call()       provider traffic
      8. success ⇒ in_progress; provider error ⇒ failed + retry schedule
    """
    # Phase gate, defense in depth: phase 1 must place zero calls even if a
    # scheduled row somehow exists (the compliance gate also blocks it).
    if cfg.phase == 1:
        log.warning("phase 1: dial_attempt refused for attempt %d", attempt_id)
        return "phase_gate"

    now = _aware(now) or datetime.now(timezone.utc)
    with db.tx(cfg) as conn:
        row = conn.execute(_LOAD_ATTEMPT_SQL, {"id": attempt_id}).fetchone()
        # Stale/duplicate job: Redis offered work Postgres no longer backs.
        if row is None or row["status"] not in ("scheduled", "queued"):
            return "stale"

        if killswitch.is_engaged(cfg):
            conn.execute(
                _DEFER_SQL,
                {"id": attempt_id, "when": now + timedelta(seconds=_DEFER_NO_EARLIEST_S)},
            )
            return "killswitch"

        contact = conn.execute(_LOAD_CONTACT_SQL, {"id": row["contact_id"]}).fetchone()
        if contact is None:
            conn.execute(_CANCEL_SQL, {"id": attempt_id, "error": "contact row missing"})
            return "canceled"

        # Gate re-check moments before dialing: suppression/killswitch/window
        # state can all have changed since the planner ran.
        from .. import compliance  # lazy: sibling package
        decision = compliance.check_dial_allowed(cfg, contact, now=now)
        if not decision.allowed:
            if decision.permanently_blocked:
                reasons = ",".join(r.value for r in decision.reasons)
                conn.execute(_CANCEL_SQL, {"id": attempt_id, "error": reasons})
                log.info("attempt %d canceled by gate: %s", attempt_id, reasons)
                return "canceled"
            when = _aware(decision.earliest_allowed) or now + timedelta(
                seconds=_DEFER_NO_EARLIEST_S
            )
            conn.execute(_DEFER_SQL, {"id": attempt_id, "when": when})
            return "deferred"

        if not limiter.try_acquire_call_slot(cfg):
            conn.execute(
                _DEFER_SQL,
                {"id": attempt_id, "when": now + timedelta(seconds=_DEFER_PACED_S)},
            )
            return "paced"

        # NOTE: pick_number commits its own transaction. If we crash before
        # the dialing-mark commits below, one usage tick leaks — the safe
        # direction (undercounts a number's remaining budget).
        lease = selector.pick_number(cfg, str(row["to_phone_e164"]), now=now)
        if lease is None:
            when = _tomorrow_window(cfg, str(row["to_phone_e164"]), now)
            conn.execute(_DEFER_SQL, {"id": attempt_id, "when": when})
            return "no_number"

        conn.execute(
            _MARK_DIALING_SQL,
            {"id": attempt_id, "number_id": lease.number_id, "provider": cfg.voice_provider},
        )
    # ---- transaction committed: the 'dialing' reservation is durable BEFORE
    # any provider traffic. A duplicate job now exits "stale" above. ----

    variables = _call_variables(cfg, row, contact)
    try:
        from ..voice import get_voice_provider  # lazy: SDK stays behind voice/
        provider = get_voice_provider(cfg)
        provider_call_id = provider.start_call(
            to_number=str(row["to_phone_e164"]),
            from_number=lease.phone_e164,
            variables=variables,
            attempt_id=attempt_id,
        )
    except Exception as exc:
        log.exception("provider start_call failed for attempt %d", attempt_id)
        selector.release_unused(cfg, lease)  # the dial never happened
        db.execute(cfg, _MARK_FAILED_SQL, {"id": attempt_id, "error": str(exc)[:500]})
        retries.schedule_retry(cfg, {**dict(row), "status": "failed"}, now=now)
        return "failed"

    db.execute(cfg, _MARK_IN_PROGRESS_SQL, {"id": attempt_id, "pcid": provider_call_id})
    log.info("attempt %d dialing via %s → %s", attempt_id, lease.phone_e164[-4:], provider_call_id)
    return "dialed"


async def dial_attempt(ctx: dict, attempt_id: int) -> str:
    cfg = _cfg_from(ctx)
    return await asyncio.to_thread(_dial_attempt_sync, cfg, attempt_id)


# ---------------------------------------------------------------------------
# scheduler_tick — due scheduled rows → queued + arq dial jobs


def _claim_due_sync(cfg: Settings, limit: int) -> list[dict[str, Any]]:
    with db.tx(cfg) as conn:
        claimed = conn.execute(_CLAIM_DUE_SQL, {"limit": limit}).fetchall()
        stale = conn.execute(_REQUEUE_STALE_SQL).fetchall()
    return list(claimed) + list(stale)


async def scheduler_tick(ctx: dict) -> int:
    """Every 30s: move due rows to 'queued' and enqueue their dial jobs."""
    cfg = _cfg_from(ctx)
    if await asyncio.to_thread(killswitch.is_engaged, cfg):
        return 0  # leave rows 'scheduled'; nothing moves while halted
    batch = max(cfg.calls_per_minute * 2, 20)
    rows = await asyncio.to_thread(_claim_due_sync, cfg, batch)
    enqueued = 0
    for row in rows:
        try:
            # _job_id = idempotency key ⇒ arq dedupes concurrent enqueues.
            job = await ctx["redis"].enqueue_job(
                "dial_attempt", int(row["id"]), _job_id=str(row["idempotency_key"])
            )
        except Exception:
            log.exception("enqueue failed for attempt %s — returning to scheduled", row["id"])
            await asyncio.to_thread(db.execute, cfg, _UNQUEUE_SQL, {"id": row["id"]})
            continue
        if job is not None:
            enqueued += 1
    return enqueued


# ---------------------------------------------------------------------------
# reconcile_tick — resolve rows stuck between us and the provider


def _reconcile_sync(cfg: Settings) -> int:
    stuck = db.query(cfg, _STUCK_DIALING_SQL)
    overtime = db.query(
        cfg, _OVERTIME_IN_PROGRESS_SQL, {"minutes": cfg.max_call_minutes + 5}
    )
    if not stuck and not overtime:
        return 0

    provider = None
    try:
        from ..voice import get_voice_provider  # lazy: sibling package
        provider = get_voice_provider(cfg)
    except Exception:
        # Without a provider we can still orphan rows that never got a call id
        # (nothing to look up); rows WITH an id wait for the next tick.
        log.exception("voice provider unavailable — partial reconcile only")

    resolved = 0
    for row in stuck + overtime:
        pcid = row.get("provider_call_id")
        update = None
        if pcid:
            if provider is None:
                continue
            try:
                update = provider.fetch_call_details(str(pcid))
            except Exception:
                # Transient lookup failure: do NOT orphan on a flaky API call.
                log.exception("provider lookup failed for attempt %s", row["id"])
                continue

        if update is not None and update.outcome is not None:
            completion.finalize_attempt(
                cfg,
                row,
                CallResult(
                    provider_call_id=str(pcid),
                    outcome=update.outcome,
                    duration_sec=update.duration_sec,
                    recording_url=update.recording_url,
                ),
            )
            resolved += 1
        elif update is not None and row["status"] == "dialing":
            # Provider knows the call and it is live — we crashed between the
            # provider ack and our in_progress mark. Recover the row.
            db.execute(cfg, _RECOVER_IN_PROGRESS_SQL, {"id": row["id"]})
            resolved += 1
        elif update is not None:
            # in_progress beyond the time limit but the provider still calls
            # it live: leave it for the next tick rather than guess an outcome.
            log.warning("attempt %s still live past its time limit", row["id"])
        else:
            # No provider record at all (or no call id): unresolvable.
            error = f"orphaned by reconciler (status was {row['status']})"
            db.execute(cfg, _ORPHAN_SQL, {"id": row["id"], "error": error})
            alerts.send_alert(
                cfg, f"Attempt {row['id']} orphaned: {row['status']} with no provider record"
            )
            resolved += 1
    return resolved


async def reconcile_tick(ctx: dict) -> int:
    cfg = _cfg_from(ctx)
    return await asyncio.to_thread(_reconcile_sync, cfg)


# ---------------------------------------------------------------------------
# writeback_tick / pool_health_tick


async def writeback_tick(ctx: dict) -> int:
    cfg = _cfg_from(ctx)

    def _run() -> int:
        from ..justcall import writeback  # lazy: sibling package
        return writeback.process_outbox_batch(cfg)

    return await asyncio.to_thread(_run)


async def pool_health_tick(ctx: dict) -> int:
    cfg = _cfg_from(ctx)
    actions = await asyncio.to_thread(health.auto_bench, cfg)
    return len(actions)


# ---------------------------------------------------------------------------
# WorkerSettings — `arq orchestrator.queueing.worker.WorkerSettings`


async def startup(ctx: dict) -> None:
    cfg = load_settings()
    setup_logging(cfg)
    await asyncio.to_thread(db.migrate, cfg)
    ctx["cfg"] = cfg
    log.info(
        "worker up: phase=%d concurrency=%d cpm=%d",
        cfg.phase, cfg.effective_concurrency(), cfg.calls_per_minute,
    )


# Settings are read once at import: this module is the worker entry point, and
# arq consumes WorkerSettings as class attributes.
_cfg = load_settings()


class WorkerSettings:
    functions = [dial_attempt, scheduler_tick, reconcile_tick, writeback_tick, pool_health_tick]
    cron_jobs = [
        cron(scheduler_tick, second={0, 30}),
        cron(reconcile_tick, minute=set(range(0, 60, 10)), second=0),
        cron(writeback_tick, second=0),  # minute unset ⇒ every minute
        cron(pool_health_tick, minute={0, 30}, second=0),
    ]
    redis_settings = RedisSettings.from_dsn(_cfg.redis_url)
    max_jobs = _cfg.effective_concurrency()
    on_startup = startup
    # Job ids are idempotency keys: a kept result would block re-enqueueing a
    # deferred dial. Postgres, not Redis, is the record of what happened.
    keep_result = 0
