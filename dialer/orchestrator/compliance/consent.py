"""Consent configuration: the per-touch consent basis and two-party states.

The consent basis is REQUIRED with NO default (SPEC §6): these functions
report what is configured and never invent a value. Leaving the env vars
unset means contacts sync but are never dialable — that is the intended
fail-closed behavior, not a bug.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..config import ConfigError, Settings
from ..models import TouchType


def consent_basis_for(cfg: Settings, touch: TouchType) -> str | None:
    """The configured consent basis for a touch type; None = not dialable."""
    if touch == TouchType.FIRST:
        return cfg.consent_basis_first_touch
    return cfg.consent_basis_second_touch


@lru_cache(maxsize=8)
def _load_two_party_states(path_str: str) -> frozenset[str]:
    """Parse two_party_states.json: {"states": [...]} (or a bare list);
    "_"-prefixed keys are documentation. Missing/invalid file -> ConfigError,
    because recording policy is compliance input.
    """
    path = Path(path_str)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"two-party states file missing/invalid: {path}") from exc
    if isinstance(data, list):
        states = data
    elif isinstance(data, dict):
        states = data.get("states")
        if states is None:
            raise ConfigError(f"two-party states file needs a 'states' array: {path}")
    else:
        raise ConfigError(f"two-party states file must be a JSON object or array: {path}")
    return frozenset(str(s).upper() for s in states)


def is_two_party_state(cfg: Settings, state: str | None) -> bool:
    """Whether all-party recording consent applies.

    Unknown state (None) returns True: when we can't prove a one-party state,
    we apply the stricter policy (disclose or don't record). That costs a
    sentence of script, never a violation.
    """
    if state is None:
        return True
    listed = _load_two_party_states(str(cfg.resolve_path(cfg.two_party_states_path)))
    return state.upper() in listed
