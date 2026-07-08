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
  python -m tools.integrations.instantly export.xlsx --drift-check          # report only
  python -m tools.integrations.instantly export.xlsx --drift-check --push   # remove drifted

Drift check: MLS status is a snapshot at export time. A lead loaded while
unlisted can list with an agent afterwards — the drift check compares the
freshest export against who's currently in the campaign and removes any lead
whose status flipped to listed/pending (or litigator), so the "no agents, no
buyers" rule stays true continuously, not just at load time.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

# Auto-load ./.env so the CLI works standalone on the VPS (harmless if absent).
try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from . import suppression
from .leadfile import parse_propstream, suppress, suppressed_emails
from .secrets import SecretError, get_secret

API_URL = "https://api.instantly.ai/api/v2/leads"
LIST_URL = "https://api.instantly.ai/api/v2/leads/list"
DELETE_URL = "https://api.instantly.ai/api/v2/leads/{lead_id}"

DEFAULT_LIMIT = 100
REQUEST_SPACING_S = 0.1   # modest client-side rate limiting
MAX_AUTH_FAILURES = 3     # consecutive 401/403 -> abort (bad key, fail closed)

# Instantly's API is behind Cloudflare, which bans the default Python-urllib
# User-Agent (HTTP 403 "error code: 1010"). Send a real browser UA so requests
# get through to Instantly's own auth layer.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _mask_email(email: str) -> str:
    """``jane.doe@host.com`` -> ``j***@host.com`` (reports stay PII-free)."""

    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _http_request(method: str, url: str, payload: dict | None, api_key: str) -> tuple[int, str]:
    """JSON request with Bearer auth. Returns (status_code, body_text)."""

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _http_post(url: str, payload: dict, api_key: str) -> tuple[int, str]:
    """POST JSON with Bearer auth. Returns (status_code, body_text)."""

    return _http_request("POST", url, payload, api_key)


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

    # Never (re-)load an opted-out address — CAN-SPAM, no exceptions. A fresh
    # export can contain someone who replied STOP to an earlier campaign.
    suppressed = suppression.load()
    kept = [l for l in leads if l.get("email", "").lower() not in suppressed]

    batch = kept[:limit]
    report = {
        "campaign_id": campaign_id,
        "dry_run": dry_run,
        "attempted": len(batch),
        "held_by_limit": max(0, len(kept) - limit),
        "suppressed_opt_outs": len(leads) - len(kept),
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


def remove_lead_by_email(
    email: str, *, api_key: str, campaign_id: str, http_request=_http_request,
) -> bool | None:
    """Delete one lead from the campaign (opt-out compliance).

    Returns True on removal, False if the email isn't in the campaign, and
    raises nothing above the caller's try (auth failures raise RuntimeError
    from ``fetch_campaign_leads`` — the caller decides how loud to be).
    """

    leads = fetch_campaign_leads(api_key=api_key, campaign_id=campaign_id,
                                 http_request=http_request)
    lead_id = leads.get((email or "").lower())
    if not lead_id:
        return False
    status, _ = http_request("DELETE", DELETE_URL.format(lead_id=lead_id), None, api_key)
    return 200 <= status < 300


# ---------------------------------------------------------------------------
# Drift check — keep "no agents, no buyers" true after load time
# ---------------------------------------------------------------------------


def fetch_campaign_leads(
    *, api_key: str, campaign_id: str, http_request=_http_request, page_size: int = 100,
) -> dict[str, str]:
    """Return ``{email: lead_id}`` for every lead currently in the campaign.

    Paginates the Instantly v2 list endpoint. Raises ``RuntimeError`` on an
    auth failure (fail closed — a drift check that can't see the campaign must
    not silently report "no drift").
    """

    leads: dict[str, str] = {}
    starting_after = None
    while True:
        payload = {"campaign": campaign_id, "limit": page_size}
        if starting_after:
            payload["starting_after"] = starting_after
        status, body = http_request("POST", LIST_URL, payload, api_key)
        if status in (401, 403):
            raise RuntimeError(f"instantly auth failed listing campaign leads (HTTP {status})")
        if not 200 <= status < 300:
            raise RuntimeError(f"instantly list failed (HTTP {status})")
        data = json.loads(body or "{}")
        items = data.get("items") or []
        for item in items:
            email = (item.get("email") or "").lower()
            if email:
                leads[email] = item.get("id", "")
        starting_after = data.get("next_starting_after")
        if not starting_after or not items:
            break
    return leads


def drift_check(
    rows: list[dict],
    *,
    api_key: str,
    campaign_id: str,
    dry_run: bool = True,
    http_request=_http_request,
    sleep=time.sleep,
) -> dict:
    """Remove campaign leads whose fresh export status says "has agent/buyer".

    Compares the freshest export's suppression set (MLS listed/pending/
    contingent or litigator-flagged) against who is actually in the campaign,
    and removes the overlap. Multi-parcel owners are handled precisely: an
    email drifts only when the owner has NO still-unlisted parcel left in the
    export (one lot going under contract doesn't kill outreach about their
    other, unlisted lot). Dry-run reports what WOULD be removed.
    """

    flagged = suppressed_emails(rows)
    still_sendable = {lead["email"] for lead in suppress(rows)[0]}
    in_campaign = fetch_campaign_leads(
        api_key=api_key, campaign_id=campaign_id, http_request=http_request
    )
    drifted = sorted(set(in_campaign) & (flagged - still_sendable))

    report = {
        "campaign_id": campaign_id,
        "dry_run": dry_run,
        "in_campaign": len(in_campaign),
        "flagged_in_export": len(flagged),
        "drifted": len(drifted),
        "removed": 0,
        "errors": 0,
        "drifted_masked": [_mask_email(e) for e in drifted[:10]],
    }
    if dry_run:
        return report

    for email in drifted:
        lead_id = in_campaign[email]
        status, _ = http_request(
            "DELETE", DELETE_URL.format(lead_id=lead_id), None, api_key
        )
        if 200 <= status < 300:
            report["removed"] += 1
        else:
            report["errors"] += 1
            print(f"[instantly] drift remove {_mask_email(email)}: HTTP {status}")
        sleep(REQUEST_SPACING_S)
    return report


def _print_drift_report(report: dict) -> None:
    print("\n==== Instantly drift check ====")
    print(f"campaign             : {report['campaign_id']}")
    print(f"leads in campaign    : {report['in_campaign']}")
    print(f"flagged in export    : {report['flagged_in_export']} (listed/pending/litigator)")
    print(f"drifted (overlap)    : {report['drifted']}")
    if report["drifted_masked"]:
        print(f"  e.g.               : {', '.join(report['drifted_masked'])}")
    if report["dry_run"]:
        print("mode                 : DRY-RUN — use --push to remove these from the campaign")
    else:
        print(f"removed              : {report['removed']}")
        print(f"errors               : {report['errors']}")
    print("===============================\n")


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
    parser.add_argument("--drift-check", action="store_true",
                        help="Compare the export against the campaign and remove "
                             "leads that are now listed/pending/litigator "
                             "(report-only without --push).")
    args = parser.parse_args(argv)

    rows = parse_propstream(args.export)
    kept, suppression = suppress(rows)

    campaign_id = args.campaign_id or get_secret("INSTANTLY_CAMPAIGN_ID")
    # The drift check must READ the campaign even in dry-run, so it always
    # needs the key; a plain load only needs it when actually pushing.
    if args.push or args.drift_check:
        try:
            api_key = get_secret("INSTANTLY_API_KEY", required=True)
        except SecretError as exc:
            print(f"[instantly] {exc}")
            return 1
    else:
        api_key = ""  # never needed for a load dry-run

    if args.drift_check:
        try:
            report = drift_check(
                rows, api_key=api_key, campaign_id=campaign_id,
                dry_run=not args.push,
            )
        except RuntimeError as exc:
            print(f"[instantly] {exc}")
            return 1
        _print_drift_report(report)
        return 1 if report["errors"] else 0

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
