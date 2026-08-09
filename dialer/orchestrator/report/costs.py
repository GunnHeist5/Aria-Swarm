"""Per-call cost model. Cost tracking is a first-class requirement (SPEC §9).

Rates live in config/rates.json (RATES_PATH), not in code, because Twilio and
Anthropic reprice without asking us. `estimate_costs` prefers the
provider-billed voice figure when we have it (fetched from the Calls API after
completion) and falls back to duration × rate; ConversationRelay and
Conversational Intelligence are always duration-derived because Twilio bills
them per minute of call.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..config import ConfigError, Settings
from ..models import CallCosts

_DEFAULT_RATES = {
    "voice_per_min": 0.014,
    "relay_per_min": 0.07,
    "intelligence_per_min": 0.025,
    "number_monthly": 1.15,
    "llm_input_per_mtok": 5.0,
    "llm_output_per_mtok": 25.0,
}

_cache: tuple[Path, float, dict] | None = None


def load_rates(cfg: Settings) -> dict:
    """Load RATES_PATH, filling any missing key from the shipped defaults."""
    global _cache
    path = cfg.resolve_path(cfg.rates_path)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise ConfigError(f"rates file unreadable: {path} ({exc})") from exc
    if _cache is not None and _cache[0] == path and _cache[1] == mtime:
        return _cache[2]
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"rates file invalid: {path} ({exc})") from exc
    rates = {**_DEFAULT_RATES, **{k: v for k, v in loaded.items() if not k.startswith("_")}}
    _cache = (path, mtime, rates)
    return rates


def _billing_minutes(duration_sec: int | None) -> int:
    """Twilio bills per started minute."""
    if not duration_sec or duration_sec <= 0:
        return 0
    return math.ceil(duration_sec / 60)


def estimate_costs(
    cfg: Settings,
    *,
    duration_sec: int | None,
    provider_voice_usd: float | None,
    llm_usd: float | None,
) -> CallCosts:
    rates = load_rates(cfg)
    minutes = _billing_minutes(duration_sec)
    voice = (
        abs(provider_voice_usd)  # Twilio reports prices as negative amounts
        if provider_voice_usd is not None
        else minutes * float(rates["voice_per_min"])
    )
    return CallCosts(
        voice_usd=round(voice, 5),
        relay_usd=round(minutes * float(rates["relay_per_min"]), 5),
        intelligence_usd=round(minutes * float(rates["intelligence_per_min"]), 5),
        llm_usd=round(llm_usd or 0.0, 5),
    )
