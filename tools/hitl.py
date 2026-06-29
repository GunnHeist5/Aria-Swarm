"""tools/hitl.py — Human-in-the-Loop emergency notification.

When the swarm freezes (wallet spend-cap breach, a CRITICAL_GATE withdrawal/key
change, or a CAPTCHA/2FA/bank-verification wall), it must reach a human out of
band with enough context to decide, plus a secure remote-view handle to resume.

``dispatch_hitl_alert`` builds the breach payload and emits it. For now the
"webhook" is a logging/print stub that mimics a Discord/Slack/Telegram outbound
call and surfaces a distinct secure tracking string — wiring a real HTTP webhook
is a drop-in replacement for ``_emit_webhook``.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from state import BusinessState

logger = logging.getLogger(__name__)

# Base for the secure remote-view tracking URL surfaced to the human operator.
REMOTE_VIEW_BASE = "https://swarm-hitl.local/view"


def _classify_breach(hitl: dict) -> str:
    """Human-readable breach class derived from the frozen HITL state."""

    reason = hitl.get("hitl_reason")
    if hitl.get("pending_critical_gate_tool"):
        return "Critical withdrawal / security-key change requested"
    mapping = {
        "wallet_spend_cap": "Wallet spend cap exceeded",
        "captcha": "CAPTCHA challenge encountered",
        "2fa": "Two-factor authentication required",
        "bank_verification": "Bank verification screen encountered",
    }
    if reason in mapping:
        return mapping[reason]
    return "Autonomous execution halted — manual authorization required"


def dispatch_hitl_alert(state: BusinessState) -> dict:
    """Build and emit the HITL breach alert; return the payload.

    Generates a cryptographically-random tracking id and remote-view URL, writes
    the URL back into ``state['hitl']['hitl_webhook_url']``, fires the (stubbed)
    outbound webhook, and returns the payload so callers/tests can inspect it.
    """

    hitl = state["hitl"]
    tracking_id = secrets.token_urlsafe(16)
    remote_view_url = f"{REMOTE_VIEW_BASE}/{tracking_id}"
    hitl["hitl_webhook_url"] = remote_view_url

    payload = {
        "alert": "SWARM_HITL_FREEZE",
        "swarm_id": state["evolution"]["swarm_id"],
        "cycle": state["cycle_count"],
        "breach_type": _classify_breach(hitl),
        "reason": hitl.get("hitl_reason"),
        "requires_auth": hitl.get("requires_auth", False),
        "hitl_pending": hitl.get("hitl_pending", False),
        "critical_tool": hitl.get("pending_critical_gate_tool"),
        "tracking_id": tracking_id,
        "remote_view_url": remote_view_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    _emit_webhook(payload)
    return payload


def _emit_webhook(payload: dict) -> None:
    """Outbound notification stub (Discord/Slack/Telegram).

    Replace the body with a real HTTP POST to go live; the payload contract is
    already shaped for a chat webhook. Until then it logs at WARNING and prints a
    distinct banner so the freeze is impossible to miss on a terminal.
    """

    logger.warning("HITL FREEZE dispatched: %s", json.dumps(payload))

    banner = (
        "\n"
        "==================== 🚨 SWARM HITL FREEZE 🚨 ====================\n"
        f"  Swarm     : {payload['swarm_id']}  (cycle {payload['cycle']})\n"
        f"  Breach    : {payload['breach_type']}\n"
        f"  Reason    : {payload['reason']}\n"
        f"  Requires  : human authorization to resume\n"
        f"  SECURE REMOTE VIEW → {payload['remote_view_url']}\n"
        f"  Resume    : python resume.py <thread_id> approve|reject\n"
        "================================================================\n"
    )
    print(banner)
