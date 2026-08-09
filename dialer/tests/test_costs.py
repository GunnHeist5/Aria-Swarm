"""Cost model: rates file loading and per-call estimation arithmetic."""

from __future__ import annotations

import json

from conftest import make_settings

from orchestrator.report import costs


def test_defaults_fill_missing_keys(tmp_path):
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({"voice_per_min": 0.02}))
    cfg = make_settings(rates_path=path)
    rates = costs.load_rates(cfg)
    assert rates["voice_per_min"] == 0.02
    assert rates["relay_per_min"] == 0.07  # default filled in


def test_estimate_bills_per_started_minute(tmp_path):
    path = tmp_path / "rates.json"
    path.write_text(json.dumps({}))
    cfg = make_settings(rates_path=path)
    c = costs.estimate_costs(cfg, duration_sec=61, provider_voice_usd=None, llm_usd=0.003)
    # 61s => 2 billed minutes
    assert c.voice_usd == round(2 * 0.014, 5)
    assert c.relay_usd == round(2 * 0.07, 5)
    assert c.intelligence_usd == round(2 * 0.025, 5)
    assert c.llm_usd == 0.003
    assert c.total_usd == round(c.voice_usd + c.relay_usd + c.intelligence_usd + c.llm_usd, 5)


def test_provider_billed_voice_wins_and_abs(tmp_path):
    path = tmp_path / "rates.json"
    path.write_text("{}")
    cfg = make_settings(rates_path=path)
    # Twilio reports prices as negative amounts; we store the magnitude.
    c = costs.estimate_costs(cfg, duration_sec=120, provider_voice_usd=-0.028, llm_usd=None)
    assert c.voice_usd == 0.028


def test_zero_duration_is_free(tmp_path):
    path = tmp_path / "rates.json"
    path.write_text("{}")
    cfg = make_settings(rates_path=path)
    c = costs.estimate_costs(cfg, duration_sec=None, provider_voice_usd=None, llm_usd=None)
    assert c.total_usd == 0.0
