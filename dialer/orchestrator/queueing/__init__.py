"""Queueing: the operational core that turns contacts into (allowed) dials.

Modules:

- `planner`     — audience rows → `scheduled` call_attempts (gate + spread)
- `worker`      — arq jobs: dial_attempt + the scheduler/reconcile/writeback/
                  pool-health crons
- `retries`     — no_answer/busy/failed → next scheduled attempt, capped
- `completion`  — shared terminal path: persist result, costs, side effects
- `killswitch`  — global halt flag (Redis fast path, kv_state authority)
- `alerts`      — operator notifications (webhook POST + always a log line)

Postgres is authoritative; Redis (arq) is transport. Every job re-checks row
state before acting, so flushing Redis can never cause a wrong dial or lose a
result.
"""
