"""Central configuration — every knob from env/.env, nothing hardcoded.

One `Settings` object is built at process start (`load_settings()`) and passed
down explicitly; modules must not read `os.environ` themselves. Secrets are
plain fields here but MUST never be logged — logging_utils installs a redaction
filter keyed off `Settings.secret_values()`.

Fail-closed philosophy: a missing secret or compliance input does not get a
convenient default. `require()` raises for subsystems that need a value, and
the compliance gate blocks dials when consent/DNC inputs are absent.
"""

from __future__ import annotations

import json
import re
from datetime import time, timedelta
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Paths in config values resolve relative to the dialer project root (the
# directory containing pyproject.toml), so the container workdir doesn't matter.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """A required setting is absent or invalid — refuse to run, never guess."""


def _csv(value: object) -> list[str]:
    """Accept JSON lists or comma-separated strings from env vars."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return [str(v).strip() for v in json.loads(text)]
        return [part.strip() for part in text.split(",") if part.strip()]
    raise ValueError(f"expected list or comma-separated string, got {value!r}")


_DURATION_RE = re.compile(r"^(\d+)\s*(m|h|d)$")


def parse_duration(text: str) -> timedelta:
    """Parse '30m' / '4h' / '3d' retry-schedule entries."""
    match = _DURATION_RE.match(text.strip().lower())
    if not match:
        raise ValueError(f"bad duration {text!r} (use e.g. 30m, 4h, 3d)")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- phase gate -------------------------------------------------------
    phase: Literal[1, 2, 3] = 1
    trust_hub_confirmed: bool = False
    phase2_max_contacts: int = 50
    phase2_max_concurrency: int = 2

    # --- JustCall ---------------------------------------------------------
    justcall_api_key: str | None = None
    justcall_api_secret: str | None = None
    justcall_campaign_id: int = 3310579
    justcall_webhook_secret: str | None = None
    justcall_company_field_key: str | None = None
    justcall_qualifying_dispositions: list[str] = ["Interested", "Callback", "No Answer"]

    # --- voice provider ---------------------------------------------------
    voice_provider: str = "twilio"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    public_base_url: str | None = None
    max_call_minutes: int = 10
    amd_enabled: bool = False

    # --- LLM (behind ConversationRelay) -----------------------------------
    llm_provider: str = "anthropic"
    llm_model: str = "claude-opus-5"
    llm_api_key: str | None = None
    anthropic_api_key: str | None = None  # conventional fallback for llm_api_key
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = 1024

    # --- storage ----------------------------------------------------------
    database_url: str = "postgresql://orchestrator:orchestrator@localhost:5432/orchestrator"
    redis_url: str = "redis://localhost:6379/0"

    # --- pacing -----------------------------------------------------------
    max_concurrent_calls: int = 5
    concurrency_hard_ceiling: int = 8
    calls_per_minute: int = 10

    # --- number pool ------------------------------------------------------
    per_number_daily_cap: int = 150
    number_cooldown_seconds: int = 120
    ramp_schedule: list[int] = [10, 25, 50, 100, 150]
    bench_relative_threshold: float = 0.5
    bench_min_dials: int = 30
    area_code_targets_path: Path = Path("config/area_code_targets.example.json")

    # --- compliance (hard blocks) -----------------------------------------
    call_window_start: time = time(9, 0)
    call_window_end: time = time(17, 0)
    state_windows_path: Path = Path("config/state_windows.json")
    two_party_states_path: Path = Path("config/two_party_states.json")
    dnc_list_path: Path | None = None
    consent_basis_first_touch: str | None = None
    consent_basis_second_touch: str | None = None
    max_attempts_total: int = 4
    retry_schedule: list[str] = ["1d", "3d", "7d"]
    followup_delay_hours: int = 20
    recording_enabled: bool = True
    recording_retention_days: int = 90
    transcript_retention_days: int = 365
    two_party_policy: Literal["disclose", "no_record"] = "disclose"
    allow_non_us_nanp: bool = False

    # --- costs ------------------------------------------------------------
    rates_path: Path = Path("config/rates.json")

    # --- misc -------------------------------------------------------------
    alert_webhook_url: str | None = None
    opener_variants: list[str] = ["opener_a", "opener_b"]
    port: int = 8080
    log_level: str = "INFO"

    @field_validator(
        "justcall_qualifying_dispositions", "retry_schedule", "opener_variants",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: object) -> list[str]:
        return _csv(value)

    @field_validator("ramp_schedule", mode="before")
    @classmethod
    def _parse_int_csv(cls, value: object) -> list[int]:
        if isinstance(value, list) and all(isinstance(v, int) for v in value):
            return value
        return [int(v) for v in _csv(value)]

    @field_validator("dnc_list_path", mode="before")
    @classmethod
    def _empty_path_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    # ------------------------------------------------------------------
    def resolve_path(self, p: Path) -> Path:
        """Config-file paths are project-root-relative unless absolute."""
        return p if p.is_absolute() else PROJECT_ROOT / p

    def require(self, *field_names: str) -> None:
        """Fail closed: raise ConfigError naming every missing field."""
        missing = [n for n in field_names if getattr(self, n) in (None, "")]
        if missing:
            raise ConfigError(
                "missing required settings: "
                + ", ".join(n.upper() for n in missing)
                + " — set them in the environment/.env"
            )

    def retry_delays(self) -> list[timedelta]:
        return [parse_duration(entry) for entry in self.retry_schedule]

    def effective_llm_api_key(self) -> str | None:
        return self.llm_api_key or self.anthropic_api_key

    def effective_concurrency(self) -> int:
        """Configured concurrency clamped by the hard ceiling and phase gates."""
        cap = min(self.max_concurrent_calls, self.concurrency_hard_ceiling)
        if self.phase == 2:
            cap = min(cap, self.phase2_max_concurrency)
        return max(1, cap)

    def secret_values(self) -> list[str]:
        """Every value the log redaction filter must scrub."""
        return [
            v
            for v in (
                self.justcall_api_key,
                self.justcall_api_secret,
                self.justcall_webhook_secret,
                self.twilio_auth_token,
                self.llm_api_key,
                self.anthropic_api_key,
            )
            if v
        ]


def load_settings(**overrides: object) -> Settings:
    """Build Settings from env/.env (overrides are for tests only)."""
    return Settings(**overrides)  # type: ignore[arg-type]
