"""Operator CLI: `orchestrator <command>`.

Phase 1 lives entirely here: `sync` then `dry-run` produces the CSV that gets
validated against the real list before a cent is spent. Later phases add
`serve` (webhooks + relay socket), `worker` (arq), `numbers`, `report`,
`kill`, and `suppress`.

Every data-touching command loads settings, configures logging, and runs
migrations first. `kill` and `readiness` deliberately tolerate a broken
database — you must be able to halt dialing and inspect config even when
Postgres is on fire.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import ConfigError, Settings, load_settings
from .logging_utils import get_logger, setup_logging
from .models import TouchType

log = get_logger(__name__)

_SECRET_FIELDS = {
    "justcall": ["justcall_api_key", "justcall_api_secret", "justcall_webhook_secret"],
    "twilio": ["twilio_account_sid", "twilio_auth_token"],
    "llm": ["llm_api_key|anthropic_api_key"],
    "compliance": ["consent_basis_first_touch", "consent_basis_second_touch", "dnc_list_path"],
    "runtime": ["public_base_url", "database_url", "redis_url"],
}


def _prepare(cfg: Settings) -> None:
    setup_logging(cfg)
    applied = db.migrate(cfg)
    if applied:
        log.info("migrations applied: %s", ", ".join(applied))


def _touches(arg: str) -> list[TouchType]:
    if arg == "both":
        return [TouchType.FIRST, TouchType.SECOND]
    return [TouchType(arg)]


# ---------------------------------------------------------------------------
# subcommands


def _cmd_migrate(cfg: Settings, _args: argparse.Namespace) -> int:
    setup_logging(cfg)
    applied = db.migrate(cfg)
    print(f"applied: {applied or 'nothing (up to date)'}")
    return 0


def _cmd_sync(cfg: Settings, args: argparse.Namespace) -> int:
    _prepare(cfg)
    from .justcall import sync

    stats = sync.run_sync(cfg, full=args.full)
    print(
        f"synced: fetched={stats.fetched} created={stats.created} "
        f"updated={stats.updated} suppressed={stats.suppressed} "
        f"company_field={stats.company_field_key or 'UNRESOLVED (fallback: your business)'}"
    )
    return 0


def _cmd_dry_run(cfg: Settings, args: argparse.Namespace) -> int:
    _prepare(cfg)
    from .dryrun import csv_out, plan

    rows = plan.build_dry_run(cfg, _touches(args.touch), limit=args.limit)
    out = Path(args.out) if args.out else Path("out") / (
        f"dryrun_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    )
    csv_out.write_csv(rows, out)

    summary = plan.summarize(rows)
    print(f"dry run: {summary['total']} contacts considered")
    print(f"  dialable now/later : {summary['dialable']}")
    print(f"  with a caller ID   : {summary['with_caller_id']}")
    if summary["blocked"]:
        print("  blocked, by reason:")
        for reason, count in sorted(summary["blocked"].items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<24} {count}")
    print(f"  CSV: {out}")
    print("  ZERO calls were placed.")
    return 0


def _cmd_plan(cfg: Settings, args: argparse.Namespace) -> int:
    _prepare(cfg)
    from .queueing import planner

    for touch in _touches(args.touch):
        stats = planner.plan_touch(cfg, touch, limit=args.limit)
        print(
            f"{touch.value}: considered={stats.considered} scheduled={stats.scheduled} "
            f"blocked={stats.blocked or '{}'}"
        )
    return 0


def _cmd_serve(cfg: Settings, _args: argparse.Namespace) -> int:
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(cfg), host="0.0.0.0", port=cfg.port, log_level="info")
    return 0


def _cmd_worker(cfg: Settings, _args: argparse.Namespace) -> int:
    setup_logging(cfg)
    from arq.worker import run_worker

    from .queueing.worker import WorkerSettings

    run_worker(WorkerSettings)  # blocks; own startup hook migrates
    return 0


def _cmd_numbers(cfg: Settings, args: argparse.Namespace) -> int:
    _prepare(cfg)
    if args.numbers_cmd == "purchase":
        from .voice.twilio import numbers as twilio_numbers

        path = cfg.resolve_path(Path(args.plan) if args.plan else cfg.area_code_targets_path)
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"purchase plan unreadable: {path} ({exc})") from exc
        plan = doc["plan"] if isinstance(doc, dict) else doc
        bought = twilio_numbers.purchase_numbers(cfg, plan)
        print(f"purchased {len(bought)} numbers: {', '.join(bought) or '(none)'}")
    elif args.numbers_cmd == "import":
        from .voice.twilio import numbers as twilio_numbers

        twilio_numbers.import_number(cfg, args.phone)
        print(f"registered {args.phone}")
    elif args.numbers_cmd == "list":
        rows = db.query(cfg, "SELECT * FROM numbers ORDER BY area_code, phone_e164")
        for row in rows:
            print(
                f"{row['phone_e164']}  ({row['area_code']})  {row['status']}"
                + (f"  [{row['benched_reason']}]" if row.get("benched_reason") else "")
            )
        if not rows:
            print("(no numbers registered — see `orchestrator numbers purchase`)")
    elif args.numbers_cmd in ("bench", "unbench"):
        status = "benched" if args.numbers_cmd == "bench" else "active"
        reason = "operator" if status == "benched" else None
        updated = db.execute(
            cfg,
            "UPDATE numbers SET status = %s, benched_reason = %s WHERE phone_e164 = %s",
            (status, reason, args.phone),
        )
        print(f"{args.phone}: {'now ' + status if updated else 'NOT FOUND'}")
        return 0 if updated else 1
    return 0


def _cmd_report(cfg: Settings, args: argparse.Namespace) -> int:
    _prepare(cfg)
    from .report import metrics

    since = datetime.fromisoformat(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    print(metrics.render_report(metrics.gather_metrics(cfg, since=since)))
    return 0


def _cmd_kill(cfg: Settings, args: argparse.Namespace) -> int:
    setup_logging(cfg)
    from .queueing import killswitch

    # Halting must work even when parts of the stack are down; engage/release
    # raise only if BOTH Redis and kv_state are unreachable.
    if args.release:
        killswitch.release(cfg)
        print("kill switch RELEASED — dialing may resume")
    else:
        killswitch.engage(cfg, args.reason or "operator")
        print("kill switch ENGAGED — all dialing halted")
    return 0


def _cmd_suppress(cfg: Settings, args: argparse.Namespace) -> int:
    _prepare(cfg)
    from .compliance import suppression
    from .compliance.phones import normalize_phone

    phone = normalize_phone(args.phone) or args.phone
    if args.suppress_cmd == "add":
        added = suppression.suppress(cfg, phone, "manual", "cli")
        print(f"{phone}: {'suppressed' if added else 'already suppressed'}")
    else:
        print(f"{phone}: {'SUPPRESSED' if suppression.is_suppressed(cfg, phone) else 'not suppressed'}")
    return 0


def _cmd_readiness(cfg: Settings, _args: argparse.Namespace) -> int:
    """The .env checklist — names and masked presence only, never values."""
    setup_logging(cfg)

    def _mask(value: str) -> str:
        return f"set (••••{value[-4:]}, len {len(value)})" if len(value) > 4 else "set"

    print("==== Reachwell Orchestrator readiness (names only; no values) ====")
    print(f"  PHASE={cfg.phase}  TRUST_HUB_CONFIRMED={cfg.trust_hub_confirmed}")
    for group, names in _SECRET_FIELDS.items():
        print(f"  [{group}]")
        for name in names:
            value = None
            for candidate in name.split("|"):
                value = getattr(cfg, candidate, None)
                if value:
                    name = candidate
                    break
            shown = _mask(str(value)) if value else "MISSING"
            print(f"    {name.upper():<28} {shown}")
    if cfg.phase >= 3 and not cfg.trust_hub_confirmed:
        print("  ⛔ PHASE=3 requires TRUST_HUB_CONFIRMED=true (see docs/trusthub.md)")
        return 1
    return 0


# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrator", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="apply database migrations")

    p = sub.add_parser("sync", help="sync JustCall campaign contacts into Postgres")
    p.add_argument("--full", action="store_true", help="re-pull everything (ignore cursors)")

    p = sub.add_parser("dry-run", help="Phase 1: simulate the dial plan, write CSV, zero calls")
    p.add_argument("--touch", choices=["first", "second", "both"], default="both")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None, help="CSV path (default out/dryrun_<ts>.csv)")

    p = sub.add_parser("plan", help="create scheduled attempts for an audience (live phases)")
    p.add_argument("--touch", choices=["first", "second", "both"], required=True)
    p.add_argument("--limit", type=int, default=None)

    sub.add_parser("serve", help="run the webhook/relay server")
    sub.add_parser("worker", help="run the arq dial worker + crons")

    p = sub.add_parser("numbers", help="caller-ID pool management")
    nsub = p.add_subparsers(dest="numbers_cmd", required=True)
    np = nsub.add_parser("purchase", help="buy numbers per the area-code plan")
    np.add_argument("--plan", default=None, help="plan JSON (default AREA_CODE_TARGETS_PATH)")
    np = nsub.add_parser("import", help="register an existing Twilio number")
    np.add_argument("phone")
    nsub.add_parser("list")
    np = nsub.add_parser("bench")
    np.add_argument("phone")
    np = nsub.add_parser("unbench")
    np.add_argument("phone")

    p = sub.add_parser("report", help="observability report (SPEC §9)")
    p.add_argument("--since", default=None, help="ISO timestamp lower bound")

    p = sub.add_parser("kill", help="global kill switch: halt all dialing immediately")
    p.add_argument("--release", action="store_true")
    p.add_argument("--reason", default=None)

    p = sub.add_parser("suppress", help="internal do-not-contact ledger")
    ssub = p.add_subparsers(dest="suppress_cmd", required=True)
    sp = ssub.add_parser("add")
    sp.add_argument("phone")
    sp = ssub.add_parser("check")
    sp.add_argument("phone")

    sub.add_parser("readiness", help="config checklist (masked; no values shown)")
    return parser


_HANDLERS = {
    "migrate": _cmd_migrate,
    "sync": _cmd_sync,
    "dry-run": _cmd_dry_run,
    "plan": _cmd_plan,
    "serve": _cmd_serve,
    "worker": _cmd_worker,
    "numbers": _cmd_numbers,
    "report": _cmd_report,
    "kill": _cmd_kill,
    "suppress": _cmd_suppress,
    "readiness": _cmd_readiness,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_settings()
        return _HANDLERS[args.cmd](cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
