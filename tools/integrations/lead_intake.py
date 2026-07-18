"""tools/integrations/lead_intake.py — drop-folder lead loader.

Drop a PropStream export into the inbox and this loads it end to end:
  1. wait for the file to finish copying (size holds steady),
  2. copy it to the deal-desk export path so callers get priced against it,
  3. restart the deal-desk service so it re-reads the fresh file,
  4. push the leads into the Instantly campaign (dedup-safe, via the tested adapter),
  5. archive the file out of the inbox — always, success or fail, so the watcher
     never loops on a stuck file.

Wired to a systemd `.path` unit that watches the inbox; also runnable by hand:
    /root/Aria-Swarm/.venv/bin/python -m tools.integrations.lead_intake

Multi-market: the file's dominant County+State become its market key (e.g.
``harris_tx``, ``putnam_fl``). Desk data routes per market (point
``DEALDESK_EXPORT_PATH`` at a DIRECTORY and each market keeps its own file);
the Instantly push routes to ``INSTANTLY_CAMPAIGN_ID_<MARKET>`` (the default
market falls back to the original ``INSTANTLY_CAMPAIGN_ID``). A market with
no campaign configured is staged — desk data only, no outreach — never
cross-posted into another market's campaign.

Env (all optional, sane VPS defaults):
    LEADS_INBOX=/root/leads_inbox         drop files here
    LEADS_ARCHIVE=/root/leads_processed   moved here on success
    LEADS_FAILED=/root/leads_failed       moved here on failure
    DEALDESK_EXPORT_PATH=/root/land_exports   file (legacy) or directory
    DEALDESK_SERVICE=aria-dealdesk        restarted after refresh
    LEAD_INTAKE_PUSH_LIMIT=100000         cap per push (effectively uncapped)
    LEAD_INTAKE_DEFAULT_MARKET=harris_tx  market that owns INSTANTLY_CAMPAIGN_ID
    INSTANTLY_CAMPAIGN_ID_PUTNAM_FL=...   per-market campaign ids
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

INBOX = Path(os.environ.get("LEADS_INBOX", "/root/leads_inbox"))
ARCHIVE = Path(os.environ.get("LEADS_ARCHIVE", "/root/leads_processed"))
FAILED = Path(os.environ.get("LEADS_FAILED", "/root/leads_failed"))
DESK_EXPORT = Path(os.environ.get("DEALDESK_EXPORT_PATH", "/root/land_export.xlsx"))
DESK_SERVICE = os.environ.get("DEALDESK_SERVICE", "aria-dealdesk")
LOG = Path(os.environ.get("LEAD_INTAKE_LOG", "/root/.automaton/lead_intake.log"))
PUSH_LIMIT = os.environ.get("LEAD_INTAKE_PUSH_LIMIT", "100000")

_EXTS = {".xlsx", ".csv"}
_REPO = Path(__file__).resolve().parents[2]


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    line = f"[{_stamp()}] {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _stable(path: Path, checks: int = 3, interval: float = 2.0) -> bool:
    """True once the file's size holds steady across checks (finished copying)."""

    last = -1
    for _ in range(checks):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(interval)
    return last > 0


def market_key(rows: list[dict]) -> str | None:
    """'harris_tx' / 'putnam_fl' from the export's County+State columns.

    PURE. The dominant (county, state) pair wins — a stray mis-tagged row
    can't reroute the file. None when the export has no usable geography.
    """

    from collections import Counter

    pairs = Counter(
        (
            str(r.get("County") or "").strip().lower().replace(" ", ""),
            str(r.get("State") or "").strip().lower(),
        )
        for r in rows
    )
    for (county, state), _count in pairs.most_common():
        if county and state:
            return f"{county}_{state}"
    return None


def _default_campaign() -> str | None:
    # get_secret, not os.environ: the default campaign id may live in the
    # secrets DEFAULTS registry rather than .env (it did on the VPS).
    try:
        from tools.integrations.secrets import get_secret

        return get_secret("INSTANTLY_CAMPAIGN_ID")
    except Exception:  # noqa: BLE001
        return os.environ.get("INSTANTLY_CAMPAIGN_ID")


def resolve_campaign(market: str | None) -> str | None:
    """Instantly campaign for a market.

    Precedence:
      1. INSTANTLY_CAMPAIGN_ID_<MARKET> — explicit per-market override.
      2. LEAD_INTAKE_SHARED_CAMPAIGN=1 — nationwide mode: EVERY market goes
         to the default campaign. Requires geography-neutral sequence copy
         (use the {{propertyCity}}/{{county}}/{{state}} variables).
      3. The default market (LEAD_INTAKE_DEFAULT_MARKET, harris_tx) falls
         back to the original INSTANTLY_CAMPAIGN_ID.
      4. None -> the intake stages the file (desk data only) rather than
         cross-post a market into a campaign nobody opted it into.
    """

    if market:
        specific = os.environ.get(f"INSTANTLY_CAMPAIGN_ID_{market.upper()}")
        if specific:
            return specific
    if os.environ.get("LEAD_INTAKE_SHARED_CAMPAIGN", "").strip() in ("1", "true", "yes"):
        return _default_campaign()
    default_market = os.environ.get("LEAD_INTAKE_DEFAULT_MARKET", "harris_tx")
    if market == default_market or market is None:
        return _default_campaign()
    return None


