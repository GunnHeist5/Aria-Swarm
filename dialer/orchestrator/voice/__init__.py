"""Voice provider registry — the swap point the SPEC demands.

Twilio is provider #1, but nothing outside ``orchestrator/voice`` may import
a provider SDK; callers get a ``models.VoiceProvider`` from
``get_voice_provider()`` and stay ignorant of what's behind it. Adding a
provider means adding a branch here plus a subpackage — no call-site changes.
"""

from __future__ import annotations

from orchestrator.config import ConfigError, Settings
from orchestrator.models import VoiceProvider

__all__ = ["get_voice_provider"]


def get_voice_provider(cfg: Settings) -> VoiceProvider:
    """Registry keyed by cfg.voice_provider. Unknown provider fails closed."""
    name = (cfg.voice_provider or "").strip().lower()
    if name == "twilio":
        from .twilio.provider import TwilioProvider

        return TwilioProvider(cfg)
    raise ConfigError(
        f"unknown VOICE_PROVIDER {cfg.voice_provider!r} — supported: twilio"
    )
