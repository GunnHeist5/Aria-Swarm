"""Anthropic-backed LlmClient plus post-call disposition extraction.

Live conversation turns stream through AsyncAnthropic (ConversationRelay is
the one natively-async edge of the system); the post-call disposition
classification is a small synchronous call because it runs inside the relay's
persist step via ``asyncio.to_thread``.

Current-API notes (deliberate, do not "fix" from older priors):
- claude-opus-5 rejects temperature/top_p/top_k — we pass none of them.
- Thinking is adaptive by default on claude-opus-5; we do not send a
  ``thinking`` parameter at all.
- Depth is controlled via ``output_config={"effort": ...}``.
- Safety classifiers can end a turn with ``stop_reason == "refusal"`` — on a
  live phone call the only acceptable behavior is a polite one-sentence
  close-out, never dead air or an error tone.

Fail-closed: constructing the client without an API key raises ConfigError.
Extraction is the opposite by design — it must never lose a finished call's
transcript, so any SDK/network failure degrades to keyword heuristics.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

import anthropic

from orchestrator.config import ConfigError, Settings
from orchestrator.logging_utils import get_logger
from orchestrator.models import Disposition

log = get_logger(__name__)

# Spoken instead of content when the model refuses to continue the turn.
REFUSAL_CLOSEOUT = (
    "I'm sorry, I'm not able to continue this call — thanks so much for your "
    "time, and have a great day."
)

# Fallback per-Mtok list prices for the default model (claude-opus-5:
# $5 in / $25 out). Used only when config/rates.json (owned by the report
# module) is absent or doesn't carry LLM rates.
_DEFAULT_IN_PER_MTOK = 5.0
_DEFAULT_OUT_PER_MTOK = 25.0

_CLASSIFY_SYSTEM = (
    "You classify finished outbound sales call transcripts. Answer with "
    "EXACTLY ONE lowercase label from this list and nothing else: "
    + ", ".join(d.value for d in Disposition)
    + ". Rules: any request to stop calling or be removed is opt_out; a "
    "meeting or follow-up actually agreed upon is booked; a full-time "
    "in-house receptionist means disqualified; reaching voicemail is "
    "voicemail; no prospect speech at all is no_answer."
)


class AnthropicLlm:
    """Streams agent utterances for the relay. Implements models.LlmClient."""

    def __init__(self, cfg: Settings):
        api_key = cfg.effective_llm_api_key()
        if not api_key:
            raise ConfigError(
                "missing LLM API key — set LLM_API_KEY or ANTHROPIC_API_KEY"
            )
        self._cfg = cfg
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        # Observable per-connection state: the relay reads these to hang up
        # after a refusal close-out and to estimate cost_llm_usd.
        self.last_stop_reason: str | None = None
        self.usage_input_tokens: int = 0
        self.usage_output_tokens: int = 0

    async def stream_reply(
        self, *, system: str, messages: list[dict]
    ) -> AsyncIterator[str]:
        async with self._client.messages.stream(
            model=self._cfg.llm_model,
            max_tokens=self._cfg.llm_max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self._cfg.llm_effort},
        ) as stream:
            async for token in stream.text_stream:
                yield token
            final = await stream.get_final_message()
        self.last_stop_reason = final.stop_reason
        usage = getattr(final, "usage", None)
        if usage is not None:
            self.usage_input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.usage_output_tokens += getattr(usage, "output_tokens", 0) or 0
        if final.stop_reason == "refusal":
            # A refusal turn carries no content — say goodbye instead of
            # going silent. The relay hangs up after seeing last_stop_reason.
            yield REFUSAL_CLOSEOUT


# ---------------------------------------------------------------------------
# Disposition extraction (sync — called from the relay's persist step)


def _classification_client(cfg: Settings) -> anthropic.Anthropic:
    """Separate factory so tests can monkeypatch the client construction."""
    return anthropic.Anthropic(api_key=cfg.effective_llm_api_key())


# Ordered, and the order is load-bearing: opt-out first (most protective —
# it drives permanent suppression); refusals BEFORE booked so "not
# interested" in a call where the agent merely *offered* to schedule doesn't
# read as a booking (a false BOOKED pushes to the CRM, the worse error); and
# "not interested" before "interested" because one contains the other.
_HEURISTIC_RULES: list[tuple[Disposition, tuple[str, ...]]] = [
    (
        Disposition.OPT_OUT,
        (
            "stop calling",
            "take me off",
            "do not call",
            "don't call",
            "remove me",
            "never call",
            "unsubscribe",
        ),
    ),
    (Disposition.WRONG_NUMBER, ("wrong number",)),
    (
        Disposition.DISQUALIFIED,
        ("full-time receptionist", "full time receptionist", "in-house receptionist"),
    ),
    (
        Disposition.VOICEMAIL,
        ("voicemail", "leave a message", "after the beep", "at the tone"),
    ),
    (Disposition.NOT_INTERESTED, ("not interested", "no thanks", "no thank you")),
    (Disposition.BOOKED, ("booked", "scheduled", "calendar invite", "see you then")),
    (Disposition.CALLBACK, ("call me back", "call back", "callback", "try me later")),
    (Disposition.INTERESTED, ("interested", "tell me more", "sounds good")),
]


def _heuristic_disposition(transcript: str) -> Disposition:
    text = transcript.lower()
    if "prospect:" not in text:
        # Transcript has agent lines only — nobody ever spoke to us.
        return Disposition.NO_ANSWER
    for disposition, phrases in _HEURISTIC_RULES:
        if any(phrase in text for phrase in phrases):
            return disposition
    # Connected but unclassifiable: NOT_INTERESTED is the safe default — it
    # triggers no side effects (no suppression, no retry, no CRM push).
    return Disposition.NOT_INTERESTED


def extract_disposition(transcript: str, cfg: Settings) -> Disposition:
    """Classify a finished call's transcript into a Disposition.

    Tries one small non-streaming model call; every SDK/network failure (and
    a missing API key) falls back to the keyword heuristics — a finished
    call must always end up with *some* disposition on its row.
    """
    if not transcript.strip():
        return Disposition.NO_ANSWER
    if cfg.effective_llm_api_key():
        try:
            client = _classification_client(cfg)
            response = client.messages.create(
                model=cfg.llm_model,
                # Headroom: on claude-opus-5 max_tokens caps adaptive
                # thinking + text together; 256 is still tiny for a one-label
                # answer but won't starve the turn.
                max_tokens=256,
                # Classification never needs depth — always cheapest effort,
                # independent of the conversation's cfg.llm_effort.
                output_config={"effort": "low"},
                system=_CLASSIFY_SYSTEM,
                messages=[{"role": "user", "content": transcript[-8000:]}],
            )
            if response.stop_reason != "refusal":
                text = "".join(
                    block.text
                    for block in response.content
                    if getattr(block, "type", None) == "text"
                )
                label = text.strip().lower().strip(".\"' ")
                for disposition in Disposition:
                    if label == disposition.value:
                        return disposition
                log.warning(
                    "disposition classifier returned unknown label %r; "
                    "falling back to heuristics",
                    label[:40],
                )
        except Exception as exc:  # noqa: BLE001 — any failure degrades, never raises
            log.warning(
                "disposition classification failed (%s); falling back to "
                "heuristics",
                type(exc).__name__,
            )
    return _heuristic_disposition(transcript)


# ---------------------------------------------------------------------------
# Cost estimation


def _llm_rates(cfg: Settings) -> tuple[float, float]:
    """(input, output) $ per Mtok — from config/rates.json when it has them.

    The rates file is owned by the report module and may not exist yet (or
    may not carry LLM keys); this reader is deliberately tolerant and falls
    back to the default model's list prices.
    """
    path = cfg.resolve_path(cfg.rates_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _DEFAULT_IN_PER_MTOK, _DEFAULT_OUT_PER_MTOK
    llm = data.get("llm") if isinstance(data, dict) else None
    candidates = llm if isinstance(llm, dict) else data if isinstance(data, dict) else {}
    in_rate = candidates.get("input_per_mtok", candidates.get("llm_input_per_mtok"))
    out_rate = candidates.get("output_per_mtok", candidates.get("llm_output_per_mtok"))
    try:
        return float(in_rate), float(out_rate)
    except (TypeError, ValueError):
        return _DEFAULT_IN_PER_MTOK, _DEFAULT_OUT_PER_MTOK


def estimate_llm_cost_usd(
    cfg: Settings, input_tokens: int, output_tokens: int
) -> float:
    in_rate, out_rate = _llm_rates(cfg)
    return round(
        (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 5
    )
