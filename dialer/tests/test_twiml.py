"""TwiML rendering: <Connect><ConversationRelay> shape, parameters, greeting."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from conftest import make_settings
from orchestrator.config import ConfigError
from orchestrator.voice.twilio import twiml

BASE_VARS = {
    "attempt_id": "7",
    "company_name": "Acme Plumbing",
    "touch_type": "first",
    "opener_variant": "opener_a",
    "two_party_disclose": "false",
}


def _cfg(**overrides):
    return make_settings(public_base_url="https://dialer.example.com", **overrides)


def _relay_element(xml_text: str) -> ET.Element:
    root = ET.fromstring(xml_text)  # also proves the document is valid XML
    assert root.tag == "Response"
    connect = root.find("Connect")
    assert connect is not None, "missing <Connect>"
    relay = connect.find("ConversationRelay")
    assert relay is not None, "missing <ConversationRelay>"
    return relay


def test_relay_twiml_shape_and_url():
    relay = _relay_element(twiml.build_relay_twiml(_cfg(), variables=BASE_VARS))
    assert relay.get("url") == "wss://dialer.example.com/ws/relay"


def test_websocket_url_strips_scheme_and_path():
    cfg = make_settings(public_base_url="http://dialer.example.com:8080/base")
    assert twiml.relay_websocket_url(cfg) == "wss://dialer.example.com:8080/ws/relay"


def test_welcome_greeting_carries_opener_and_ai_disclosure():
    relay = _relay_element(twiml.build_relay_twiml(_cfg(), variables=BASE_VARS))
    greeting = relay.get("welcomeGreeting")
    assert greeting
    assert "AI assistant calling on behalf of Justin at Reachwell" in greeting
    assert "Acme Plumbing" in greeting
    assert "recorded" not in greeting  # two_party_disclose is false here


def test_two_party_disclose_adds_recording_line_to_greeting():
    variables = dict(BASE_VARS, two_party_disclose="true")
    relay = _relay_element(twiml.build_relay_twiml(_cfg(), variables=variables))
    assert "recorded" in relay.get("welcomeGreeting")


def test_all_contract_parameters_present_plus_extensible_extras():
    variables = dict(BASE_VARS, custom_extra="x1")  # payload is extensible
    relay = _relay_element(twiml.build_relay_twiml(_cfg(), variables=variables))
    params = {p.get("name"): p.get("value") for p in relay.findall("Parameter")}
    for name in twiml.KNOWN_VARIABLES:
        assert name in params, f"missing <Parameter {name}>"
    assert params["attempt_id"] == "7"
    assert params["opener_variant"] == "opener_a"
    assert params["custom_extra"] == "x1"


def test_missing_public_base_url_fails_closed():
    cfg = make_settings()  # no public_base_url
    with pytest.raises(ConfigError):
        twiml.build_relay_twiml(cfg, variables=BASE_VARS)


def test_hostless_public_base_url_fails_closed():
    cfg = make_settings(public_base_url="not-a-url")
    with pytest.raises(ConfigError):
        twiml.relay_websocket_url(cfg)


def test_missing_opener_variant_fails_closed():
    variables = {k: v for k, v in BASE_VARS.items() if k != "opener_variant"}
    with pytest.raises(ConfigError):
        twiml.build_relay_twiml(_cfg(), variables=variables)


def test_unknown_opener_variant_fails_closed():
    variables = dict(BASE_VARS, opener_variant="opener_zz")
    with pytest.raises(ConfigError):
        twiml.build_relay_twiml(_cfg(), variables=variables)
