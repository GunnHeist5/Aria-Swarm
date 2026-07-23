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


def _real_llm():
    """The drafting/classify model — same construction as the screener."""

    import os

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=1500, temperature=0.2)


def _cmd_replies(args, config) -> int:
    from ..integrations.instantly import remove_lead_by_email
    from ..integrations.secrets import SecretError, get_secret
    from . import ledger as ledger_mod
    from .reply import classify as classify_mod
    from .reply import ingest as ingest_mod
    from .reply import verify as verify_mod
    from .reply import draft as draft_mod

    try:
        api_key = get_secret("INSTANTLY_API_KEY", required=True)
        campaign_id = get_secret("INSTANTLY_CAMPAIGN_ID", required=True)
    except SecretError as exc:
        print(f"[replies] {exc}")
        return 1
    try:
        llm = _real_llm()
    except Exception as exc:  # noqa: BLE001
        print(f"[replies] LLM unavailable ({exc}) — classification and "
              "drafting need ANTHROPIC_API_KEY; ingest will still run")
        llm = None

    conn = ledger_mod.connect()
    try:
        try:
            r1 = ingest_mod.pull_and_ingest(api_key=api_key,
                                            campaign_id=campaign_id, conn=conn)
        except RuntimeError as exc:
            print(f"[replies] {exc}")
            print("[replies] nothing ingested this run — re-run in a minute "
                  "(Instantly rate limit) or check the API key")
            return 1
        print(f"[replies] ingest: {json.dumps(r1)}")
        if llm is None:
            return 0

        def _remove(email: str):
            return remove_lead_by_email(email, api_key=api_key,
                                        campaign_id=campaign_id)

        r2 = classify_mod.classify_pending(conn=conn, llm=llm,
                                           remove_from_campaign=_remove)
        print(f"[replies] classify: {json.dumps(r2)}")
        r3 = verify_mod.verify_classified(config=config, conn=conn)
        print(f"[replies] verify: {json.dumps(r3)}")
        r4 = draft_mod.draft_verified(conn=conn, llm=llm)
        print(f"[replies] draft: {json.dumps(r4)}")
        n = len([1 for _ in conn.execute(
            "SELECT 1 FROM review_queue WHERE state='pending_review'")])
        print(f"[replies] {n} draft(s) awaiting review — `acquire review`")
        return 0
    finally:
        conn.close()


def _cmd_review(args, config) -> int:
    from . import review as review_mod

    if args.action == "list":
        items = review_mod.pending()
        if not items:
            print("[review] queue is empty")
            return 0
        for it in items:
            print(f"\n=== {it['id']} · {it['lead_email']} · "
                  f"{it['county_key'] or '?'}/{it['apn'] or '?'} · "
                  f"{it['classification'] or it['state']} ===")
            print(f"reply: {(it['reply_text'] or '')[:400]}")
            if it["draft"]:
                print(f"--- draft ({it['draft_kind']}) ---\n{it['draft']}")
            elif it["reason"]:
                print(f"needs manual: {it['reason']}")
        return 0
    if args.action == "digest":
        print(review_mod.digest())
        return 0
    if not args.id:
        print("[review] approve/reject/snooze need an item id")
        return 1
    if args.action == "approve":
        api_key = ""
        if args.send:
            from ..integrations.secrets import SecretError, get_secret

            try:
                api_key = get_secret("INSTANTLY_API_KEY", required=True)
            except SecretError as exc:
                print(f"[review] {exc}")
                return 1
        try:
            result = review_mod.approve(args.id, send=args.send,
                                        api_key=api_key)
        except Exception as exc:  # noqa: BLE001
            print(f"[review] {exc}")
            return 1
        print(json.dumps(result))
        return 0
    if args.action == "reject":
        review_mod.reject(args.id, reason=args.note or "")
        print(f"[review] rejected {args.id}")
        return 0
    until = review_mod.snooze(args.id, days=args.days)
    print(f"[review] snoozed {args.id} until {until}")
    return 0


def _cmd_pull(args, config) -> int:
    from . import ledger as ledger_mod
    from .pull.recipes import get_recipe, manual_checklist

    try:
        recipe = get_recipe(args.recipe)
    except ValueError as exc:
        print(f"[pull] {exc}")
        return 1
    if args.plan:
        used = ledger_mod.quota_used()
        print(manual_checklist(args.county, args.state, recipe))
        print(f"\nquota: {used}/{config.quota_monthly} used this month "
              f"({config.quota_monthly - used} remaining)")
        return 0

    from ..integrations.secrets import SecretError, get_secret

    try:
        username = get_secret("PROPSTREAM_USERNAME", required=True)
        password = get_secret("PROPSTREAM_PASSWORD", required=True)
    except SecretError as exc:
        print(f"[pull] {exc}")
        return 1

    from ..browser.config import load_config as load_browser_config
    from ..browser.driver import BrowserError
    from ..browser.session import real_driver
    from .pull.propstream import run_recipe_pull

    browser_config = load_browser_config()
    try:
        with real_driver(browser_config) as driver:
            report = run_recipe_pull(
                driver, args.county, args.state, recipe_name=args.recipe,
                config=config, browser_config=browser_config,
                username=username, password=password)
    except BrowserError as exc:
        print(f"[pull] {exc}")
        return 1
    print(json.dumps(report, indent=2))
    return 0 if report.get("inbox_file") else 1


