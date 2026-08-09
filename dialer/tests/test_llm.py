"""AnthropicLlm streaming + disposition extraction. The anthropic client is
always faked — construction is allowed (no network happens there) but every
messages.* surface is replaced before use.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from conftest import make_settings
from orchestrator.config import ConfigError
from orchestrator.llm import anthropic_client as ac
from orchestrator.llm import get_llm_client
from orchestrator.llm.anthropic_client import (
    REFUSAL_CLOSEOUT,
    AnthropicLlm,
    estimate_llm_cost_usd,
    extract_disposition,
)
from orchestrator.models import Disposition


def _cfg(**overrides):
    return make_settings(llm_api_key="test-key-123456", **overrides)


# ----------------------------------------------------------- fake async client


class FakeStream:
    def __init__(self, tokens, final):
        self._tokens = tokens
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for tok in self._tokens:
                yield tok

        return gen()

    async def get_final_message(self):
        return self._final


class FakeAsyncMessages:
    def __init__(self, tokens, final):
        self._tokens = tokens
        self._final = final
        self.kwargs: dict | None = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return FakeStream(self._tokens, self._final)


def _final(stop_reason="end_turn", input_tokens=100, output_tokens=20):
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _fake_llm(tokens, final):
    llm = AnthropicLlm(_cfg())
    messages = FakeAsyncMessages(tokens, final)
    llm._client = SimpleNamespace(messages=messages)
    return llm, messages


# -------------------------------------------------------------- construction


def test_missing_api_key_fails_closed():
    with pytest.raises(ConfigError):
        AnthropicLlm(make_settings())


def test_anthropic_api_key_is_accepted_as_fallback():
    llm = AnthropicLlm(make_settings(anthropic_api_key="fallback-key-123"))
    assert llm.last_stop_reason is None


def test_registry_rejects_unknown_provider():
    with pytest.raises(ConfigError):
        get_llm_client(make_settings(llm_provider="gpt-o"))


# ----------------------------------------------------------------- streaming


async def test_stream_reply_yields_tokens_and_tracks_usage():
    llm, messages = _fake_llm(["Hi", " there"], _final())
    out = [tok async for tok in llm.stream_reply(
        system="SYS", messages=[{"role": "user", "content": "hello"}]
    )]
    assert out == ["Hi", " there"]
    assert llm.last_stop_reason == "end_turn"
    assert llm.usage_input_tokens == 100
    assert llm.usage_output_tokens == 20


async def test_stream_request_shape_matches_current_api():
    cfg = _cfg(llm_model="claude-opus-5", llm_effort="low", llm_max_tokens=512)
    llm = AnthropicLlm(cfg)
    messages = FakeAsyncMessages(["ok"], _final())
    llm._client = SimpleNamespace(messages=messages)
    async for _ in llm.stream_reply(system="S", messages=[{"role": "user", "content": "x"}]):
        pass
    kw = messages.kwargs
    assert kw["model"] == "claude-opus-5"
    assert kw["max_tokens"] == 512
    assert kw["output_config"] == {"effort": "low"}
    # Rejected on claude-opus-5 — must never be sent.
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in kw


async def test_refusal_yields_polite_closeout_instead_of_content():
    llm, _ = _fake_llm([], _final(stop_reason="refusal"))
    out = [tok async for tok in llm.stream_reply(
        system="S", messages=[{"role": "user", "content": "x"}]
    )]
    assert out == [REFUSAL_CLOSEOUT]
    assert llm.last_stop_reason == "refusal"


# -------------------------------------------------- disposition classification


def _fake_sync_client(label="opt_out", stop_reason="end_turn", error=None):
    class FakeSyncMessages:
        def __init__(self):
            self.kwargs = None

        def create(self, **kwargs):
            if error:
                raise error
            self.kwargs = kwargs
            return SimpleNamespace(
                stop_reason=stop_reason,
                content=[SimpleNamespace(type="text", text=label)],
            )

    return SimpleNamespace(messages=FakeSyncMessages())


def test_extract_disposition_uses_model_label(monkeypatch):
    client = _fake_sync_client("opt_out")
    monkeypatch.setattr(ac, "_classification_client", lambda cfg: client)
    result = extract_disposition("Agent: hi\nProspect: stop it", _cfg())
    assert result is Disposition.OPT_OUT
    # Classification is a plain non-streaming create with a strict label ask.
    assert client.messages.kwargs["output_config"] == {"effort": "low"}


def test_extract_disposition_tolerates_decorated_labels(monkeypatch):
    client = _fake_sync_client('  "Booked."  ')
    monkeypatch.setattr(ac, "_classification_client", lambda cfg: client)
    result = extract_disposition("Agent: hi\nProspect: yes let's do Friday", _cfg())
    assert result is Disposition.BOOKED


def test_extract_disposition_falls_back_on_sdk_error(monkeypatch):
    client = _fake_sync_client(error=RuntimeError("network down"))
    monkeypatch.setattr(ac, "_classification_client", lambda cfg: client)
    result = extract_disposition(
        "Agent: hi\nProspect: please stop calling me", _cfg()
    )
    assert result is Disposition.OPT_OUT


def test_extract_disposition_falls_back_on_garbage_label(monkeypatch):
    client = _fake_sync_client("maybe-later")
    monkeypatch.setattr(ac, "_classification_client", lambda cfg: client)
    result = extract_disposition("Agent: hi\nProspect: we booked it", _cfg())
    assert result is Disposition.BOOKED


def test_extract_disposition_without_key_uses_heuristics_only():
    cfg = make_settings()  # no key at all — must not attempt the SDK
    result = extract_disposition("Agent: hi\nProspect: take me off your list", cfg)
    assert result is Disposition.OPT_OUT


# ------------------------------------------------------------------ heuristics


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Agent: hi\nProspect: stop calling me", Disposition.OPT_OUT),
        ("Agent: hi\nProspect: take me off the list", Disposition.OPT_OUT),
        ("Agent: hi\nProspect: we got you booked for Friday", Disposition.BOOKED),
        ("Agent: hi\nProspect: you have the wrong number", Disposition.WRONG_NUMBER),
        (
            "Agent: hi\nProspect: we have a full-time receptionist",
            Disposition.DISQUALIFIED,
        ),
        ("Agent: hi\nProspect: not interested, thanks", Disposition.NOT_INTERESTED),
        (
            # A refusal in a call where the agent merely OFFERED to schedule
            # must not read as booked.
            "Agent: want to get scheduled?\nProspect: no thanks",
            Disposition.NOT_INTERESTED,
        ),
        ("Agent: hi\nProspect: call me back tomorrow", Disposition.CALLBACK),
        ("Agent: hi\nProspect: I'm interested, tell me more", Disposition.INTERESTED),
        ("Agent: hello?\nAgent: anyone there? [interrupted]", Disposition.NO_ANSWER),
        ("Agent: hi\nProspect: hmm what", Disposition.NOT_INTERESTED),
    ],
)
def test_heuristic_disposition(transcript, expected):
    assert ac._heuristic_disposition(transcript) is expected


def test_empty_transcript_is_no_answer():
    assert extract_disposition("", make_settings()) is Disposition.NO_ANSWER
    assert extract_disposition("   \n ", make_settings()) is Disposition.NO_ANSWER


# ------------------------------------------------------------------------ cost


def test_cost_estimate_default_rates_when_rates_file_absent():
    cfg = make_settings(rates_path="/nonexistent/rates.json")
    # claude-opus-5 list prices: $5/M in, $25/M out.
    assert estimate_llm_cost_usd(cfg, 1_000_000, 0) == pytest.approx(5.0)
    assert estimate_llm_cost_usd(cfg, 0, 1_000_000) == pytest.approx(25.0)
    assert estimate_llm_cost_usd(cfg, 2000, 500) == pytest.approx(0.0225)


def test_cost_estimate_reads_rates_file(tmp_path):
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps({"llm": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}))
    cfg = make_settings(rates_path=str(rates))
    assert estimate_llm_cost_usd(cfg, 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_cost_estimate_ignores_malformed_rates_file(tmp_path):
    rates = tmp_path / "rates.json"
    rates.write_text("{not json")
    cfg = make_settings(rates_path=str(rates))
    assert estimate_llm_cost_usd(cfg, 1_000_000, 0) == pytest.approx(5.0)
