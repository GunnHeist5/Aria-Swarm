"""ConversationRelay websocket bridge: Twilio speech ⇄ LLM tokens.

Twilio's docs were unreachable from this environment, so the message shapes
follow the architecture contract (marked assumption; verified against the
SDK's TwiML output): inbound ``{"type": "setup", ..., "customParameters":
{...}}`` / ``{"type": "prompt", "voicePrompt": str, "last": bool}`` /
``{"type": "interrupt", ...}`` / ``{"type": "dtmf", ...}`` / ``{"type":
"error", ...}``; outbound ``{"type": "text", "token": str, "last": bool}``
streamed per LLM token, ``{"type": "end"}`` to hang up.

Design rules:
- This handler NEVER finalizes the attempt — the Twilio status callback owns
  the lifecycle. On disconnect we persist the transcript + an LLM-extracted
  disposition + the LLM cost estimate onto call_attempts and nothing else,
  deduped via a ``relay:{call_sid}`` webhook_events row so a Twilio reconnect
  can't double-write.
- Sibling packages (llm, agent, db) are imported lazily inside functions so
  this module imports and tests before the rest of the tree exists — and so
  tests monkeypatch ``orchestrator.llm.*`` / ``orchestrator.db.*``.
- Never log voicePrompt/transcript content at INFO — call audio transcripts
  are PII.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from orchestrator.config import ConfigError, Settings
from orchestrator.logging_utils import get_logger
from orchestrator.models import TouchType

log = get_logger(__name__)

_TRUTHY = ("true", "1", "yes")


@dataclass
class _RelaySession:
    """Per-connection state (one websocket == one live call)."""

    params: dict[str, str] = field(default_factory=dict)
    call_sid: str | None = None
    attempt_id: int | None = None
    system_prompt: str = ""
    # Alternating conversation lines ("Agent: ..." / "Prospect: ...").
    transcript: list[str] = field(default_factory=list)
    # Anthropic-shaped turns (first entry must be role=user, so the spoken
    # greeting lives in the transcript + system prompt, not in messages).
    messages: list[dict] = field(default_factory=list)
    prompt_buffer: str = ""
    llm: object | None = None
    ended: bool = False


async def _send(websocket: WebSocket, payload: dict) -> None:
    await websocket.send_text(json.dumps(payload))


async def _end_call(websocket: WebSocket, session: _RelaySession) -> None:
    if session.ended:
        return
    session.ended = True
    try:
        await _send(websocket, {"type": "end"})
    except Exception:  # noqa: BLE001 — socket may already be gone
        pass


def _handle_setup(cfg: Settings, session: _RelaySession, msg: dict) -> None:
    """Capture custom parameters and build the per-call system prompt."""
    raw_params = msg.get("customParameters") or {}
    session.params = {str(k): str(v) for k, v in raw_params.items()}
    session.call_sid = msg.get("callSid") or session.params.get("call_sid")
    attempt_raw = session.params.get("attempt_id")
    try:
        session.attempt_id = int(attempt_raw) if attempt_raw else None
    except (TypeError, ValueError):
        session.attempt_id = None
    if session.attempt_id is None:
        log.error("relay setup without usable attempt_id (sid=%s)", session.call_sid)

    from orchestrator.agent import script  # lazy: sibling package

    touch = (
        TouchType.SECOND
        if session.params.get("touch_type") == TouchType.SECOND.value
        else TouchType.FIRST
    )
    two_party = session.params.get("two_party_disclose", "").lower() in _TRUTHY
    opener_variant = session.params.get("opener_variant") or (
        cfg.opener_variants[0] if cfg.opener_variants else ""
    )
    company_name = session.params.get("company_name") or None
    session.system_prompt = script.build_system_prompt(
        cfg,
        touch=touch,
        company_name=company_name,
        opener_variant=opener_variant,
        two_party_disclose=two_party,
    )
    # Twilio TTS already spoke the welcomeGreeting (from the TwiML) before
    # this socket saw a single prompt — record it so the transcript reads as
    # the prospect heard it and the disposition extractor sees the opener.
    greeting = script.opener_greeting(
        cfg,
        opener_variant=opener_variant,
        company_name=company_name,
        two_party_disclose=two_party,
    )
    session.transcript.append(f"Agent: {greeting}")
    log.info(
        "relay setup: attempt=%s sid=%s touch=%s opener=%s",
        session.attempt_id, session.call_sid, touch.value, opener_variant,
    )


async def _handle_prompt(
    cfg: Settings, websocket: WebSocket, session: _RelaySession, msg: dict
) -> None:
    """Accumulate partial prompts; on the final one, stream the LLM reply."""
    session.prompt_buffer = (
        f"{session.prompt_buffer} {msg.get('voicePrompt') or ''}".strip()
    )
    if not msg.get("last", True):
        return
    utterance, session.prompt_buffer = session.prompt_buffer, ""
    if not utterance or session.llm is None:
        return
    session.transcript.append(f"Prospect: {utterance}")
    session.messages.append({"role": "user", "content": utterance})

    reply_parts: list[str] = []
    try:
        async for token in session.llm.stream_reply(
            system=session.system_prompt, messages=list(session.messages)
        ):
            reply_parts.append(token)
            await _send(websocket, {"type": "text", "token": token, "last": False})
        await _send(websocket, {"type": "text", "token": "", "last": True})
    except WebSocketDisconnect:
        raise
    except Exception as exc:  # noqa: BLE001 — a live call must not go silent
        log.error("LLM stream failed mid-call (%s); closing out", type(exc).__name__)
        from orchestrator.llm.anthropic_client import REFUSAL_CLOSEOUT

        reply_parts = [REFUSAL_CLOSEOUT]
        await _send(
            websocket, {"type": "text", "token": REFUSAL_CLOSEOUT, "last": True}
        )
        await _end_call(websocket, session)

    reply = "".join(reply_parts).strip()
    if reply:
        session.messages.append({"role": "assistant", "content": reply})
        session.transcript.append(f"Agent: {reply}")
    if getattr(session.llm, "last_stop_reason", None) == "refusal":
        # AnthropicLlm already yielded the polite close-out — hang up.
        await _end_call(websocket, session)


def _handle_interrupt(session: _RelaySession, msg: dict) -> None:
    """The prospect spoke over us — mark the last agent line as truncated."""
    for i in range(len(session.transcript) - 1, -1, -1):
        if session.transcript[i].startswith("Agent: "):
            spoken = msg.get("utteranceUntilInterrupt")
            if spoken:
                session.transcript[i] = f"Agent: {spoken} [interrupted]"
            elif not session.transcript[i].endswith("[interrupted]"):
                session.transcript[i] += " [interrupted]"
            # Keep the LLM's view consistent with what was actually heard.
            if session.messages and session.messages[-1].get("role") == "assistant":
                session.messages[-1]["content"] = session.transcript[i][len("Agent: "):]
            break


def _persist_session(cfg: Settings, session: _RelaySession) -> None:
    """On disconnect: transcript + disposition + LLM cost onto the attempt.

    Deliberately does NOT touch status/outcome — the status callback owns
    finalization; a relay socket can drop while the call is still live.
    """
    if session.attempt_id is None or not session.transcript:
        return
    from orchestrator import db, llm  # lazy: monkeypatched in tests

    if session.call_sid:
        # relay:{call_sid} dedupe row — a second socket for the same call
        # (Twilio reconnect) must not overwrite the persisted transcript.
        inserted = db.execute(
            cfg,
            """
            INSERT INTO webhook_events (source, external_id, event_type, payload)
            VALUES ('twilio', %s, 'relay', %s)
            ON CONFLICT (source, external_id, event_type) DO NOTHING
            """,
            (session.call_sid, db.Jsonb({"attempt_id": session.attempt_id})),
        )
        if inserted == 0:
            log.info("relay result for sid=%s already persisted", session.call_sid)
            return

    transcript_text = "\n".join(session.transcript)
    disposition = llm.extract_disposition(transcript_text, cfg)
    cost_llm = llm.estimate_llm_cost_usd(
        cfg,
        getattr(session.llm, "usage_input_tokens", 0) or 0,
        getattr(session.llm, "usage_output_tokens", 0) or 0,
    )
    db.execute(
        cfg,
        """
        UPDATE call_attempts
           SET transcript = %s, disposition = %s, cost_llm_usd = %s,
               updated_at = now()
         WHERE id = %s
        """,
        (transcript_text, disposition.value, cost_llm, session.attempt_id),
    )
    log.info(
        "relay persisted attempt=%s disposition=%s llm_cost=%.5f",
        session.attempt_id, disposition.value, cost_llm,
    )


async def handle_relay_socket(websocket: WebSocket, cfg: Settings) -> None:
    """Serve one ConversationRelay connection end to end."""
    await websocket.accept()
    session = _RelaySession()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                log.warning("relay: non-JSON frame ignored")
                continue
            mtype = msg.get("type")
            if mtype == "setup":
                _handle_setup(cfg, session, msg)
                try:
                    from orchestrator import llm  # lazy: monkeypatched in tests

                    session.llm = llm.get_llm_client(cfg)
                except ConfigError as exc:
                    # No brain, no call: end politely rather than dead air.
                    log.error("relay cannot start LLM: %s", exc)
                    await _end_call(websocket, session)
            elif mtype == "prompt":
                if not session.ended:
                    await _handle_prompt(cfg, websocket, session, msg)
            elif mtype == "interrupt":
                _handle_interrupt(session, msg)
            elif mtype == "dtmf":
                digit = msg.get("digit")
                if digit is not None:
                    session.transcript.append(f"Prospect: [pressed {digit}]")
            elif mtype == "error":
                log.error("relay error frame: %s", msg.get("description"))
            else:
                log.warning("relay: unknown frame type %r ignored", mtype)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 — never lose the transcript
        log.error("relay socket crashed (%s)", type(exc).__name__)
    finally:
        try:
            await asyncio.to_thread(_persist_session, cfg, session)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "relay persist failed for attempt=%s (%s)",
                session.attempt_id, type(exc).__name__,
            )
