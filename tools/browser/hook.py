"""tools/browser/hook.py — the swarm's executor bridge for a PropStream pull.

The graph never launches a browser inside a node's invoke (CLAUDE.md: browser
automation lives in the isolated runner, and the invoke must not block on slow
un-mockable IO). Instead a node RECORDS the intent
(operational_flags["pipeline_actions_due"]); this thin executor does the real
pull out-of-band and, on success, fires ``new_leads_synced`` so the swarm's
bookkeeping learns the pipeline grew.
"""

from __future__ import annotations

import subprocess
import sys

from .config import BrowserConfig, load_config
from .driver import AuthChallenge, BrowserError
from .session import freeze_hitl, real_driver, resolve_credentials


def pipeline_replenish(county: str, state: str, *, config: BrowserConfig | None = None,
                       config_path: str | None = None, fire_event: bool = True,
                       **overrides) -> dict:
    """Run one PropStream pull for a market; hand off to the inbox pipeline.

    Returns {ok, file|error}. On an auth challenge it freezes (HITL webhook)
    and returns ok=False rather than raising into a caller/cron.
    """

    from . import propstream

    cfg = (config or load_config(config_path)).mutate(**overrides)
    try:
        user, pw = resolve_credentials(cfg)
    except Exception as exc:  # noqa: BLE001 — missing creds is operator config
        return {"ok": False, "error": f"credentials: {exc}"}

    try:
        with real_driver(cfg) as driver:
            dest = propstream.run_pull(driver, cfg, county, state,
                                       username=user, password=pw)
    except AuthChallenge as exc:
        freeze_hitl(f"{county}/{state}: {exc}", cfg)
        return {"ok": False, "error": f"auth_challenge: {exc}", "frozen": True}
    except BrowserError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if fire_event:
        _fire_new_leads_synced(county, state)
    return {"ok": True, "file": str(dest)}


def _fire_new_leads_synced(county: str, state: str) -> None:
    """Tell the swarm the pipeline grew (existing event; bookkeeping only)."""

    payload = ('{"source": "propstream_browser", '
               f'"county": "{county}", "state": "{state}"}}')
    try:
        subprocess.run(
            [sys.executable, "main.py", "--event", "new_leads_synced",
             "--payload", payload],
            cwd=_repo_root(), timeout=120, capture_output=True, text=True)
    except Exception:  # noqa: BLE001 — bookkeeping is best-effort, pull already won
        pass


def _repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])
