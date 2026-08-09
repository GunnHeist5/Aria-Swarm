"""Number health: auto-bench caller IDs whose connect rate collapses.

A number whose connect rate drops materially below the pool average is almost
certainly spam-flagged by carriers — continuing to dial from it poisons every
remaining call. Benching is relative (threshold × pool average) rather than an
absolute floor because "normal" connect rates swing with audience and time of
day; the pool average is the only stable baseline we have.

The pool average includes the failing number's own dials. With a handful of
numbers a bad one drags the average down, making the check *more* lenient —
acceptable, because the alternative (excluding self) makes the math depend on
which number you're judging and benches flap when the pool is small.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import Settings
from ..logging_utils import get_logger

log = get_logger(__name__)

_STATS_SQL = """
SELECT n.id, n.phone_e164,
       COALESCE(SUM(u.dials), 0)    AS dials,
       COALESCE(SUM(u.connects), 0) AS connects
FROM numbers n
LEFT JOIN number_usage u
       ON u.number_id = n.id AND u.usage_date >= %(since)s
WHERE n.status = 'active'
GROUP BY n.id, n.phone_e164
ORDER BY n.id
"""

# The status guard makes a concurrent bench (or operator retire) a no-op.
_BENCH_SQL = """
UPDATE numbers SET status = 'benched', benched_reason = %(reason)s
WHERE id = %(number_id)s AND status = 'active'
"""


@dataclass
class BenchAction:
    number_id: int
    phone_e164: str
    connect_rate: float
    pool_avg: float


def auto_bench(cfg: Settings, *, window_days: int = 7) -> list[BenchAction]:
    """Bench under-performing numbers; one alert per action. Returns actions.

    A number is benched when it has at least `bench_min_dials` dials in the
    window AND its connect rate is below `bench_relative_threshold` × the pool
    average. A pool with zero connects overall produces no benching — there is
    no relative signal, only a uniformly bad day (or a broken upstream), and
    benching the whole pool would leave nothing to dial from.
    """
    since = datetime.now(timezone.utc).date() - timedelta(days=window_days)
    rows = db.query(cfg, _STATS_SQL, {"since": since})
    total_dials = sum(int(r["dials"]) for r in rows)
    total_connects = sum(int(r["connects"]) for r in rows)
    if total_dials == 0 or total_connects == 0:
        return []
    pool_avg = total_connects / total_dials
    floor = cfg.bench_relative_threshold * pool_avg

    actions: list[BenchAction] = []
    for row in rows:
        dials = int(row["dials"])
        if dials < cfg.bench_min_dials:
            continue  # not enough volume to judge
        rate = int(row["connects"]) / dials
        if rate >= floor:
            continue
        reason = (
            f"auto-bench: connect rate {rate:.3f} < "
            f"{cfg.bench_relative_threshold} x pool avg {pool_avg:.3f} "
            f"over {window_days}d ({dials} dials)"
        )
        db.execute(cfg, _BENCH_SQL, {"number_id": row["id"], "reason": reason})
        action = BenchAction(
            number_id=int(row["id"]),
            phone_e164=str(row["phone_e164"]),
            connect_rate=rate,
            pool_avg=pool_avg,
        )
        actions.append(action)
        log.warning("benched number %s: %s", row["phone_e164"], reason)
        _alert(cfg, f"Number {row['phone_e164']} benched — {reason}")
    return actions


def _alert(cfg: Settings, text: str) -> None:
    # Lazy import: queueing is a sibling package (module-contract rule); an
    # unavailable alert channel must never stop the benching itself.
    try:
        from ..queueing import alerts
        alerts.send_alert(cfg, text)
    except Exception:
        log.exception("bench alert delivery failed (bench already applied)")
