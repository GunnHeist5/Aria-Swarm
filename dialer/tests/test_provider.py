"""TwilioProvider: status mapping, signature validation, origination kwargs,
cost fetch, and the never-leak-the-auth-token invariant. All Twilio traffic is
faked — no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from conftest import make_settings
from orchestrator.config import ConfigError
from orchestrator.models import Outcome
from orchestrator.voice.twilio.provider import (
    TwilioProvider,
    TwilioVoiceError,
    parse_status_callback,
    validate_twilio_signature,
)

AUTH_TOKEN = "supersecrettoken123"


def _cfg(**overrides):
    return make_settings(
        twilio_account_sid="ACxxxxxxxx",
        twilio_auth_token=AUTH_TOKEN,
        public_base_url="https://dialer.example.com",
        **overrides,
    )


class FakeCallResource:
    def __init__(self, **attrs):
        self._attrs = attrs
        self.fetch_error = attrs.pop("fetch_error", None)

    def fetch(self):
        if self.fetch_error:
            raise self.fetch_error
        return SimpleNamespace(**self._attrs)


class FakeCalls:
    """Stands in for client.calls: attribute .create AND callable calls(sid)."""

    def __init__(self, resource: FakeCallResource | None = None,
                 create_error: Exception | None = None):
        self.resource = resource
        self.create_error = create_error
        self.create_kwargs: dict | None = None

    def create(self, **kwargs):
        if self.create_error:
            raise self.create_error
        self.create_kwargs = kwargs
        return SimpleNamespace(sid="CA123")

    def __call__(self, sid):
        return self.resource


def _provider(fake_calls: FakeCalls, **cfg_overrides) -> TwilioProvider:
    p = TwilioProvider(_cfg(**cfg_overrides))
    p._client = SimpleNamespace(calls=fake_calls)
    return p


# ------------------------------------------------------------ status mapping


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("completed", Outcome.CONNECTED),
        ("busy", Outcome.BUSY),
        ("no-answer", Outcome.NO_ANSWER),
        ("failed", Outcome.FAILED),
        ("canceled", Outcome.CANCELED),
    ],
)
def test_terminal_status_mapping(status, outcome):
    update = parse_status_callback(
        {"CallSid": "CA1", "CallStatus": status, "CallDuration": "63"}
    )
    assert update.outcome is outcome
    assert update.provider_call_id == "CA1"
    assert update.duration_sec == 63


@pytest.mark.parametrize("status", ["queued", "initiated", "ringing", "in-progress"])
def test_non_terminal_statuses_never_produce_an_outcome(status):
    update = parse_status_callback({"CallSid": "CA1", "CallStatus": status})
    assert update.outcome is None
    assert update.call_status == status


@pytest.mark.parametrize(
    "answered_by",
    ["machine_start", "machine_end_beep", "machine_end_silence", "fax"],
)
def test_amd_machine_answer_becomes_voicemail(answered_by):
    update = parse_status_callback(
        {"CallSid": "CA1", "CallStatus": "completed", "AnsweredBy": answered_by}
    )
    assert update.outcome is Outcome.VOICEMAIL
    assert update.answered_by == answered_by


def test_amd_human_answer_stays_connected():
    update = parse_status_callback(
        {"CallSid": "CA1", "CallStatus": "completed", "AnsweredBy": "human"}
    )
    assert update.outcome is Outcome.CONNECTED


def test_status_callback_without_callsid_is_rejected():
    with pytest.raises(ValueError):
        parse_status_callback({"CallStatus": "completed"})


def test_bad_duration_does_not_crash():
    update = parse_status_callback(
        {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "abc"}
    )
    assert update.duration_sec is None


# -------------------------------------------------------- signature checking


def test_signature_fails_closed_without_auth_token():
    cfg = make_settings()  # no twilio_auth_token
    assert validate_twilio_signature(cfg, "https://x/webhook", {}, "sig") is False


def test_signature_roundtrip_and_tamper():
    cfg = _cfg()
    url = "https://dialer.example.com/webhooks/twilio/status"
    params = {"CallSid": "CA1", "CallStatus": "completed"}
    good = RequestValidator(AUTH_TOKEN).compute_signature(url, params)
    assert validate_twilio_signature(cfg, url, params, good) is True
    assert validate_twilio_signature(cfg, url, params, good + "x") is False
    assert validate_twilio_signature(cfg, url, params, "") is False


# -------------------------------------------------------------- construction


def test_provider_requires_credentials():
    with pytest.raises(ConfigError):
        TwilioProvider(make_settings())
    with pytest.raises(ConfigError):
        TwilioProvider(make_settings(twilio_account_sid="AC1"))


def test_registry_rejects_unknown_provider():
    from orchestrator.voice import get_voice_provider

    with pytest.raises(ConfigError):
        get_voice_provider(make_settings(voice_provider="carrier-pigeon"))


# ----------------------------------------------------------------- start_call


def test_start_call_kwargs_and_sid():
    fake = FakeCalls()
    p = _provider(fake, max_call_minutes=10)
    sid = p.start_call(
        to_number="+16145550100",
        from_number="+16145550999",
        variables={"company_name": "Acme", "touch_type": "first",
                   "opener_variant": "opener_a", "two_party_disclose": "false"},
        attempt_id=7,
    )
    assert sid == "CA123"
    kw = fake.create_kwargs
    assert kw["to"] == "+16145550100"
    assert kw["from_"] == "+16145550999"
    assert kw["status_callback"] == "https://dialer.example.com/webhooks/twilio/status"
    assert kw["time_limit"] == 600
    assert "machine_detection" not in kw  # amd_enabled defaults off
    assert "<ConversationRelay" in kw["twiml"]
    assert 'name="attempt_id" value="7"' in kw["twiml"]


def test_start_call_enables_amd_when_configured():
    fake = FakeCalls()
    p = _provider(fake, amd_enabled=True)
    p.start_call(
        to_number="+16145550100", from_number="+16145550999",
        variables={"opener_variant": "opener_a"}, attempt_id=1,
    )
    assert fake.create_kwargs["machine_detection"] == "Enable"


def test_start_call_failure_is_wrapped_and_never_leaks_the_token():
    fake = FakeCalls(create_error=RuntimeError(f"boom auth={AUTH_TOKEN}"))
    p = _provider(fake)
    with pytest.raises(TwilioVoiceError) as excinfo:
        p.start_call(
            to_number="+16145550100", from_number="+16145550999",
            variables={"opener_variant": "opener_a"}, attempt_id=1,
        )
    assert AUTH_TOKEN not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)


# ------------------------------------------------------- fetch details / cost


def test_fetch_call_details_maps_status():
    fake = FakeCalls(FakeCallResource(status="completed", duration="63",
                                      answered_by=None))
    update = _provider(fake).fetch_call_details("CA9")
    assert update.outcome is Outcome.CONNECTED
    assert update.duration_sec == 63


def test_fetch_call_details_404_returns_none():
    fake = FakeCalls(FakeCallResource(
        fetch_error=TwilioRestException(404, "https://api.twilio.com/x")))
    assert _provider(fake).fetch_call_details("CA9") is None


def test_fetch_call_cost_absolute_value():
    fake = FakeCalls(FakeCallResource(price="-0.014"))
    assert _provider(fake).fetch_call_cost_usd("CA9") == pytest.approx(0.014)


def test_fetch_call_cost_unknown_yet():
    fake = FakeCalls(FakeCallResource(price=None))
    assert _provider(fake).fetch_call_cost_usd("CA9") is None


# ------------------------------------------------- number pool provisioning


class FakeIncomingNumbers:
    def __init__(self, owned=(), fail_first_create=False):
        self.owned = set(owned)
        self.fail_first_create = fail_first_create
        self.created: list[str] = []

    def create(self, phone_number, friendly_name=None):
        if self.fail_first_create and not self.created:
            self.created.append("FAILED")
            raise RuntimeError("number just got taken")
        self.created.append(phone_number)
        return SimpleNamespace(sid=f"PN{len(self.created)}")

    def list(self, phone_number=None, limit=1):
        if phone_number in self.owned:
            return [SimpleNamespace(sid="PN-owned")]
        return []


class FakeTwilioClient:
    def __init__(self, available=(), **incoming_kwargs):
        self._available = list(available)
        self.incoming_phone_numbers = FakeIncomingNumbers(**incoming_kwargs)

    def available_phone_numbers(self, country):
        assert country == "US"
        candidates = [SimpleNamespace(phone_number=n) for n in self._available]
        return SimpleNamespace(
            local=SimpleNamespace(list=lambda **kw: candidates[: kw.get("limit")])
        )


@pytest.fixture
def numbers_env(monkeypatch):
    from orchestrator import db as db_mod
    from orchestrator.voice.twilio import numbers as numbers_mod

    registered: list[tuple] = []

    def fake_execute(cfg, sql, params=None):
        registered.append(params)
        return 1

    monkeypatch.setattr(db_mod, "execute", fake_execute)
    return numbers_mod, registered, monkeypatch


def test_purchase_numbers_buys_and_registers(numbers_env):
    numbers_mod, registered, monkeypatch = numbers_env
    client = FakeTwilioClient(available=["+16145550001", "+16145550002", "+16145550003"])
    monkeypatch.setattr(numbers_mod, "_rest_client", lambda cfg: client)
    bought = numbers_mod.purchase_numbers(_cfg(), [{"area_code": "614", "count": 2}])
    assert bought == ["+16145550001", "+16145550002"]
    assert [p[0] for p in registered] == bought
    assert all(p[1] == "614" for p in registered)


def test_purchase_numbers_skips_failed_candidate(numbers_env):
    numbers_mod, registered, monkeypatch = numbers_env
    client = FakeTwilioClient(
        available=["+16145550001", "+16145550002"], fail_first_create=True
    )
    monkeypatch.setattr(numbers_mod, "_rest_client", lambda cfg: client)
    bought = numbers_mod.purchase_numbers(_cfg(), [{"area_code": "614", "count": 1}])
    assert bought == ["+16145550002"]  # first candidate lost, next one taken


def test_purchase_numbers_rejects_bad_area_code(numbers_env):
    numbers_mod, _, monkeypatch = numbers_env
    monkeypatch.setattr(numbers_mod, "_rest_client", lambda cfg: FakeTwilioClient())
    with pytest.raises(ConfigError):
        numbers_mod.purchase_numbers(_cfg(), [{"area_code": "61", "count": 1}])


def test_import_number_requires_ownership(numbers_env):
    numbers_mod, registered, monkeypatch = numbers_env
    client = FakeTwilioClient(owned={"+16145550100"})
    monkeypatch.setattr(numbers_mod, "_rest_client", lambda cfg: client)
    numbers_mod.import_number(_cfg(), "+16145550100")
    assert registered[0][0] == "+16145550100"
    assert registered[0][1] == "614"
    with pytest.raises(TwilioVoiceError):
        numbers_mod.import_number(_cfg(), "+16145559999")


def test_area_code_derivation():
    from orchestrator.voice.twilio.numbers import _area_code_of

    assert _area_code_of("+16145550100") == "614"
    assert _area_code_of("6145550100") == "614"
    with pytest.raises(ConfigError):
        _area_code_of("+4420712345678")
