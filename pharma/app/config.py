"""Environment-driven settings. Fail-closed: anything security-relevant that is
missing disables the feature that needs it rather than falling back."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Data root. Everything mutable lives under here: control.db, knowledge.db,
# and one directory per tenant. Never inside the package tree in production.
DATA_DIR = Path(os.environ.get("PHARMA_DATA_DIR", Path(__file__).resolve().parent.parent / "var"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PHARMA_MODEL = os.environ.get("PHARMA_MODEL", "claude-opus-5")

# Session-cookie signing secret. No default: sessions are refused without it.
SECRET_KEY = os.environ.get("PHARMA_SECRET_KEY", "")

SESSION_COOKIE = "pharma_session"
SESSION_MAX_AGE = 12 * 3600  # seconds

# Subscription tiers: analyses per month. Soft-blocked at the limit.
TIERS = {
    "starter": {"price_usd": 2000, "analyses_per_month": 20},
    "growth": {"price_usd": 5000, "analyses_per_month": 75},
    "enterprise": {"price_usd": None, "analyses_per_month": 1000},
}

# Sandbox limits for agent-written analysis code.
SANDBOX_WALL_TIMEOUT_S = 60
SANDBOX_CPU_S = 30
SANDBOX_RSS_BYTES = 1024**3  # 1 GiB

MAX_UPLOAD_BYTES = 50 * 1024**2

# Max problems a single web-triggered gym run may attempt; full batches go
# through the CLI, which takes an explicit --limit.
GYM_WEB_RUN_LIMIT = 5


def control_db_path() -> Path:
    return DATA_DIR / "control.db"


def knowledge_db_path() -> Path:
    return DATA_DIR / "knowledge.db"


def tenants_root() -> Path:
    return DATA_DIR / "tenants"


def gym_db_path() -> Path:
    return DATA_DIR / "gym.db"


def gym_files_root() -> Path:
    return DATA_DIR / "gym_files"
