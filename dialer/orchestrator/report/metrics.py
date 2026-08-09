"""The CLI report (SPEC §9): dials, connect rate, durations, costs, bookings,
per-number health, error rates, blocked tallies.

`gather_metrics` is plain SQL aggregation over the tables; `render_report`
formats the dict for a terminal. Keeping them separate means a future
dashboard reuses gather_metrics untouched.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .. import db
from ..config import Settings

_DIALED_STATUSES = "('dialing', 'in_progress', 'completed', 'failed', 'orphaned')"

_SUMMARY_SQL = f"""
SELECT
    count(*) FILTER (WHERE status IN {_DIALED_STATUSES})            AS dials,
    count(*) FILTER (WHERE outcome = 'connected')                   AS connects,
    count(*) FILTER (WHERE disposition = 'booked')                  AS bookings,
    count(*) FILTER (WHERE disposition = 'opt_out')                 AS opt_outs,
    count(*) FILTER (WHERE status = 'failed')                       AS failed,
    count(*) FILTER (WHERE status = 'orphaned')                     AS orphaned,
    count(*) FILTER (WHERE status = 'scheduled')                    AS scheduled,
    count(*) FILTER (WHERE status = 'canceled')                     AS canceled,
    avg(duration_sec) FILTER (WHERE outcome = 'connected')          AS avg_duration_sec,
    coalesce(sum(cost_total_usd), 0)                                AS total_cost_usd
FROM call_attempts
WHERE created_at >= coalesce(%(since)s, '-infinity'::timestamptz)
"""

_BLOCKED_SQL = """
SELECT error FROM call_attempts
WHERE status = 'canceled' AND error IS NOT NULL
  AND created_at >= coalesce(%(since)s, '-infinity'::timestamptz)
"""

_NUMBERS_SQL = """
SELECT n.id, n.phone_e164, n.area_code, n.status, n.benched_reason,
       coalesce(sum(u.dials), 0)    AS dials,
       coalesce(sum(u.connects), 0) AS connects
FROM numbers n
LEFT JOIN number_usage u
       ON u.number_id = n.id AND u.usage_date >= current_date - 7
GROUP BY n.id
ORDER BY n.area_code, n.phone_e164
"""

_OUTBOX_SQL = """
SELECT status, count(*) AS n FROM writeback_outbox GROUP BY status
"""

_SUPPRESSION_SQL = "SELECT count(*) AS n FROM suppression"


def gather_metrics(cfg: Settings, *, since: datetime | None = None) -> dict:
    summary = db.query_one(cfg, _SUMMARY_SQL, {"since": since}) or {}
    dials = int(summary.get("dials") or 0)
    connects = int(summary.get("connects") or 0)
    bookings = int(summary.get("bookings") or 0)
    total_cost = float(summary.get("total_cost_usd") or 0)

    blocked: Counter[str] = Counter()
    for row in db.query(cfg, _BLOCKED_SQL, {"since": since}):
        # canceled attempts carry comma-joined BlockReason values in `error`
        for reason in str(row.get("error") or "").split(","):
            reason = reason.strip()
            if reason:
                blocked[reason] += 1

    numbers = []
    for row in db.query(cfg, _NUMBERS_SQL):
        n_dials = int(row.get("dials") or 0)
        numbers.append(
            {
                "phone_e164": row["phone_e164"],
                "area_code": row["area_code"],
                "status": row["status"],
                "benched_reason": row.get("benched_reason"),
                "dials_7d": n_dials,
                "connects_7d": int(row.get("connects") or 0),
                "connect_rate_7d": (int(row.get("connects") or 0) / n_dials) if n_dials else None,
            }
        )

    outbox = {str(r["status"]): int(r["n"]) for r in db.query(cfg, _OUTBOX_SQL)}
    suppressed_row = db.query_one(cfg, _SUPPRESSION_SQL)

    failed = int(summary.get("failed") or 0)
    orphaned = int(summary.get("orphaned") or 0)
    return {
        "since": since.isoformat() if since else None,
        "dials": dials,
        "connects": connects,
        "connect_rate": (connects / dials) if dials else None,
        "avg_duration_sec": (
            float(summary["avg_duration_sec"]) if summary.get("avg_duration_sec") else None
        ),
        "bookings": bookings,
        "opt_outs": int(summary.get("opt_outs") or 0),
        "scheduled": int(summary.get("scheduled") or 0),
        "canceled": int(summary.get("canceled") or 0),
        "failed": failed,
        "orphaned": orphaned,
        "error_rate": ((failed + orphaned) / dials) if dials else None,
        "total_cost_usd": round(total_cost, 4),
        "cost_per_dial_usd": round(total_cost / dials, 4) if dials else None,
        "cost_per_conversation_usd": round(total_cost / connects, 4) if connects else None,
        "cost_per_booking_usd": round(total_cost / bookings, 4) if bookings else None,
        "blocked": dict(blocked),
        "numbers": numbers,
        "writeback_outbox": outbox,
        "suppressed_total": int(suppressed_row["n"]) if suppressed_row else 0,
    }


def _fmt(value: object, *, pct: bool = False, usd: bool = False) -> str:
    if value is None:
        return "—"
    if pct:
        return f"{float(value) * 100:.1f}%"
    if usd:
        return f"${float(value):.4f}"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def render_report(metrics: dict) -> str:
    lines = [
        "Reachwell Orchestrator — report"
        + (f" (since {metrics['since']})" if metrics.get("since") else ""),
        "",
        f"  dials attempted        {_fmt(metrics['dials'])}",
        f"  connects               {_fmt(metrics['connects'])}"
        f"   (rate {_fmt(metrics['connect_rate'], pct=True)})",
        f"  avg duration (conn.)   {_fmt(metrics['avg_duration_sec'])} s",
        f"  bookings               {_fmt(metrics['bookings'])}",
        f"  opt-outs               {_fmt(metrics['opt_outs'])}",
        f"  failed / orphaned      {_fmt(metrics['failed'])} / {_fmt(metrics['orphaned'])}"
        f"   (error rate {_fmt(metrics['error_rate'], pct=True)})",
        f"  still scheduled        {_fmt(metrics['scheduled'])}",
        "",
        f"  total cost             {_fmt(metrics['total_cost_usd'], usd=True)}",
        f"  cost per dial          {_fmt(metrics['cost_per_dial_usd'], usd=True)}",
        f"  cost per conversation  {_fmt(metrics['cost_per_conversation_usd'], usd=True)}",
        f"  cost per booking       {_fmt(metrics['cost_per_booking_usd'], usd=True)}",
        "",
        f"  suppression ledger     {_fmt(metrics['suppressed_total'])} numbers",
        f"  writeback outbox       {metrics['writeback_outbox'] or '{}'}",
    ]
    if metrics["blocked"]:
        lines.append("")
        lines.append("  blocked (canceled attempts, by reason):")
        for reason, count in sorted(metrics["blocked"].items(), key=lambda kv: -kv[1]):
            lines.append(f"    {reason:<24} {count}")
    lines.append("")
    lines.append("  number pool (last 7 days):")
    if not metrics["numbers"]:
        lines.append("    (no numbers registered)")
    for n in metrics["numbers"]:
        rate = _fmt(n["connect_rate_7d"], pct=True)
        bench = f"  BENCHED: {n['benched_reason']}" if n["status"] == "benched" else ""
        lines.append(
            f"    {n['phone_e164']}  ({n['area_code']})  {n['status']:<7}"
            f" dials {n['dials_7d']:<4} connects {n['connects_7d']:<4} rate {rate}{bench}"
        )
    return "\n".join(lines)
