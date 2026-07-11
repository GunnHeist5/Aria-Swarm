"""tools/dealflow/leads_in.py — leads walk in through Telegram.

Two front doors, both feeding the SAME intake pipeline (dedupe →
suppression → market routing → deal-desk refresh → Instantly push) that
the drop folder uses — this module only moves bytes into the inbox and
reports back:

  * ATTACH A FILE: send the bot a PropStream .xlsx/.csv as a Telegram
    document — it lands in the inbox and processes immediately, and the
    bot replies with the intake report (market, kept/suppressed, pushed).
  * SAY A LEAD: the deal-desk agent's ``add_lead`` tool writes a one-row
    PropStream-shaped CSV into the inbox from whatever the operator typed
    ("add lead: 123 Main St Houston TX 77002, owner Jane Doe,
    jane@x.com") and the same pipeline handles it.

Only the operator's chat id is honored — the router enforces that before
anything here runs.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.request
from pathlib import Path

_API = "https://api.telegram.org/bot{token}/{method}"
_FILE = "https://api.telegram.org/file/bot{token}/{path}"
_EXTS = (".xlsx", ".csv")

# PropStream-compatible header set: everything downstream (leadfile parser,
# market routing, deal-desk lookup, Instantly mapping) reads these names.
LEAD_COLUMNS = [
    "Address", "City", "State", "Zip", "County", "APN",
    "Owner 1 First Name", "Owner 1 Last Name",
    "Email 1", "Phone 1", "Lot Size Sqft", "Est. Value",
]


def _fetch(url: str, *, timeout: float = 60.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _inbox() -> Path:
    from tools.integrations.lead_intake import INBOX

    INBOX.mkdir(parents=True, exist_ok=True)
    return INBOX


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "leads.csv")[:120]


def handle_document(message: dict, *, token: str, chat_id: str,
                    fetch=_fetch, notifier=None, intake=None) -> dict:
    """Download a Telegram document into the leads inbox and process it."""

    if notifier is None:
        from . import notify as notifier  # noqa: PLC0415

    def say(text: str) -> None:
        notifier.send_text(text[:3500], token=token, chat_id=chat_id,
                           parse_mode=None)

    doc = message.get("document") or {}
    name = doc.get("file_name") or ""
    if not name.lower().endswith(_EXTS):
        say(f"I can take lead files as .xlsx or .csv — '{name}' isn't one. "
            "Export from PropStream (or match its columns) and resend.")
        return {"ok": False, "reason": "not_a_lead_file"}

    try:
        meta = json.loads(fetch(
            _API.format(token=token, method="getFile")
            + f"?file_id={doc.get('file_id')}"))
        tg_path = (meta.get("result") or {}).get("file_path")
        blob = fetch(_FILE.format(token=token, path=tg_path))
    except Exception as exc:  # noqa: BLE001
        say(f"Couldn't download that file from Telegram: {exc}")
        return {"ok": False, "reason": f"download: {exc}"}

    target = _inbox() / _safe_name(name)
    target.write_bytes(blob)
    say(f"Got {name} ({len(blob):,} bytes) — running intake: dedupe, "
        "suppression, market routing, deal-desk refresh, Instantly push…")

    report = _run_intake(target.name, intake=intake)
    say(report)
    return {"ok": True, "file": str(target)}


def add_lead_row(fields: dict) -> Path:
    """Write a one-row PropStream-shaped CSV into the inbox. Returns the path."""

    row = {c: str(fields.get(c, "") or "") for c in LEAD_COLUMNS}
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEAD_COLUMNS)
    writer.writeheader()
    writer.writerow(row)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = _inbox() / f"manual_lead_{stamp}.csv"
    target.write_text(buf.getvalue(), encoding="utf-8")
    return target


def _run_intake(filename: str, *, intake=None) -> str:
    """Run the intake now and pull this file's lines out of the shared log.

    The systemd inbox watcher may race us and win — that's fine (everything
    is idempotent); the log lines exist either way, so the report is honest
    about what happened regardless of who did the work.
    """

    try:
        if intake is None:
            from tools.integrations import lead_intake as intake  # noqa: PLC0415
        intake.run_once()
        log_path = Path(intake.LOG)
        lines = log_path.read_text(encoding="utf-8").splitlines()[-80:]
    except Exception as exc:  # noqa: BLE001
        return (f"File is in the inbox but I couldn't run/inspect the intake "
                f"({exc}) — the folder watcher will pick it up; check the "
                f"intake log for results.")

    stem = filename.rsplit(".", 1)[0]
    started = [i for i, ln in enumerate(lines) if stem in ln and "intake start" in ln]
    if not started:
        return ("File staged in the inbox — the watcher hasn't logged it yet; "
                "I'll have results in the intake log within a minute.")
    relevant = lines[started[-1]:]
    return "Intake report:\n" + "\n".join(relevant[:30])
