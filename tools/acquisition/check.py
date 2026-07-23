"""tools/acquisition/check.py — `acquire --check` live self-probes.

Same convention as the screener/dealflow checks: run on the VPS, exercise
each dependency lightly, print a JSON report, exit 0 iff everything the
current milestone needs is green. No pulls, no pushes, no PII in output.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ledger


def run_check(*, http_request=None) -> dict:
    out: dict = {"ok": True}

    # 1. ledger schema (creates the DB if missing — idempotent)
    try:
        conn = ledger.connect()
        ledger.init_db(conn)
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        need = {"leads", "suppression", "quota", "campaign_map"}
        out["ledger"] = {"path": str(ledger.db_path()),
                        "tables_ok": need <= tables}
        if not need <= tables:
            out["ok"] = False
    except Exception as exc:  # noqa: BLE001
        out["ledger"] = {"error": str(exc)}
        out["ok"] = False

    # 2. screener importable as a library (enrichment dependency)
    try:
        from ..screener.cli import run_pipeline  # noqa: F401

        out["screener_import"] = True
    except Exception as exc:  # noqa: BLE001
        out["screener_import"] = f"FAILED: {exc}"
        out["ok"] = False

    # 3. playbook present (draft stage hard-requires it)
    playbook = Path(__file__).with_name("playbook.md")
    out["playbook"] = playbook.is_file()
    if not playbook.is_file():
        out["ok"] = False

    # 4. Instantly auth (list one lead — read-only)
    try:
        from ..integrations.instantly import LIST_URL, _http_request
        from ..integrations.secrets import get_secret

        api_key = get_secret("INSTANTLY_API_KEY", required=True)
        req = http_request or _http_request
        status, _ = req("POST", LIST_URL, {"limit": 1}, api_key)
        out["instantly_auth"] = {"status": status, "ok": status not in (401, 403)}
        if status in (401, 403):
            out["ok"] = False
    except Exception as exc:  # noqa: BLE001
        out["instantly_auth"] = {"error": str(exc), "ok": False}
        out["ok"] = False

    # 5. classify + draft dry run on a fixture reply (offline — stub LLM;
    #    proves the reply pipeline's wiring and guards without spending tokens)
    try:
        from .reply.classify import classify_text
        from .reply.draft import dollars_in, holding_draft, load_playbook

        class _StubLLM:
            def invoke(self, _prompt):
                return ('{"classification": "interested_no_price", '
                        '"price_mentioned": null, "summary": "fixture"}')

        fixture = classify_text("How much would you offer for my land?",
                                llm=_StubLLM())
        hold = holding_draft({"reply_text": ""}, reason="fixture")
        load_playbook()
        out["reply_dry_run"] = {
            "classify": fixture["classification"] == "interested_no_price",
            "holding_has_no_number": dollars_in(hold["draft"]) == [],
        }
        if not all(out["reply_dry_run"].values()):
            out["ok"] = False
    except Exception as exc:  # noqa: BLE001
        out["reply_dry_run"] = {"error": str(exc)}
        out["ok"] = False

    # 6. PropStream login probe — arrives with M3 (browser pull)
    out["propstream"] = "not built yet (M3)"

    return out


def main() -> int:
    report = run_check()
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
