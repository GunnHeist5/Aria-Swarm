"""Shared domain types: enums, dataclasses, and the provider interfaces.

This module is dependency-light on purpose (stdlib only) so every other module
can import it without cycles. The Protocols here are the swap points the spec
demands: the voice layer (Twilio first, others later), the DNC source, the LLM
brain, and the CRM/calendar sink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Mapping, Protocol, runtime_checkable


class TouchType(str, Enum):
    FIRST = "first"    # progress_status=Undialed — never worked by a rep
    SECOND = "second"  # progress_status=Dialed  — reps already touched them


class AttemptStatus(str, Enum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    DIALING = "dialing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    ORPHANED = "orphaned"


class Outcome(str, Enum):
    CONNECTED = "connected"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    VOICEMAIL = "voicemail"
    FAILED = "failed"
    CANCELED = "canceled"


class Disposition(str, Enum):
    BOOKED = "booked"
    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    OPT_OUT = "opt_out"
    DISQUALIFIED = "disqualified"
    CALLBACK = "callback"
    WRONG_NUMBER = "wrong_number"
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"
    FAILED = "failed"


class BlockReason(str, Enum):
    """Why a dial is not allowed. Every one of these is a hard block."""

    KILL_SWITCH = "kill_switch"
    PHASE_GATE = "phase_gate"
    SUPPRESSED = "suppressed"
    FEDERAL_DNC = "federal_dnc"
    DNC_SOURCE_MISSING = "dnc_source_missing"
    NO_CONSENT_BASIS = "no_consent_basis"
    CONTACT_DNCA = "contact_dnca"
    CONTACT_INVALID = "contact_invalid"
    INVALID_PHONE = "invalid_phone"
    UNKNOWN_AREA_CODE = "unknown_area_code"
    NON_US_NUMBER = "non_us_number"
    OUTSIDE_WINDOW = "outside_window"
    ATTEMPT_CAP = "attempt_cap"
    NO_NUMBER_AVAILABLE = "no_number_available"

    @property
    def is_permanent(self) -> bool:
        """Permanent blocks cancel the attempt; temporal ones reschedule it."""
        return self not in (
            BlockReason.KILL_SWITCH,
            BlockReason.OUTSIDE_WINDOW,
            BlockReason.NO_NUMBER_AVAILABLE,
        )


@dataclass
class DialDecision:
    allowed: bool
    reasons: list[BlockReason] = field(default_factory=list)
    # For OUTSIDE_WINDOW blocks: when the prospect's local window next opens.
    earliest_allowed: datetime | None = None

    @property
    def permanently_blocked(self) -> bool:
        return any(r.is_permanent for r in self.reasons)


@dataclass
class NumberLease:
    """A caller ID reserved for one dial (usage counter already incremented)."""

    number_id: int
    phone_e164: str
    area_code: str
    exact_area_match: bool


@dataclass
class CallCosts:
    voice_usd: float = 0.0
    relay_usd: float = 0.0
    intelligence_usd: float = 0.0
    llm_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return round(
            self.voice_usd + self.relay_usd + self.intelligence_usd + self.llm_usd, 5
        )


@dataclass
class StatusUpdate:
    """Normalized provider status callback (Twilio's form fields → this)."""

    provider_call_id: str
    call_status: str                 # provider-native status string
    outcome: Outcome | None          # normalized, once terminal
    duration_sec: int | None = None
    answered_by: str | None = None   # AMD result when enabled
    recording_url: str | None = None
    raw: Mapping[str, object] = field(default_factory=dict)


@dataclass
class CallResult:
    """Everything persisted onto call_attempts at completion time."""

    provider_call_id: str
    outcome: Outcome
    disposition: Disposition | None = None
    duration_sec: int | None = None
    transcript: str | None = None
    recording_url: str | None = None
    costs: CallCosts = field(default_factory=CallCosts)


# ---------------------------------------------------------------------------
# Swap points


@runtime_checkable
class VoiceProvider(Protocol):
    """The voice layer. Twilio first, but nothing outside orchestrator/voice
    may import a provider SDK — always go through get_voice_provider()."""

    name: str

    def start_call(
        self,
        *,
        to_number: str,
        from_number: str,
        variables: Mapping[str, str],
        attempt_id: int,
    ) -> str:
        """Originate a call; returns the provider call id. Blocking (sync)."""
        ...

    def fetch_call_details(self, provider_call_id: str) -> StatusUpdate | None:
        """Poll the provider for a call's current state (reconciliation)."""
        ...

    def fetch_call_cost_usd(self, provider_call_id: str) -> float | None:
        """Provider-billed voice cost for a finished call, if known yet."""
        ...


class DncChecker(Protocol):
    """Federal DNC scrub source. Absence of a source is itself a block."""

    def available(self) -> bool: ...

    def is_listed(self, phone_e164: str) -> bool: ...


class LlmClient(Protocol):
    """The conversation brain bridged into ConversationRelay."""

    async def stream_reply(
        self, *, system: str, messages: list[dict]
    ) -> AsyncIterator[str]:
        """Yield text tokens for the next agent utterance."""
        ...


class CrmSink(Protocol):
    """Booked meetings go here. Target system TBD — stub logs + outbox row."""

    def push_booking(self, *, attempt_id: int, details: Mapping[str, object]) -> None: ...
