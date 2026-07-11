"""Offline tests for Telegram lead ingestion (file drop + dictated lead)."""

from __future__ import annotations

import json
from pathlib import Path

from tools.integrations import lead_intake

from . import leads_in


class StubNotify:
    def __init__(self):
        self.sent: list[str] = []

    def send_text(self, text, *, token, chat_id, parse_mode=None):
        self.sent.append(text)
        return 200, "{}"


class StubIntake:
    """Stands in for lead_intake: records the run, writes a plausible log."""

    def __init__(self, log_path: Path, filename: str):
        self.LOG = log_path
        self.filename = filename
        self.runs = 0

    def run_once(self):
        self.runs += 1
        self.LOG.write_text(
            f"[stamp] intake start: {self.filename} (123 bytes)\n"
            f"[stamp] market: harris_tx\n"
            f"[stamp] deal-desk export updated -> /x/harris_tx.csv\n"
            f"[stamp] archived: /y/{self.filename}\n"
        )
        return 0


def _patched_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    lead_intake.INBOX = inbox  # leads_in._inbox() reads the live attribute
    return inbox


def test_handle_document_downloads_and_reports(tmp_path):
    inbox = _patched_inbox(tmp_path)
    notifier = StubNotify()
    intake = StubIntake(tmp_path / "intake.log", "My_Export.csv")

    def fetch(url, **kw):
        if "getFile" in url:
            return json.dumps({"result": {"file_path": "docs/file_7.csv"}}).encode()
        assert "docs/file_7.csv" in url
        return b"Address,City\n1 Main St,Houston\n"

    message = {"document": {"file_name": "My Export.csv", "file_id": "F7"}}
    out = leads_in.handle_document(message, token="t", chat_id="c",
                                   fetch=fetch, notifier=notifier, intake=intake)
    assert out["ok"]
    assert (inbox / "My_Export.csv").read_bytes().startswith(b"Address")
    assert intake.runs == 1
    assert any("Got My Export.csv" in s for s in notifier.sent)
    assert any("market: harris_tx" in s for s in notifier.sent)


def test_handle_document_rejects_non_lead_files(tmp_path):
    _patched_inbox(tmp_path)
    notifier = StubNotify()
    message = {"document": {"file_name": "photo.pdf", "file_id": "F8"}}
    out = leads_in.handle_document(message, token="t", chat_id="c",
                                   fetch=lambda *a, **k: b"", notifier=notifier)
    assert not out["ok"]
    assert any(".xlsx or .csv" in s for s in notifier.sent)


def test_add_lead_row_roundtrips_through_the_parser(tmp_path):
    inbox = _patched_inbox(tmp_path)
    path = leads_in.add_lead_row({
        "Address": "123 Main St", "City": "Palatka", "State": "FL",
        "Zip": "32177", "County": "Putnam", "Email 1": "jane@x.com",
        "Phone 1": "555-0100", "Lot Size Sqft": 8000,
    })
    assert path.parent == inbox

    from tools.integrations.lead_intake import market_key
    from tools.integrations.leadfile import parse_propstream

    rows = parse_propstream(path)
    assert rows[0]["Address"] == "123 Main St"
    assert rows[0]["Email 1"] == "jane@x.com"
    assert market_key(rows) == "putnam_fl"


def test_agent_add_lead_tool(tmp_path):
    from .agent import Toolbox

    _patched_inbox(tmp_path)
    real_run = leads_in._run_intake
    leads_in._run_intake = lambda name, **kw: f"Intake report: processed {name}"
    try:
        box = Toolbox(token="t", chat_id="c", export_path=None)
        result = box.run("add_lead", {"address": "9 Oak Ln", "city": "Houston",
                                      "state": "tx", "zip": "77002"})
        assert result["ok"] and "manual_lead_" in result["staged_as"]
        assert "no email" in result["note"]
        assert "processed" in result["intake"]
    finally:
        leads_in._run_intake = real_run


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    module = sys.modules[__name__]
    for name in sorted(dir(module)):
        if name.startswith("test_"):
            fn = getattr(module, name)
            try:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
                print(f"ok {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
