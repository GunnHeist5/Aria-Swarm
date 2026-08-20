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

# Set PHARMA_COOKIE_SECURE=1 once the app is served over HTTPS (e.g. behind the
# Cloudflare tunnel) so session cookies are never sent over plain HTTP.
COOKIE_SECURE = os.environ.get("PHARMA_COOKIE_SECURE", "") == "1"

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

# A 'running' analysis with no activity for this long is reported as stale
# (the app restarted mid-run). Read-only judgment — nothing mutates on a poll.
STALE_AFTER_S = 600

# ask_user clarifying questions: how long the worker waits for a click, and
# how often it checks. Module-level so tests can monkeypatch them.
ASK_TIMEOUT_S = 600
ASK_POLL_S = 1.0

# Streaming-answer flush throttle: write the partial at most this often
# (whichever of the two trips first).
PARTIAL_FLUSH_S = 0.7
PARTIAL_FLUSH_CHARS = 300

MAX_CHAT_FILES = 5
DOC_CHUNK_CHARS = 8000


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
