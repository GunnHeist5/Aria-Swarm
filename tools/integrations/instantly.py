"""tools/integrations/instantly.py — load leads into the Instantly campaign.

The front door of the outreach machine: maps a PropStream export through the
suppression rules (``leadfile.py``) and pushes the survivors into an Instantly
campaign via the v2 API. Instantly then sends the sequence across the attached
warmed mailboxes on its own scheduler — this adapter loads the magazine, it
never fires it (sends are enabled/paused in the Instantly UI).

Safety posture:
  * **Dry-run is the default.** ``--push`` is required to touch the network,
    and ``--limit`` caps every run (default 100) so first live tests are small.
  * The API key comes from the environment only (`secrets.get_secret`), and
    repeated auth failures abort the run (fail closed, don't hammer).
  * ``skip_if_in_campaign`` makes re-runs idempotent server-side.
  * Reports mask lead emails — no PII in logs.

CLI (run on the VPS, where INSTANTLY_API_KEY lives in .env):
  python -m tools.integrations.instantly export.xlsx              # dry-run report
  python -m tools.integrations.instantly export.xlsx --push --limit 5
  python -m tools.integrations.instantly export.xlsx --push --limit 2000
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

from .leadfile import parse_propstream, suppress
from .secrets import SecretError, get_secret

API_URL = "https://api.instantly.ai/api/v2/leads"

DEFAULT_LIMIT = 100
REQUEST_SPACING_S = 0.1   # modest client-side rate limiting
MAX_AUTH_FAILURES = 3     # consecutive 401/403 -> abort (bad key, fail closed)


def _mask_email(email: str) -> str:
    """``jane.doe@host.com`` -> ``j***@host.com`` (reports stay PII-free)."""

    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _http_post(url: str, payload: dict, api_key: str) -> tuple[int, str]:
    """POST JSON with Bearer auth. Returns (status_code, body_text)."""

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def push_leads(
    leads: list[dict],
    *,
    api_key: str,
    campaign_id: str,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = True,
    http_post=_http_post,
    sleep=time.sleep,
) -> dict:
    """Push up to ``limit`` leads into the campaign. Returns a summary report.

    ``http_post``/``sleep`` are injectable for offline tests. In dry-run mode
    no network call is made — the report shows what WOULD be pushed.
    """

    batch = leads[:limit]
    report = {
        "campaign_id": campaign_id,
        "dry_run": dry_run,
        "attempted": len(batch),
        "held_by_limit": max(0, len(leads) - limit),
        "pushed": 0,
        "skipped_existing": 0,
        "errors": 0,
        "aborted": None,
    }

    if dry_run:
        return report

    auth_failures = 0
    for lead in batch:
        payload = {
            "campaign": campaign_id,
            "email": lead["email"],
            "first_name": lead["first_name"],
            "last_name": lead["last_name"],
            "custom_variables": lead["custom_variables"],
            "skip_if_in_campaign": True,  # server-side idempotency on re-runs
        }
        status, body = http_post(API_URL, payload, api_key)

        if status in (401, 403):
            auth_failures += 1
            report["errors"] += 1
            if auth_failures >= MAX_AUTH_FAILURES:
                report["aborted"] = f"auth_failed_x{auth_failures}"
                break
            continue
        auth_failures = 0

        if 200 <= status < 300:
            report["pushed"] += 1
        elif status == 409 or "already" in body.lower():
            report["skipped_existing"] += 1
        else:
            report["errors"] += 1
            print(f"[instantly] {_mask_email(lead['email'])}: HTTP {status}")
        sleep(REQUEST_SPACING_S)

    return report


def _print_report(kept: list[dict], suppression: dict, push_report: dict) -> None:
    print("\n==== Instantly load report ====")
    print(f"export rows        : {suppression['total_rows']}")
    print(f"  - no email       : {suppression['no_email']}")
    print(f"  - litigator      : {suppression['litigator']}")
    print(f"  - MLS listed     : {suppression['mls_listed']}")
    print(f"  - duplicate email: {suppression['duplicate_email']}")
    print(f"sendable leads     : {suppression['kept']}")
    print(f"campaign           : {push_report['campaign_id']}")
    mode = "DRY-RUN (no network — use --push to load)" if push_report["dry_run"] else "PUSH"
    print(f"mode               : {mode}")
    print(f"in this batch      : {push_report['attempted']}"
          f" (held by --limit: {push_report['held_by_limit']})")
    if not push_report["dry_run"]:
        print(f"pushed             : {push_report['pushed']}")
        print(f"already in campaign: {push_report['skipped_existing']}")
        print(f"errors             : {push_report['errors']}")
        if push_report["aborted"]:
            print(f"ABORTED            : {push_report['aborted']}")
    if push_report["dry_run"] and kept:
        print("sample (masked)    :")
        for lead in kept[: min(5, len(kept))]:
            acres = lead["custom_variables"].get("lotAcres", "?")
            county = lead["custom_variables"].get("county", "?")
            print(f"  {_mask_email(lead['email']):<28} {acres} ac, {county} County")
    print("===============================\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.integrations.instantly",
        description="Load a PropStream export into the Instantly campaign.",
    )
    parser.add_argument("export", help="Path to the PropStream .xlsx/.csv export.")
    parser.add_argument("--push", action="store_true",
                        help="Actually call the Instantly API (default: dry-run).")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max leads this run (default {DEFAULT_LIMIT}).")
    parser.add_argument("--campaign-id", default=None,
                        help="Override INSTANTLY_CAMPAIGN_ID.")
    args = parser.parse_args(argv)

    rows = parse_propstream(args.export)
    kept, suppression = suppress(rows)

    campaign_id = args.campaign_id or get_secret("INSTANTLY_CAMPAIGN_ID")
    if args.push:
        try:
            api_key = get_secret("INSTANTLY_API_KEY", required=True)
        except SecretError as exc:
            print(f"[instantly] {exc}")
            return 1
    else:
        api_key = ""  # never needed for a dry-run

    push_report = push_leads(
        kept,
        api_key=api_key,
        campaign_id=campaign_id,
        limit=args.limit,
        dry_run=not args.push,
    )
    _print_report(kept, suppression, push_report)

    if args.push and push_report["pushed"] > 0:
        _notify_swarm(push_report)

    if push_report.get("aborted"):
        return 1
    return 0


def _notify_swarm(push_report: dict) -> None:
    """Best-effort: fire the swarm's ``new_leads_synced`` trigger (same box).

    The swarm's pipeline bookkeeping wants to know the front door was loaded;
    a failure here never fails the push itself.
    """

    import subprocess
    import sys
    from pathlib import Path

    main_py = Path(__file__).resolve().parents[2] / "main.py"
    payload = {"count": push_report["pushed"], "source": "instantly_load"}
    try:
        subprocess.run(
            [sys.executable, str(main_py), "--event", "new_leads_synced",
             "--payload", json.dumps(payload)],
            timeout=120, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"[instantly] swarm notify skipped (non-fatal): {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
