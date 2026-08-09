"""Prompt assembly rules — the compliance-critical parts of agent/script.py.

Pins behavior, not prose: the AI disclosure must survive every assembly path,
placeholders must never leak into spoken text, and the A/B assignment must be
stable across processes.
"""

from __future__ import annotations

import pytest

from conftest import make_settings
from orchestrator.agent import script
from orchestrator.config import ConfigError
from orchestrator.models import TouchType

AI_DISCLOSURE = "AI assistant calling on behalf of Justin at Reachwell"


def test_build_system_prompt_contains_core_sections():
    cfg = make_settings()
    prompt = script.build_system_prompt(
        cfg,
        touch=TouchType.FIRST,
        company_name="Acme Plumbing",
        opener_variant="opener_a",
        two_party_disclose=False,
    )
    assert "outbound AI sales representative" in prompt  # ROLE
    assert "PERSONALITY" in prompt and "GUARDRAILS" in prompt and "CALL FLOW" in prompt
    assert "ASSIGNED OPENER (variant: opener_a)" in prompt
    assert AI_DISCLOSURE in prompt


def test_company_name_substitution_and_fallback():
    cfg = make_settings()
    with_name = script.build_system_prompt(
        cfg, touch=TouchType.FIRST, company_name="Acme Plumbing",
        opener_variant="opener_a", two_party_disclose=False,
    )
    assert "Acme Plumbing" in with_name
    assert "{{CompanyName}}" not in with_name

    for empty in (None, "", "   "):
        fallback = script.build_system_prompt(
            cfg, touch=TouchType.FIRST, company_name=empty,
            opener_variant="opener_a", two_party_disclose=False,
        )
        assert "your business" in fallback
        assert "{{CompanyName}}" not in fallback


def test_second_touch_addendum_only_for_second_touch():
    cfg = make_settings()
    first = script.build_system_prompt(
        cfg, touch=TouchType.FIRST, company_name=None,
        opener_variant="opener_a", two_party_disclose=False,
    )
    second = script.build_system_prompt(
        cfg, touch=TouchType.SECOND, company_name=None,
        opener_variant="opener_a", two_party_disclose=False,
    )
    assert "SECOND-TOUCH" not in first
    assert "SECOND-TOUCH" in second
    assert "previously contacted" in second


def test_recording_disclosure_only_when_two_party():
    cfg = make_settings()
    plain = script.build_system_prompt(
        cfg, touch=TouchType.FIRST, company_name=None,
        opener_variant="opener_a", two_party_disclose=False,
    )
    disclosed = script.build_system_prompt(
        cfg, touch=TouchType.FIRST, company_name=None,
        opener_variant="opener_a", two_party_disclose=True,
    )
    assert "RECORDING DISCLOSURE" not in plain
    assert "RECORDING DISCLOSURE" in disclosed
    assert script.RECORDING_DISCLOSURE_LINE in disclosed


def test_maintainer_comments_never_reach_the_model():
    cfg = make_settings()
    prompt = script.build_system_prompt(
        cfg, touch=TouchType.SECOND, company_name="Acme",
        opener_variant="opener_b", two_party_disclose=True,
    )
    assert "<!--" not in prompt
    assert "TODO" not in prompt


def test_unknown_opener_variant_fails_closed():
    cfg = make_settings()
    with pytest.raises(ConfigError):
        script.build_system_prompt(
            cfg, touch=TouchType.FIRST, company_name=None,
            opener_variant="opener_zz", two_party_disclose=False,
        )
    with pytest.raises(ConfigError):
        script.opener_greeting(cfg, opener_variant="nope", company_name=None)


# ---------------------------------------------------------------------- opener


def test_every_configured_variant_carries_the_ai_disclosure():
    cfg = make_settings()
    for variant in cfg.opener_variants:
        greeting = script.opener_greeting(
            cfg, opener_variant=variant, company_name="Acme"
        )
        assert AI_DISCLOSURE in greeting, f"variant {variant} lost the disclosure"


def test_opener_greeting_is_a_single_spoken_line():
    cfg = make_settings()
    greeting = script.opener_greeting(
        cfg, opener_variant="opener_a", company_name="Acme Plumbing"
    )
    assert "\n" not in greeting
    assert "Acme Plumbing" in greeting
    assert "{{" not in greeting


def test_opener_greeting_appends_recording_disclosure():
    cfg = make_settings()
    plain = script.opener_greeting(
        cfg, opener_variant="opener_a", company_name=None
    )
    disclosed = script.opener_greeting(
        cfg, opener_variant="opener_a", company_name=None, two_party_disclose=True
    )
    assert script.RECORDING_DISCLOSURE_LINE not in plain
    assert disclosed.endswith(script.RECORDING_DISCLOSURE_LINE)


# ------------------------------------------------------------- A/B assignment


def test_pick_opener_variant_is_stable_and_valid():
    cfg = make_settings()
    for contact_id in (1, 42, 999_999):
        first = script.pick_opener_variant(cfg, contact_id)
        assert first in cfg.opener_variants
        assert script.pick_opener_variant(cfg, contact_id) == first


def test_pick_opener_variant_uses_every_variant():
    cfg = make_settings()
    seen = {script.pick_opener_variant(cfg, cid) for cid in range(40)}
    assert seen == set(cfg.opener_variants)


def test_pick_opener_variant_empty_config_fails_closed():
    cfg = make_settings(opener_variants=[])
    with pytest.raises(ConfigError):
        script.pick_opener_variant(cfg, 1)
