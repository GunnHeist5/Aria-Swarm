"""tools/integrations/secrets.py — the single secure secret chokepoint.

All credentials are read from environment variables ONLY (populated from the VPS
`.env`/vault), through this one module. It declares the env-var *names* each
integration needs and reports readiness — without ever printing a value.

Design rules:
  * Never log, print, or return a secret value except to the caller that asked.
  * The readiness report shows names + a masked length hint, never the value.
  * Missing required secrets fail closed (`SecretError`), never a silent empty.

Populate these on the VPS after rotating your credentials. Nothing here needs a
real value to be imported or tested.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Required env-var NAMES per integration (values live in the VPS env, not here)
# ---------------------------------------------------------------------------

REQUIRED: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    # Model tiers (cost discipline): cheap baseline / smart / approval-gated.
    "models": ["CLAUDE_MODEL", "CLAUDE_CHEAP_MODEL", "CLAUDE_APPROVAL_MODEL"],
    "google_sheets": ["GOOGLE_SHEETS_SPREADSHEET_ID", "GOOGLE_SA_JSON"],
    "gmail": [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_PORT", "SENDER_NAME", "SENDER_EMAIL",
    ],
    "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"],
    "instantly": ["INSTANTLY_API_KEY", "INSTANTLY_CAMPAIGN_ID"],
    "liquid": ["LIQUID_API_KEY"],
    "propstream": ["PROPSTREAM_API_KEY"],
    "pandadoc": [
        "PANDADOC_API_KEY",
        "PANDADOC_PURCHASE_TEMPLATE_ID",
        "PANDADOC_ASSIGNMENT_TEMPLATE_ID",
    ],
    "relayfi": ["RELAYFI_API_BASE", "RELAYFI_API_KEY", "RELAYFI_API_SECRET"],
    "notion": ["NOTION_API_KEY", "NOTION_HQ_PAGE_ID"],
    "cloudflare": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
    "composio": ["COMPOSIO_API_KEY"],
    "telegram": ["MUFFIN_TELEGRAM_TOKEN", "JUSTIN_TELEGRAM_CHAT_ID"],
}

# Sensible non-secret defaults (overridable via env). These are NOT secrets.
DEFAULTS: dict[str, str] = {
    "CLAUDE_CHEAP_MODEL": "claude-haiku-4-5",
    "CLAUDE_APPROVAL_MODEL": "claude-opus-4-6",
    "SMTP_PORT": "587",
    "IMAP_PORT": "993",
    "RELAYFI_API_BASE": "https://api.relayfi.com",
    # Campaign ids are not secrets; the Harris County land campaign.
    "INSTANTLY_CAMPAIGN_ID": "a49a690b-ff8a-414e-84e1-ee84558469e7",
    # Liquid AI endpoint (not a secret; confirm the exact host on the VPS).
    "LIQUID_BASE_URL": "https://api.liquid.ai/v1",
    "LIQUID_MODEL_SLUG": "lfm-7b",
}


class SecretError(RuntimeError):
    """A required secret was requested but is not present in the environment."""


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def get_secret(name: str, *, required: bool = False) -> str | None:
    """Return the env value for ``name`` (or its non-secret default), or None.

    Fail-closed: ``required=True`` and absent → ``SecretError`` (never a silent
    empty string that would send an unauthenticated request). The value is
    returned only to the caller; it is never logged here.
    """

    value = os.environ.get(name) or DEFAULTS.get(name)
    if value:
        return value
    if required:
        raise SecretError(
            f"missing required secret {name!r} — set it in the VPS .env/vault"
        )
    return None


# ---------------------------------------------------------------------------
# Readiness (names + masked hints only — never values)
# ---------------------------------------------------------------------------


def _mask(value: str) -> str:
    """A non-revealing presence hint: length + last 4 chars only."""

    if len(value) <= 4:
        return f"set (len {len(value)})"
    return f"set (••••{value[-4:]}, len {len(value)})"


def integration_status() -> dict[str, dict]:
    """Per integration: ``{ready, present[names], missing[names]}`` — no values."""

    status: dict[str, dict] = {}
    for integration, names in REQUIRED.items():
        present = [n for n in names if get_secret(n) is not None]
        missing = [n for n in names if get_secret(n) is None]
        status[integration] = {
            "ready": not missing,
            "present": present,
            "missing": missing,
        }
    return status


def print_readiness() -> None:
    """Print the .env checklist: which integrations are ready vs. what's missing.

    Shows only names and masked presence hints — never a secret value.
    """

    print("\n==== ARIA integration readiness (names only; no values shown) ====")
    for integration, info in integration_status().items():
        mark = "✅" if info["ready"] else "⛔"
        print(f"{mark} {integration}")
        for name in REQUIRED[integration]:
            val = get_secret(name)
            shown = _mask(val) if val else "MISSING — set in .env"
            print(f"     {name:<32} {shown}")
    print("=================================================================\n")


if __name__ == "__main__":
    print_readiness()
