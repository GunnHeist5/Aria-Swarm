"""LLM bridge — the conversation brain behind ConversationRelay.

Nothing outside this package imports the anthropic SDK (architecture rule);
callers go through ``get_llm_client()`` so the backend stays swappable, the
same way the voice layer hides the Twilio SDK. The disposition extractor and
cost estimator are re-exported here so the relay (and tests) have one stable
monkeypatchable surface: ``orchestrator.llm.<name>``.
"""

from __future__ import annotations

from orchestrator.config import ConfigError, Settings
from orchestrator.models import LlmClient

from .anthropic_client import (  # noqa: F401  (re-exported surface)
    estimate_llm_cost_usd,
    extract_disposition,
)

__all__ = ["estimate_llm_cost_usd", "extract_disposition", "get_llm_client"]


def get_llm_client(cfg: Settings) -> LlmClient:
    """Registry keyed by cfg.llm_provider. Unknown provider fails closed."""
    provider = (cfg.llm_provider or "").strip().lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicLlm

        return AnthropicLlm(cfg)
    raise ConfigError(
        f"unknown LLM_PROVIDER {cfg.llm_provider!r} — supported: anthropic"
    )
