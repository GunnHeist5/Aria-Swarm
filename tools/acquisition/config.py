"""tools/acquisition/config.py — the acquisition agent's config genome.

Frozen dataclass + optional YAML overlay, same pattern as
``tools/screener/config.py``. The offer-box economics are REUSED from the
screener's genome (single source of truth for ratios) — override them here
only via yaml/mutate, never by editing two places.

Autonomy flags all default OFF. ``auto_send_offers`` exists so the graduated-
autonomy conversation is a config change later, but nothing in this package
reads it as permission yet: the reply agent drafts, the human sends.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace

from ..screener.config import DEFAULT_CONFIG as _SCREENER


@dataclass(frozen=True)
class AcquisitionConfig:
    # --- ledger ---------------------------------------------------------
    db_path: str = "~/.automaton/acquisition.db"

    # --- PropStream export quota (M3 records against this) --------------
    quota_monthly: int = 50_000

    # --- offer box (reused from the screener genome) ---------------------
    buyer_ceiling_ratio: float = _SCREENER.buyer_ceiling_ratio
    target_fee_usd: float = _SCREENER.target_fee_usd
    min_fee_usd: float = _SCREENER.min_fee_usd
    open_at_ratio: float = _SCREENER.open_at_ratio

    # --- enrollment gates -------------------------------------------------
    # Only these screener verdicts may enter a campaign. Unscreened leads
    # (verdict NULL) need the explicit --include-unscreened override.
    eligible_verdicts: tuple[str, ...] = ("SEND CONTRACT", "NEGOTIATE")
    # Statuses that may be considered for enrollment at all. Everything else
    # is excluded — and negotiating/offer_out/contracted are additionally
    # HARD-FAIL statuses (see ledger.assert_enrollable): never cold-email
    # someone mid-deal.
    enrollable_statuses: tuple[str, ...] = ("new", "screened", "traced")

    # --- browser pacing (M3; humans don't click every 100 ms) ------------
    action_delay_min_s: float = 2.0
    action_delay_max_s: float = 5.0
    session_max_actions: int = 400

    # --- autonomy flags (ALL OFF; the review queue is the only send path) -
    auto_send_offers: bool = False
    graduated_autonomy: bool = False

    def mutate(self, **changes) -> "AcquisitionConfig":
        return replace(self, **changes)


DEFAULT_CONFIG = AcquisitionConfig()


def load_config(path: str | None = None) -> AcquisitionConfig:
    """DEFAULT_CONFIG overlaid with a YAML mapping; unknown keys error."""

    if not path:
        return DEFAULT_CONFIG
    import yaml

    with open(path, encoding="utf-8") as fh:
        overlay = yaml.safe_load(fh) or {}
    if not isinstance(overlay, dict):
        raise ValueError(f"config overlay must be a mapping: {path}")
    known = {f.name for f in fields(AcquisitionConfig)}
    unknown = set(overlay) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    for key in ("eligible_verdicts", "enrollable_statuses"):
        if key in overlay and isinstance(overlay[key], list):
            overlay[key] = tuple(overlay[key])
    return DEFAULT_CONFIG.mutate(**overlay)


def config_hash(config: AcquisitionConfig = DEFAULT_CONFIG) -> str:
    blob = json.dumps(asdict(config), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]
