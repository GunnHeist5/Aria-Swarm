"""ConversationRelay websocket handler: setup → prompt → stream → persist.

The websocket, the LLM client, and the DB layer are all faked; assertions pin
the outbound message shapes ({"type":"text",...} / {"type":"end"}), the
transcript/dedupe persistence, and the never-finalize rule (only transcript /
disposition / cost columns are touched).
"""

from __future__ import annotations

import json

import pytest
from fastapi import WebSocketDisconnect

from conftest import make_settings
from orchestrator import db as db_mod
from orchestrator import llm as llm_mod
from orchestrator.models import Disposition
from orchestrator.voice.twilio import relay


def _cfg():
    return make_settings()


def setup_frame(**overrides):
    params = {
        "attempt_id": "7",
        "company_name": "Acme Plumbing",
        "touch_type": "first",
        "opener_variant": "opener_a",
        "two_party_disclose": "false",
    }
    params.update(overrides.pop("params", {}))
    frame = {"type": "setup", "callSid": "CA1", "customParameters": params}
    frame.update(overrides)
    return frame


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_text(self) -> str:
        if not self.frames:
            raise WebSocketDisconnect(1000)
        frame = self.frames.pop(0)
        return frame if isinstance(frame, str) else json.dumps(frame)

    async def send_text(self, data: str):
        self.sent.append(json.loads(data))


class FakeLlm:
    """Scripted replies; one entry per expected prompt turn."""

    def __init__(self, replies=(("Great", " question."),), stop_reason="end_turn"):
        self.replies = [list(r) for r in replies]
        self.stop_reason = stop_reason
        self.last_stop_reason = None
        self.usage_input_tokens = 300
        self.usage_output_tokens = 40
        self.calls: list[tuple[str, list[dict]]] = []

    async def stream_reply(self, *, system, messages):
        self.calls.append((system, list(messages)))
        for token in self.replies.pop(0):
            yield token
        self.last_stop_reason = self.stop_reason


@pytest.fixture
def db_calls(monkeypatch):
    calls: list[tuple[str, tuple]] = []

    def fake_execute(cfg, sql, params=None):
        calls.append((" ".join(sql.split()), params))
        return 1

    monkeypatch.setattr(db_mod, "execute", fake_execute)
    return calls


@pytest.fixture
def wired(monkeypatch, db_calls):
    """Standard wiring: fake LLM, fixed disposition + cost, recorded DB."""
    fake_llm = FakeLlm()
    monkeypatch.setattr(llm_mod, "get_llm_client", lambda cfg: fake_llm)
    extracted: list[str] = []

    def fake_extract(transcript, cfg):
        extracted.append(transcript)
        return Disposition.INTERESTED

    monkeypatch.setattr(llm_mod, "extract_disposition", fake_extract)
    monkeypatch.setattr(
        llm_mod, "estimate_llm_cost_usd", lambda cfg, i, o: round(i * 1e-6 + o * 1e-6, 5)
    )
    return fake_llm, extracted, db_calls


async def test_full_call_streams_tokens_and_persists(wired):
    fake_llm, extracted, db_calls = wired
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "Who is this?", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())

    assert socket.accepted
    # Outbound: one frame per token (last=False), then the last=True marker.
    text_frames = [f for f in socket.sent if f["type"] == "text"]
    assert [f["token"] for f in text_frames] == ["Great", " question.", ""]
    assert [f["last"] for f in text_frames] == [False, False, True]

    # LLM saw the prospect turn as role=user (first message must be user).
    system, messages = fake_llm.calls[0]
    assert "outbound AI sales representative" in system
    assert messages[0] == {"role": "user", "content": "Who is this?"}

    # Persisted transcript: greeting + prospect + agent reply, in order.
    transcript = extracted[0]
    lines = transcript.splitlines()
    assert lines[0].startswith("Agent: ")
    assert "AI assistant calling on behalf of Justin at Reachwell" in lines[0]
    assert lines[1] == "Prospect: Who is this?"
    assert lines[2] == "Agent: Great question."

    # DB: dedupe insert first, then the attempt update — and ONLY transcript/
    # disposition/cost columns (never status/outcome: no finalizing here).
    assert len(db_calls) == 2
    insert_sql, insert_params = db_calls[0]
    assert "INSERT INTO webhook_events" in insert_sql
    assert insert_params[0] == "CA1"
    update_sql, update_params = db_calls[1]
    assert "UPDATE call_attempts" in update_sql
    assert "status" not in update_sql and "outcome" not in update_sql
    assert update_params[0] == transcript
    assert update_params[1] == Disposition.INTERESTED.value
    assert update_params[2] == pytest.approx(0.00034)  # 300+40 tokens @ $1/Mtok
    assert update_params[3] == 7


