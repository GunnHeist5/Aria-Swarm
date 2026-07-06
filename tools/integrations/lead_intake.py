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

Env (all optional, sane VPS defaults):
    LEADS_INBOX=/root/leads_inbox         drop files here
    LEADS_ARCHIVE=/root/leads_processed   moved here on success
    LEADS_FAILED=/root/leads_failed       moved here on failure
    DEALDESK_EXPORT_PATH=/root/land_export.xlsx   the desk reads this
    DEALDESK_SERVICE=aria-dealdesk        restarted after refresh
    LEAD_INTAKE_PUSH_LIMIT=100000         cap per push (effectively uncapped)
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

    load_dotenv()
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


def _process(path: Path) -> bool:
    """Feed the deal desk + push to Instantly. Returns True on a clean push."""

    _log(f"intake start: {path.name} ({path.stat().st_size} bytes)")

    # 1. Deal desk: copy the file to the export path + restart so it re-reads.
    #    A desk-refresh failure is logged but does not fail the whole intake —
    #    the Instantly load is the primary job.
    try:
        shutil.copy2(path, DESK_EXPORT)
        _log(f"deal-desk export updated -> {DESK_EXPORT}")
        r = subprocess.run(["systemctl", "restart", DESK_SERVICE],
                           capture_output=True, text=True)
        _log(f"deal-desk restart rc={r.returncode} {r.stderr.strip()}")
    except Exception as exc:  # noqa: BLE001
        _log(f"deal-desk refresh FAILED (continuing to push): {exc}")

    # 2. Instantly push via the tested adapter (suppression + dedup built in).
    try:
        r = subprocess.run(
            [sys.executable, "-m", "tools.integrations.instantly", str(path),
             "--push", "--limit", PUSH_LIMIT],
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
    """Process every stable file in the inbox; always clear it out afterward."""

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