def _existing_market(export: Path) -> str | None:
    try:
        from tools.integrations.leadfile import parse_propstream

        return market_key(parse_propstream(export))
    except Exception:  # noqa: BLE001 — unreadable/missing => no constraint
        return None


def _refresh_desk(path: Path, market: str | None) -> None:
    """Route the file into the desk's export set and restart the desk.

    Directory mode (DEALDESK_EXPORT_PATH is a dir): each market gets its own
    file, so a Putnam drop can never clobber Harris pricing data. Legacy
    single-file mode: overwrite only when the incoming market matches what
    the desk already serves — mismatches are refused loudly.
    """

    if DESK_EXPORT.is_dir():
        stem = market or "default"
        for old in DESK_EXPORT.glob(f"{stem}.*"):  # one file per market
            old.unlink()
        target = DESK_EXPORT / f"{stem}{path.suffix.lower()}"
    else:
        current = _existing_market(DESK_EXPORT)
        if current and market and current != market:
            _log(
                f"deal-desk NOT updated: desk serves '{current}' but file is "
                f"'{market}'. Point DEALDESK_EXPORT_PATH at a directory to "
                f"serve multiple markets."
            )
            return
        target = DESK_EXPORT

    shutil.copy2(path, target)
    _log(f"deal-desk export updated -> {target}")
    r = subprocess.run(["systemctl", "restart", DESK_SERVICE],
                       capture_output=True, text=True)
    _log(f"deal-desk restart rc={r.returncode} {r.stderr.strip()}")


def _process(path: Path) -> bool:
    """Feed the deal desk + push to Instantly. Returns True on a clean run."""

    _log(f"intake start: {path.name} ({path.stat().st_size} bytes)")

    try:
        from tools.integrations.leadfile import parse_propstream

        market = market_key(parse_propstream(path))
    except Exception as exc:  # noqa: BLE001
        _log(f"unparseable export: {exc}")
        return False
    _log(f"market: {market or 'UNKNOWN'}")

    # 1. Deal desk refresh — failure is logged but doesn't fail the intake.
    try:
        _refresh_desk(path, market)
    except Exception as exc:  # noqa: BLE001
        _log(f"deal-desk refresh FAILED (continuing to push): {exc}")

    # 2. Instantly push, routed to the market's campaign.
    campaign = resolve_campaign(market)
    if not campaign:
        _log(
            f"no campaign for market '{market}' — desk data staged, no leads "
            f"pushed. Set INSTANTLY_CAMPAIGN_ID_{(market or 'X').upper()} to "
            f"enable outreach for this market."
        )
        return True  # staged as intended, not a failure

    try:
        r = subprocess.run(
            [sys.executable, "-m", "tools.integrations.instantly", str(path),
             "--push", "--limit", PUSH_LIMIT, "--campaign-id", campaign],
            cwd=str(_REPO), capture_output=True, text=True, timeout=1800,
        )
        if r.stdout:
            _log("instantly push:\n" + r.stdout.strip())
        if r.returncode != 0:
            _log(f"instantly push FAILED rc={r.returncode} {r.stderr.strip()}")
            return False
    except Exception as exc:  # noqa: BLE001
        _log(f"instantly push FAILED: {exc}")
        return False
    return True


def run_once() -> int:
    """Process every stable file in the inbox; always clear it out afterward.

    Serialized via an exclusive lock: the systemd watcher and a manual run can
    otherwise both read the same inbox file before either archives it and
    double-push it (harmless downstream — skip_if_in_campaign — but noisy).
    """

    import fcntl

    LOG.parent.mkdir(parents=True, exist_ok=True)
    lock = (LOG.parent / "lead_intake.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _log("another intake run holds the lock — skipping (it has the inbox)")
        return 0

    INBOX.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in INBOX.iterdir()
                   if p.is_file() and p.suffix.lower() in _EXTS)
    if not files:
        return 0

    for path in files:
        if not _stable(path):
            _log(f"skip (still copying / empty): {path.name}")
            continue
        ok = _process(path)
        dest_dir = ARCHIVE if ok else FAILED
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_stamp = _stamp().replace(":", "").replace(" ", "_")
        dest = dest_dir / f"{safe_stamp}-{path.name}"
        try:
            shutil.move(str(path), str(dest))
            _log(f"{'archived' if ok else 'moved to FAILED'}: {dest}")
        except Exception as exc:  # noqa: BLE001
            _log(f"could not move {path.name} out of inbox: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_once())
