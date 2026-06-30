"""tools/wholesaling/ — the swarm's vacant-land / real-estate wholesaling operator.

Consolidates the "Muffin" operation onto this tested codebase. Split into:

  * A **deterministic operational core** (this package) — scoring, offer math,
    escalation rules. No LLM, no network: arithmetic that runs the same way
    every time (a reliability win over an LLM-scored pipeline).
  * A **genome-configurable strategy** (`config.WholesalingConfig`) — the
    scoring weights, offer multipliers, and thresholds the swarm's evolution
    (sandbox/HGT) mutates and the metabolic ratio selects.

Live I/O adapters (PropStream, Google Sheets, Gmail, Twilio, PandaDoc, RelayFi)
and the human approval gates (mapped to the swarm's HITL) arrive in later
increments — each behind a seam, offline-tested, live-validated on the VPS.
"""

from .config import WholesalingConfig, DEFAULT_CONFIG
from .scoring import score_lead, rank_leads
from .deals import max_offer, evaluate_deal

__all__ = [
    "WholesalingConfig",
    "DEFAULT_CONFIG",
    "score_lead",
    "rank_leads",
    "max_offer",
    "evaluate_deal",
]
