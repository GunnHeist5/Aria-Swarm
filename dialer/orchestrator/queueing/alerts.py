"""Operator alerts: POST to a webhook (Slack/Discord-style) + always a log.

The log line is the guaranteed channel — the webhook is best-effort and its
failure must never propagate into the caller (a bench, a kill switch, an
orphaned attempt). Alert text is written by us, never raw payloads, so the
redaction filter plus this discipline keeps secrets and PII out of channels.
"""

from __future__ import annotations

import httpx

from ..config import Settings
from ..logging_utils import get_logger

log = get_logger(__name__)

_TIMEOUT_SECONDS = 10.0


def _make_client() -> httpx.Client:
    # Separated so tests can swap in an httpx.MockTransport-backed client.
    return httpx.Client(timeout=_TIMEOUT_SECONDS)


def send_alert(cfg: Settings, text: str) -> None:
    """Log the alert, then best-effort POST {"text": ...} to the webhook."""
    log.warning("ALERT: %s", text)
    url = cfg.alert_webhook_url
    if not url:
        return
    try:
        with _make_client() as client:
            client.post(url, json={"text": text}).raise_for_status()
    except Exception:
        log.exception("alert webhook delivery failed (alert already logged)")
