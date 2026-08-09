"""TwilioProvider — the models.VoiceProvider implementation for Twilio.

Synchronous on purpose (the contract says so; async edges wrap calls in
``asyncio.to_thread``). Two invariants this module owns:

- ``cfg.twilio_auth_token`` must never appear in logs or exception text —
  every error path routes through ``_sanitize`` before raising, on top of
  the global log-redaction filter.
- Status mapping is conservative: only Twilio's terminal CallStatus values
  produce an Outcome; everything else maps to ``outcome=None`` so the
  completion path can't be tricked into finalizing a live call.
"""

from __future__ import annotations

from typing import Mapping

from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from orchestrator.config import Settings
from orchestrator.logging_utils import get_logger
from orchestrator.models import Outcome, StatusUpdate

from .twiml import build_relay_twiml

log = get_logger(__name__)

# Terminal statuses per Twilio's CallStatus lifecycle. "queued", "initiated",
# "ringing", and "in-progress" are transitional and must NOT finalize.
_STATUS_OUTCOME: dict[str, Outcome] = {
    "completed": Outcome.CONNECTED,  # refined by AMD below
    "busy": Outcome.BUSY,
    "no-answer": Outcome.NO_ANSWER,
    "failed": Outcome.FAILED,
    "canceled": Outcome.CANCELED,
}

# AnsweredBy values (AMD) that mean a machine picked up, not a human.
_MACHINE_ANSWERS = (
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
)


class TwilioVoiceError(RuntimeError):
    """A Twilio API call failed (message sanitized — never carries the token)."""


def _outcome_for(call_status: str, answered_by: str | None) -> Outcome | None:
    outcome = _STATUS_OUTCOME.get(call_status)
    if outcome is Outcome.CONNECTED and answered_by in _MACHINE_ANSWERS:
        return Outcome.VOICEMAIL
    return outcome


def parse_status_callback(form: Mapping) -> StatusUpdate:
    """Normalize a Twilio status-callback form POST into a StatusUpdate."""
    call_sid = form.get("CallSid")
    if not call_sid:
        raise ValueError("status callback without CallSid — refusing to guess")
    call_status = str(form.get("CallStatus", "")).lower()
    answered_by = form.get("AnsweredBy") or None
    duration_raw = form.get("CallDuration")
    try:
        duration = int(duration_raw) if duration_raw not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    return StatusUpdate(
        provider_call_id=str(call_sid),
        call_status=call_status,
        outcome=_outcome_for(call_status, answered_by),
        duration_sec=duration,
        answered_by=answered_by,
        recording_url=form.get("RecordingUrl") or None,
        raw=dict(form),
    )


def validate_twilio_signature(
    cfg: Settings, url: str, params: Mapping, signature: str
) -> bool:
    """X-Twilio-Signature check. No auth token configured ⇒ False (fail closed)."""
    if not cfg.twilio_auth_token:
        log.error("cannot validate Twilio signature: TWILIO_AUTH_TOKEN unset")
        return False
    if not signature:
        return False
    return RequestValidator(cfg.twilio_auth_token).validate(
        url, dict(params), signature
    )


class TwilioProvider:
    name = "twilio"

    def __init__(self, cfg: Settings):
        cfg.require("twilio_account_sid", "twilio_auth_token", "public_base_url")
        self._cfg = cfg
        self._client = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)

    # ------------------------------------------------------------- internal

    def _sanitize(self, text: str) -> str:
        # Belt and braces on top of the log redaction filter: exception text
        # can travel into DB `error` columns and alerts, not just logs.
        token = self._cfg.twilio_auth_token
        return text.replace(token, "[REDACTED]") if token else text

    def _wrap(self, action: str, exc: Exception) -> TwilioVoiceError:
        detail = self._sanitize(f"{type(exc).__name__}: {exc}")
        return TwilioVoiceError(f"Twilio {action} failed: {detail}")

    # ------------------------------------------------------------- protocol

    def start_call(
        self,
        *,
        to_number: str,
        from_number: str,
        variables: Mapping[str, str],
        attempt_id: int,
    ) -> str:
        """Originate the call; returns the Twilio CallSid."""
        merged = {"attempt_id": str(attempt_id), **{
            str(k): str(v) for k, v in variables.items()
        }}
        twiml = build_relay_twiml(self._cfg, variables=merged)
        kwargs: dict = {
            "to": to_number,
            "from_": from_number,
            "twiml": twiml,
            "status_callback": f"{self._cfg.public_base_url}/webhooks/twilio/status",
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "status_callback_method": "POST",
            # Hard wall-clock ceiling so a wedged call can't bill forever.
            "time_limit": self._cfg.max_call_minutes * 60,
        }
        if self._cfg.amd_enabled:
            kwargs["machine_detection"] = "Enable"
        try:
            call = self._client.calls.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 — wrapped + sanitized
            raise self._wrap(f"start_call attempt={attempt_id}", exc) from None
        log.info("started call attempt=%s sid=%s", attempt_id, call.sid)
        return call.sid

    def fetch_call_details(self, provider_call_id: str) -> StatusUpdate | None:
        """Poll one call's state; None when Twilio has no such call (404)."""
        try:
            call = self._client.calls(provider_call_id).fetch()
        except TwilioRestException as exc:
            if exc.status == 404:
                return None
            raise self._wrap(f"fetch_call_details {provider_call_id}", exc) from None
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(f"fetch_call_details {provider_call_id}", exc) from None
        call_status = str(call.status or "").lower()
        answered_by = getattr(call, "answered_by", None)
        try:
            duration = int(call.duration) if call.duration not in (None, "") else None
        except (TypeError, ValueError):
            duration = None
        return StatusUpdate(
            provider_call_id=provider_call_id,
            call_status=call_status,
            outcome=_outcome_for(call_status, answered_by),
            duration_sec=duration,
            answered_by=answered_by,
            recording_url=None,  # recordings arrive via callback, not here
            raw={"status": call_status, "duration": call.duration},
        )

    def fetch_call_cost_usd(self, provider_call_id: str) -> float | None:
        """Twilio-billed voice cost. Twilio reports price as a negative string."""
        try:
            call = self._client.calls(provider_call_id).fetch()
        except TwilioRestException as exc:
            if exc.status == 404:
                return None
            raise self._wrap(f"fetch_call_cost {provider_call_id}", exc) from None
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(f"fetch_call_cost {provider_call_id}", exc) from None
        if call.price in (None, ""):
            return None
        try:
            return abs(float(call.price))
        except (TypeError, ValueError):
            return None