def _cmd_counties(args, config) -> int:
    from . import counties as counties_mod

    if args.action == "add":
        if not args.county:
            print("[counties] add needs a county key, e.g. brazoria_tx")
            return 1
        counties_mod.upsert(
            args.county, growth_pct=args.growth_pct,
            median_lot_value=args.median_lot_value, data_cad=args.data_cad,
            data_gis=args.data_gis, data_tax=args.data_tax,
            dispo_listings=args.dispo_listings, notes=args.notes)
        print(f"[counties] {args.county} recorded")
        return 0
    if args.action == "research":
        if not args.county or "_" not in args.county:
            print("[counties] research needs a county key, e.g. brazoria_tx")
            return 1
        name, state = args.county.rsplit("_", 1)
        try:
            from ..screener.comps import BraveClient
            from ..integrations.secrets import get_secret

            brave = BraveClient(get_secret("BRAVE_API_KEY", required=True))
            fields = counties_mod.research_county(
                name, state,
                search=lambda q: brave.search(q))
        except Exception as exc:  # noqa: BLE001
            print(f"[counties] research unavailable: {exc}")
            return 1
        counties_mod.upsert(args.county,
                            growth_pct=fields.get("growth_pct"),
                            notes=fields.get("notes"))
        print(f"[counties] researched {args.county}: "
              f"growth={fields.get('growth_pct')} (notes stored — fill the "
              "rest with `counties add`)")
        return 0
    # score
    cards = counties_mod.scorecard()
    if not cards:
        print("[counties] registry empty — `acquire counties add <key> ...`")
        return 0
    for c in cards:
        gaps = f"  gaps: {', '.join(c['gaps'])}" if c["gaps"] else ""
        sat = "  [GURU-SATURATED]" if c["saturated"] else ""
        print(f"{c['score']:6.1f}  {c['county_key']:20}{sat}{gaps}")
    return 0


def _cmd_enrich(args, config) -> int:
    from . import enrich as enrich_mod
    from . import ledger as ledger_mod

    if args.browse_tasks:
        tasks = enrich_mod.browsing_tasks(args.county, limit=args.limit)
        if not tasks:
            print(f"[enrich] no 'new' leads for {args.county}")
            return 0
        from pathlib import Path

        Path(args.browse_tasks).write_text("\n\n---\n\n".join(tasks),
                                           encoding="utf-8")
        print(f"[enrich] {len(tasks)} browsing task(s) -> {args.browse_tasks}")
        return 0

    run_screener = None
    if enrich_mod.ADAPTER_BY_MARKET.get(args.county):
        from ..screener.cli import COUNTY_ADAPTERS, run_pipeline
        from ..screener.config import DEFAULT_CONFIG as SCREENER_CONFIG
        from ..screener.cache import Cache

        brave = llm = None
        try:
            from ..screener.comps import BraveClient
            from ..integrations.secrets import get_secret

            brave = BraveClient(get_secret("BRAVE_API_KEY", required=True))
            llm = _real_llm()
        except Exception as exc:  # noqa: BLE001
            print(f"[enrich] comps disabled ({exc}) — geometry/flood/roads "
                  "still run; verdicts needing comps go needs_manual")

        def run_screener(rows, adapter_name):
            return run_pipeline(rows, SCREENER_CONFIG,
                                county=COUNTY_ADAPTERS[adapter_name],
                                llm=llm, brave=brave, cache=Cache())

    report = enrich_mod.enrich_county(args.county, config=config,
                                      limit=args.limit,
                                      run_screener=run_screener)
    print(json.dumps(report, indent=2))
    return 0


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

    p = sub.add_parser("replies",
                       help="Pull replies -> classify -> verify -> draft "
                            "(drafts only; nothing is sent)")
    p.set_defaults(run=_cmd_replies)

    p = sub.add_parser("review", help="The approval queue")
    p.add_argument("action", choices=("list", "approve", "reject", "snooze",
                                      "digest"),
                   nargs="?", default="list")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--send", action="store_true",
                   help="approve: actually send via Instantly (default: "
                        "print the payload and do nothing)")
    p.add_argument("--note", default=None, help="reject: reason")
    p.add_argument("--days", type=float, default=1.0, help="snooze: days")
    p.set_defaults(run=_cmd_review)

    p = sub.add_parser("pull", help="Quota-tracked PropStream recipe pull "
                                    "(browser; needs seeded session)")
    p.add_argument("--county", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--recipe", default="vacant_land")
    p.add_argument("--plan", action="store_true",
                   help="print the recipe plan + quota headroom; no browser")
    p.set_defaults(run=_cmd_pull)

    p = sub.add_parser("counties", help="Expansion scorecard (agent "
                                        "recommends; human picks)")
    p.add_argument("action", choices=("add", "score", "research"),
                   nargs="?", default="score")
    p.add_argument("county", nargs="?", default=None,
                   help="county key for add/research, e.g. brazoria_tx")
    p.add_argument("--growth-pct", type=float, default=None)
    p.add_argument("--median-lot-value", type=float, default=None)
    p.add_argument("--data-cad", action="store_true", default=None)
    p.add_argument("--data-gis", action="store_true", default=None)
    p.add_argument("--data-tax", action="store_true", default=None)
    p.add_argument("--dispo-listings", type=int, default=None)
    p.add_argument("--notes", default=None)
    p.set_defaults(run=_cmd_counties)

    p = sub.add_parser("enrich", help="Screen 'new' ledger leads -> verdicts")
    p.add_argument("--county", required=True, help="e.g. harris_tx")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--browse-tasks", default=None, metavar="OUT.md",
                   help="no adapter: write browsing research tasks to a file")
    p.set_defaults(run=_cmd_enrich)

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