async def test_partial_prompts_accumulate_into_one_turn(wired):
    fake_llm, _, _ = wired
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "Who", "last": False},
        {"type": "prompt", "voicePrompt": "is this?", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0][1][0]["content"] == "Who is this?"


async def test_duplicate_relay_result_is_not_rewritten(monkeypatch, wired):
    _, extracted, db_calls = wired

    def dedupe_execute(cfg, sql, params=None):
        db_calls.append((" ".join(sql.split()), params))
        return 0  # webhook_events row already exists

    monkeypatch.setattr(db_mod, "execute", dedupe_execute)
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "Hi", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    assert len(db_calls) == 1  # dedupe insert only — no UPDATE
    assert extracted == []


async def test_interrupt_marks_agent_line_truncated(wired):
    fake_llm, extracted, _ = wired
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "Hello?", "last": True},
        {"type": "interrupt", "utteranceUntilInterrupt": "Great"},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    transcript = extracted[0]
    assert "Agent: Great [interrupted]" in transcript
    assert "Agent: Great question." not in transcript


async def test_refusal_closes_the_call(monkeypatch, wired):
    fake_llm, _, _ = wired
    fake_llm.stop_reason = "refusal"
    fake_llm.replies = [["I'm sorry, I can't continue — have a great day."]]
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "hey", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    assert {"type": "end"} in socket.sent


async def test_llm_stream_failure_speaks_closeout_then_ends(monkeypatch, wired):
    fake_llm, extracted, _ = wired

    async def broken_stream(*, system, messages):
        raise RuntimeError("api down")
        yield  # pragma: no cover — makes this an async generator

    fake_llm.stream_reply = broken_stream
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "hello", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    final_texts = [f for f in socket.sent if f["type"] == "text" and f["last"]]
    assert final_texts and final_texts[0]["token"]  # spoke a close-out
    assert {"type": "end"} in socket.sent
    assert "Prospect: hello" in extracted[0]  # transcript still persisted


async def test_missing_llm_key_fails_closed_with_polite_end(db_calls):
    # No monkeypatched get_llm_client: the real one raises ConfigError
    # because make_settings() carries no API key.
    socket = FakeSocket([
        setup_frame(),
        {"type": "prompt", "voicePrompt": "hello", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    assert {"type": "end"} in socket.sent
    assert not any(f["type"] == "text" for f in socket.sent)


async def test_dtmf_and_garbage_frames_are_tolerated(wired):
    _, extracted, _ = wired
    socket = FakeSocket([
        setup_frame(),
        "this is not json {{{",
        {"type": "dtmf", "digit": "3"},
        {"type": "error", "description": "tts glitch"},
        {"type": "prompt", "voicePrompt": "ok", "last": True},
    ])
    await relay.handle_relay_socket(socket, _cfg())
    assert "Prospect: [pressed 3]" in extracted[0]


async def test_no_setup_means_nothing_persisted(db_calls):
    socket = FakeSocket([{"type": "prompt", "voicePrompt": "hi", "last": True}])
    await relay.handle_relay_socket(socket, _cfg())
    assert db_calls == []
