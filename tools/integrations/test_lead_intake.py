"""Offline tests for multi-market lead intake routing."""

from __future__ import annotations

import csv
import os
from pathlib import Path

from .lead_intake import market_key, resolve_campaign


def _rows(county: str, state: str, n: int = 3) -> list[dict]:
    return [{"County": county, "State": state, "Address": f"{i} Main St"}
            for i in range(n)]


def test_market_key_dominant_pair_wins():
    rows = _rows("Harris", "TX", 5) + _rows("Putnam", "FL", 1)
    assert market_key(rows) == "harris_tx"
    assert market_key(_rows("Putnam", "FL")) == "putnam_fl"
    # spaces collapse ("Fort Bend" -> fortbend_tx)
    assert market_key(_rows("Fort Bend", "TX")) == "fortbend_tx"


def test_market_key_skips_blank_geography():
    rows = [{"County": "", "State": ""}] * 5 + _rows("Putnam", "FL", 2)
    assert market_key(rows) == "putnam_fl"
    assert market_key([{"County": "", "State": ""}]) is None


def test_resolve_campaign_routing():
    env = {
        "INSTANTLY_CAMPAIGN_ID": "harris-default-id",
        "INSTANTLY_CAMPAIGN_ID_PUTNAM_FL": "putnam-id",
    }
    saved = {k: os.environ.get(k) for k in
             list(env) + ["LEAD_INTAKE_DEFAULT_MARKET"]}
    os.environ.update(env)
    os.environ.pop("LEAD_INTAKE_DEFAULT_MARKET", None)
    try:
        # default market falls back to the original env var
        assert resolve_campaign("harris_tx") == "harris-default-id"
        # explicit per-market var wins
        assert resolve_campaign("putnam_fl") == "putnam-id"
        # unconfigured market -> None (stage, never cross-post)
        assert resolve_campaign("duval_fl") is None
        # per-market var overrides even for the default market
        os.environ["INSTANTLY_CAMPAIGN_ID_HARRIS_TX"] = "harris-specific"
        assert resolve_campaign("harris_tx") == "harris-specific"
    finally:
        os.environ.pop("INSTANTLY_CAMPAIGN_ID_HARRIS_TX", None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_export(path: Path, county: str, state: str) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["Address", "City", "State", "Zip",
                                           "County", "APN", "Email 1"])
        w.writeheader()
        w.writerow({"Address": "10 Oak St", "City": "X", "State": state,
                    "Zip": "77028", "County": county,
                    "APN": f"{county[:2].upper()}-001",
                    "Email 1": f"owner@{county.lower()}.com"})


def test_dealdesk_lookup_merges_directory(tmp_path):
    from tools.dealdesk.lookup import FileLookup

    _write_export(tmp_path / "harris_tx.csv", "Harris", "TX")
    _write_export(tmp_path / "putnam_fl.csv", "Putnam", "FL")

    lk = FileLookup(tmp_path)
    assert len(lk.sources) == 2
    assert lk.find(apn="HA-001").county == "Harris"
    assert lk.find(apn="PU-001").county == "Putnam"
    assert lk.find_by_email("owner@putnam.com").state == "FL"

    # single-file mode still works (legacy)
    single = FileLookup(tmp_path / "harris_tx.csv")
    assert single.find(apn="HA-001") is not None
    assert single.find(apn="PU-001") is None


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    module = sys.modules[__name__]
    for name in sorted(dir(module)):
        if name.startswith("test_"):
            fn = getattr(module, name)
            try:
                if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                    with tempfile.TemporaryDirectory() as td:
                        fn(Path(td))
                else:
                    fn()
                print(f"ok {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
