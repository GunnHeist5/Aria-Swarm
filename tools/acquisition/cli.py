"""tools/acquisition/cli.py — `acquire` command surface.

M1 subcommands: ingest / enroll / status / suppress / mark / --check.
M2-M4 subcommands (replies, review, pull, counties) exist as stubs that say
which milestone ships them — the CLI shape is stable from day one.

Examples (VPS)::

    acquire ingest /root/land_exports/*.xlsx --mark-enrolled   # historical import
    acquire ingest /root/leads_inbox/new_pull.xlsx
    acquire enroll --limit 200                # dry-run report
    acquire enroll --limit 200 --push
    acquire suppress add someone@x.com --reason STOP
    acquire mark harris_tx 0440240000280 negotiating --note "verbal at 9k"
    acquire status
    acquire --check
"""

from __future__ import annotations

import argparse
import json

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from . import check as check_mod
from . import enroll as enroll_mod
from . import ledger
from .config import DEFAULT_CONFIG, load_config


def _cmd_ingest(args, config) -> int:
    rc = 0
    for path in args.files:
        try:
            rows = ledger.parse_propstream(path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[ingest] {path}: {exc}")
            rc = 1
            continue
        source = args.source or path.rsplit("/", 1)[-1]
        report = ledger.ingest_rows(rows, source_list=source,
                                    mark_enrolled=args.mark_enrolled)
        print(f"[ingest] {source}: rows={report['rows']} "
              f"inserted={report['inserted']} duplicate={report['duplicate']} "
              f"no_key={report['no_key']}"
              + (" (marked enrolled)" if args.mark_enrolled else ""))
    return rc


def _cmd_enroll(args, config) -> int:
    api_key = ""
    if args.push:
        from ..integrations.secrets import SecretError, get_secret

        try:
            api_key = get_secret("INSTANTLY_API_KEY", required=True)
        except SecretError as exc:
            print(f"[enroll] {exc}")
            return 1
    report = enroll_mod.enroll(
        config, push=args.push, limit=args.limit,
        campaign_id=args.campaign_id,
        include_unscreened=args.include_unscreened, api_key=api_key)
    print(json.dumps(report, indent=2))
    return 1 if report.get("errors") or report.get("aborted") else 0


def _cmd_status(args, config) -> int:
    print(json.dumps(ledger.status_report(), indent=2))
    return 0


def _cmd_suppress(args, config) -> int:
    if args.action == "add":
        flipped = ledger.suppress_value(args.kind, args.value, reason=args.reason)
        print(f"[suppress] {args.kind} added; {flipped} ledger lead(s) suppressed")
        return 0
    entries = sorted(ledger.suppressed_set(args.kind))
    print(f"[suppress] {len(entries)} {args.kind} entr(ies)")
    return 0


def _cmd_mark(args, config) -> int:
    try:
        ledger.set_status(args.county, args.apn.upper(), args.status,
                          note=args.note)
    except ledger.LedgerError as exc:
        print(f"[mark] {exc}")
        return 1
    print(f"[mark] {args.county}/{args.apn} -> {args.status}")
    return 0


def _stub(milestone: str):
    def run(args, config) -> int:
        print(f"[acquire] this subcommand ships with {milestone} — not built yet")
        return 2

    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="acquire", description="ARIA acquisition agent")
    parser.add_argument("--config", default=None, help="YAML config overlay")
    parser.add_argument("--check", action="store_true",
                        help="Run live self-probes and exit")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("ingest", help="Load export file(s) into the ledger")
    p.add_argument("files", nargs="+")
    p.add_argument("--source", default=None,
                   help="source_list label (default: filename)")
    p.add_argument("--mark-enrolled", action="store_true",
                   help="Historical import: leads WITH an email start as "
                        "'enrolled' (they are already in the campaign). "
                        "Never use for fresh pulls.")
    p.set_defaults(run=_cmd_ingest)

    p = sub.add_parser("enroll", help="Push eligible ledger leads to Instantly")
    p.add_argument("--push", action="store_true",
                   help="Actually call the API (default: dry-run)")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--campaign-id", default=None)
    p.add_argument("--include-unscreened", action="store_true",
                   help="Also admit verdict-less leads (pre-enrichment "
                        "workflow). Explicit operator override — logged.")
    p.set_defaults(run=_cmd_enroll)

    p = sub.add_parser("status", help="Ledger counts, quota, suppression")
    p.set_defaults(run=_cmd_status)

    p = sub.add_parser("suppress", help="Manage the suppression table")
    p.add_argument("action", choices=("add", "list"))
    p.add_argument("value", nargs="?", default="")
    p.add_argument("--kind", choices=ledger.SUPPRESSION_KINDS, default="email")
    p.add_argument("--reason", choices=ledger.SUPPRESSION_REASONS,
                   default="manual")
    p.set_defaults(run=_cmd_suppress)

    p = sub.add_parser("mark", help="Set a lead's status (e.g. negotiating)")
    p.add_argument("county", help="county key, e.g. harris_tx")
    p.add_argument("apn")
    p.add_argument("status", choices=ledger.STATUSES)
    p.add_argument("--note", default=None)
    p.set_defaults(run=_cmd_mark)

    for name, milestone in (("replies", "M2"), ("review", "M2"),
                            ("pull", "M3"), ("counties", "M4")):
        p = sub.add_parser(name)
        p.set_defaults(run=_stub(milestone))

    args = parser.parse_args(argv)
    if args.check:
        return check_mod.main()
    if not args.cmd:
        parser.print_help()
        return 2
    config = load_config(args.config) if args.config else DEFAULT_CONFIG
    return args.run(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
