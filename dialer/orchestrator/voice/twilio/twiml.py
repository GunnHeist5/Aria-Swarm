"""TwiML for AI calls: <Connect><ConversationRelay> with per-call parameters.

The TwiML is tiny but compliance-critical: the welcomeGreeting is the first
thing the prospect hears, so it carries the assigned opener with its AI
disclosure (SPEC §6 — "the agent MUST disclose it is an AI at the start of
every call") and, in two-party consent states, the recording disclosure. The
<Parameter> elements come back to us verbatim in the relay socket's "setup"
message, which is how one websocket connection knows which call_attempts row
it is speaking for.
"""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlparse

from twilio.twiml.voice_response import Connect, VoiceResponse

from orchestrator.config import ConfigError, Settings

# The variables every relay TwiML must carry (architecture contract). Extra
# variables are passed through untouched — the payload is extensible (SPEC §3).
KNOWN_VARIABLES = (
    "attempt_id",
    "company_name",
    "touch_type",
    "opener_variant",
    "two_party_disclose",
)

_TRUTHY = ("true", "1", "yes")


def relay_websocket_url(cfg: Settings) -> str:
    """wss:// URL of our /ws/relay endpoint, derived from public_base_url."""
    cfg.require("public_base_url")
    host = urlparse(cfg.public_base_url).netloc
    if not host:
        raise ConfigError(
            f"PUBLIC_BASE_URL {cfg.public_base_url!r} has no host — expected "
            "e.g. https://dialer.example.com"
        )
    return f"wss://{host}/ws/relay"


def build_relay_twiml(cfg: Settings, *, variables: Mapping[str, str]) -> str:
    """Render the <Connect><ConversationRelay> document for one call."""
    ws_url = relay_websocket_url(cfg)

    # The greeting needs the opener text; import lazily so the voice package
    # imports cleanly even while sibling packages are still being built.
    from orchestrator.agent import script

    opener_variant = variables.get("opener_variant") or ""
    if not opener_variant:
        raise ConfigError(
            "build_relay_twiml: variables must carry opener_variant — the "
            "planner assigns it per attempt"
        )
    two_party = str(variables.get("two_party_disclose", "")).lower() in _TRUTHY
    greeting = script.opener_greeting(
        cfg,
        opener_variant=opener_variant,
        company_name=variables.get("company_name"),
        two_party_disclose=two_party,
    )

    response = VoiceResponse()
    connect = Connect()
    relay = connect.conversation_relay(url=ws_url, welcome_greeting=greeting)
    for name in KNOWN_VARIABLES:
        if name in variables:
            relay.parameter(name=name, value=str(variables[name]))
    for name, value in variables.items():
        if name not in KNOWN_VARIABLES:
            relay.parameter(name=name, value=str(value))
    response.append(connect)
    return str(response)
