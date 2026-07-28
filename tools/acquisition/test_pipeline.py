"""Offline tests for the end-to-end county pipeline — no browser, no network.

The stubbed flow mimics the live behaviour that shaped the design: the
export taken right after a skip-trace order carries no contacts, and only a
later export does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import pipeline


class _FakeDriver:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _driver_factory(config):
    return _FakeDriver()


def _write_export(path: Path, rows: int, with_email: int) -> str:
    """Minimal .xlsx the repo's parse_propstream can read."""

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["APN", "County", "State", "Address", "City", "Zip",
               "Email 1", "Lot Size Sqft"])
    for i in range(rows):
        ws.append([f"apn-{i}", "Galveston", "TX", f"{i} Land Rd", "Texas City",
                   "77590", (f"o{i}@x.com" if i < with_email else ""),
                   "21780"])
    wb.save(path)
    return str(path)


class _Flow:
    """Stub of tools.browser.propstream with a trace that lands late."""

    def __init__(self, tmp_path: Path, fill_after: int = 2, rows: int = 10):
        self.tmp = tmp_path
        self.fill_after = fill_after      # export attempt that finally has emails
        self.rows = rows
        self.exports = 0
        self.orders = 0

    def run_pull_v2(self, driver, config, county, state, **kw):
        return {"county": county, "state": state, "count": self.rows,
                "list_name": f"aria-{county}-{state}-20260728"}

    def run_skiptrace_list(self, driver, config, list_name, **kw):
        self.orders += 1
        return {"started": True, "order_summary": ["$ 0.00"]}

    def run_export_list(self, driver, config, list_name, **kw):
        self.exports += 1
        emails = self.rows if self.exports >= self.fill_after else 0
        path = _write_export(self.tmp / f"export{self.exports}.xlsx",
                             self.rows, emails)
        return {"downloaded": path}


def _env_db(tmp_path: Path) -> None:
    os.environ["ACQUISITION_DB"] = str(tmp_path / "acq.db")
    os.environ["SUPPRESSION_STORE"] = str(tmp_path / "sup.json")


def test_pipeline_waits_for_the_trace_then_ingests(tmp_path):
    _env_db(tmp_path)
    flow = _Flow(tmp_path, fill_after=3)
    slept = []
    report = pipeline.run_county(
        "galveston", "tx", config=object(), username="u", password="p",
        driver_factory=_driver_factory, flow=flow,
        sleep=slept.append, poll_seconds=7, log=lambda *_: None)

    assert flow.exports == 3, "must re-export until the contacts land"
    assert slept == [7, 7], "waits between export attempts"
    assert report["email_fill"] == 1.0
    assert report["ingested"]["inserted"] == 10
    assert flow.orders == 1, "the skip-trace order must never be re-placed"


def test_pipeline_gives_up_and_still_ingests(tmp_path):
    _env_db(tmp_path)
    flow = _Flow(tmp_path, fill_after=99)      # trace never lands
    report = pipeline.run_county(
        "galveston", "tx", config=object(), username="u", password="p",
        driver_factory=_driver_factory, flow=flow, sleep=lambda _: None,
        poll_attempts=3, log=lambda *_: None)

    assert flow.exports == 3
    assert report["email_fill"] == 0.0
    assert report["ingested"]["inserted"] == 10, \
        "contact-less leads are still leads — ingest them"
    assert flow.orders == 1


def test_pipeline_skips_the_trace_when_told(tmp_path):
    _env_db(tmp_path)
    flow = _Flow(tmp_path, fill_after=99)
    report = pipeline.run_county(
        "galveston", "tx", config=object(), username="u", password="p",
        skiptrace=False, driver_factory=_driver_factory, flow=flow,
        sleep=lambda _: None, log=lambda *_: None)

    assert flow.orders == 0
    assert flow.exports == 1, "no trace -> no reason to poll"
    assert report["ingested"]["inserted"] == 10


def test_pipeline_never_enrolls(tmp_path):
    """The standing rule: outreach is a human push, never a pipeline step."""

    _env_db(tmp_path)
    from . import enroll as enroll_mod

    flow = _Flow(tmp_path, fill_after=1)
    pipeline.run_county("galveston", "tx", config=object(), username="u",
                        password="p", driver_factory=_driver_factory,
                        flow=flow, sleep=lambda _: None, log=lambda *_: None)
    rows = enroll_mod.eligible_leads(include_unscreened=True)
    assert len(rows) == 10, "leads are ready..."
    from . import ledger

    conn = ledger.connect()
    statuses = {r[0] for r in conn.execute("SELECT status FROM leads")}
    conn.close()
    assert statuses == {"new"}, "...but nothing may be marked enrolled"


def test_email_fill_rate_counts_either_email_column(tmp_path):
    path = Path(_write_export(tmp_path / "e.xlsx", 4, 1))
    rows, fill = pipeline.email_fill_rate(path)
    assert rows == 4 and abs(fill - 0.25) < 1e-9
    assert json.dumps({"ok": True})      # sanity: module imports cleanly


def test_pipeline_can_work_an_existing_list_without_pulling(tmp_path):
    """--list-name enriches a list built before the pipeline existed:
    trace it, wait for the contacts, export, backfill the ledger."""

    _env_db(tmp_path)

    class _NoPull(_Flow):
        def run_pull_v2(self, *a, **kw):        # must never be called
            raise AssertionError("an existing list must not trigger a pull")

    flow = _NoPull(tmp_path, fill_after=2)
    report = pipeline.run_county(
        "montgomery", "tx", config=object(), username="u", password="p",
        list_name="Vacant Land Montgomer TX", driver_factory=_driver_factory,
        flow=flow, sleep=lambda _: None, log=lambda *_: None)

    assert report["list_name"] == "Vacant Land Montgomer TX"
    assert flow.orders == 1 and flow.exports == 2
    assert report["ingested"]["inserted"] == 10
