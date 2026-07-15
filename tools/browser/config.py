"""tools/browser/config.py — the browser-runner genome + selector map.

Same pattern as tools/screener/config.py: a frozen dataclass holding every
tunable as DATA, a YAML overlay, and a config hash for provenance. The
SELECTORS map lives here too — every PropStream element as data, so DOM
drift on the VPS is a one-line edit in ``browser.yaml`` (never a redeploy).

The SELECTORS defaults are EDUCATED GUESSES. PropStream is login-walled and
the dev sandbox can't reach it, so they must be calibrated once on the VPS
with ``python -m tools.browser.cli --check`` (see calibrate.py).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class BrowserConfig:
    # -- hosts (data; PropStream runs a separate login front end) --
    login_url: str = "https://login.propstream.com/"
    app_url: str = "https://app.propstream.com/"

    # -- vacant-land filter defaults (None/False => that control is skipped) --
    property_class: str = "Vacant Land"   # Property CLASS (raw land), not the "Vacant" quick list
    absentee_owner: bool = True
    owner_occupied: bool = False
    tax_delinquent: bool = False
    lot_min_acres: float | None = 0.5
    lot_max_acres: float | None = 100.0
    assessed_min_usd: float | None = None
    assessed_max_usd: float | None = None
    equity_min_pct: float | None = None

    # -- run behaviour / ToS guardrails --
    run_skiptrace: bool = True            # Email/Phone columns only exist AFTER skip trace
    max_export_rows: int = 5000           # hard cap << monthly pool; fail closed if the
                                          # filtered count exceeds it (narrow filters instead)
    saved_list_name: str = "aria-{county}-{state}-{date}"

    # -- pacing (human cadence; ToU "non-customary usage" avoidance) --
    action_min_ms: int = 350
    action_max_ms: int = 1200
    type_delay_ms: int = 40

    # -- timeouts --
    default_timeout_ms: int = 12_000
    nav_timeout_ms: int = 60_000
    download_timeout_ms: int = 180_000    # large exports blow past the 30s default

    # -- paths / dirs (repo convention: ~/.automaton for durable state) --
    download_dir: str = "~/.automaton/propstream/downloads"
    storage_state: str = "~/.automaton/propstream/storage_state.json"  # credential, 0600
    user_data_dir: str = "~/.automaton/propstream/profile"             # 0700
    artifact_dir: str = "~/.automaton/propstream/artifacts"
    headless: bool = True                 # seed-login / --headful flip this to False

    # -- credential env NAMES (values via get_secret; never inline, never logged) --
    username_env: str = "PROPSTREAM_USERNAME"
    password_env: str = "PROPSTREAM_PASSWORD"

    def mutate(self, **changes) -> "BrowserConfig":
        return replace(self, **changes)


DEFAULT_CONFIG = BrowserConfig()


# ---------------------------------------------------------------------------
# SELECTORS — logical key -> how to find it. GUESSED; calibrate on the VPS.
# Resolution priority in playwright_driver: role -> label -> text -> testid ->
# placeholder -> css. {property_class} etc. are .format()-substituted at resolve.
# ---------------------------------------------------------------------------

SELECTORS: dict[str, dict] = {
    # cookie consent (OneTrust overlay can intercept pre-login clicks)
    "consent.accept": {"by": "role", "role": "button", "name": "Accept All"},
    # login page (login.propstream.com)
    "login.username": {"by": "label", "text": "Email"},
    "login.password": {"by": "label", "text": "Password"},
    "login.submit": {"by": "role", "role": "button", "name": "Sign In"},
    "login.error": {"by": "text", "text": "incorrect"},
    # challenge markers -> HITL freeze if any is present
    "challenge.otp": {"by": "text", "text": "verification code"},
    "challenge.captcha": {"by": "css",
                          "css": "iframe[src*='captcha'], iframe[title*='Cloudflare']"},
    # post-login proof-of-state anchor (login VERIFY gate)
    "app.ready": {"by": "testid", "id": "app-shell"},
    # geography / search
    "search.box": {"by": "placeholder", "text": "Enter County, City, Zip"},
    "search.submit": {"by": "role", "role": "button", "name": "Search"},
    # filter panel
    "filters.open": {"by": "role", "role": "button", "name": "Filter"},
    "filters.property_class": {"by": "label", "text": "Property Class"},
    "filters.property_class_option": {"by": "role", "role": "option",
                                      "name": "{property_class}"},
    "filters.lot_min": {"by": "label", "text": "Lot Size Min"},
    "filters.lot_max": {"by": "label", "text": "Lot Size Max"},
    "filters.assessed_min": {"by": "label", "text": "Assessed Value Min"},
    "filters.assessed_max": {"by": "label", "text": "Assessed Value Max"},
    "filters.absentee": {"by": "label", "text": "Absentee Owner"},
    "filters.owner_occupied": {"by": "label", "text": "Owner Occupied"},
    "filters.tax_delinquent": {"by": "label", "text": "Tax Delinquent"},
    "filters.equity_min": {"by": "label", "text": "Equity Min"},
    "filters.result_count": {"by": "testid", "id": "result-count"},
    "filters.apply": {"by": "role", "role": "button", "name": "Apply"},
    "filters.active_chip": {"by": "testid", "id": "active-filter-chip"},  # filters VERIFY gate
    # list persistence
    "results.select_all": {"by": "testid", "id": "select-all-checkbox"},
    "results.add_to_list": {"by": "role", "role": "button", "name": "Add to List"},
    "list.name_input": {"by": "label", "text": "List Name"},
    "list.save": {"by": "role", "role": "button", "name": "Save"},
    # skip trace (must precede export to populate Email/Phone)
    "skiptrace.button": {"by": "role", "role": "button", "name": "Skip Trace"},
    "skiptrace.confirm": {"by": "role", "role": "button", "name": "Confirm"},
    "skiptrace.done": {"by": "text", "text": "Skip trace complete"},  # skiptrace VERIFY gate
    # export
    "export.button": {"by": "role", "role": "button", "name": "Export"},
    "export.confirm_csv": {"by": "role", "role": "menuitem", "name": "CSV"},
}

# Gates that MUST confirm before the flow may proceed (fail closed).
VERIFY_GATES: dict[str, list[str]] = {
    "login": ["app.ready"],
    "filters": ["filters.active_chip"],
    "skiptrace": ["skiptrace.done"],  # only enforced when cfg.run_skiptrace
}

CHALLENGE_KEYS = ("challenge.otp", "challenge.captcha", "login.error")


def load_config(path: str | None = None) -> BrowserConfig:
    """Default genome, overlaid with a YAML file. Unknown keys are an error."""

    if not path:
        return DEFAULT_CONFIG
    import yaml

    with open(path, encoding="utf-8") as fh:
        overrides = yaml.safe_load(fh) or {}
    if not isinstance(overrides, dict):
        raise ValueError(f"config file must be a YAML mapping: {path}")
    # A YAML overlay may carry a `selectors:` block (merged into SELECTORS by the
    # driver) alongside dataclass fields; only dataclass fields build the config.
    field_overrides = {k: v for k, v in overrides.items() if k != "selectors"}
    known = set(asdict(DEFAULT_CONFIG))
    unknown = set(field_overrides) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    return DEFAULT_CONFIG.mutate(**field_overrides)


def load_selectors(path: str | None = None) -> dict[str, dict]:
    """SELECTORS defaults, overlaid per-key from a YAML `selectors:` block."""

    merged = {k: dict(v) for k, v in SELECTORS.items()}
    if not path:
        return merged
    import yaml

    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for key, spec in (data.get("selectors") or {}).items():
        merged[key] = dict(spec)  # a calibrated key fully replaces the guess
    return merged


def config_hash(config: BrowserConfig = DEFAULT_CONFIG) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
