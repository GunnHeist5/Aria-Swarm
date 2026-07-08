"""tools/integrations/instantly_webhook.py — receive Instantly reply webhooks.

Instantly is the *real* reply sensor: when a seller replies to a campaign,
Instantly fires a ``reply_received`` webhook whose payload already carries the
full reply text and the lead's email — no inbox scraping. This receiver
validates a shared secret and, for a genuine human reply, fires the swarm's
``seller_reply`` event (detached, idempotent, noise-filtered) — the exact same
qualifier brain the CLI/Muffin seam uses. Only the *sensor* changed; the
machinery is reused.

Run on the VPS (behind your Cloudflare tunnel, same pattern as the deal desk):

    INSTANTLY_WEBHOOK_SECRET=<a long random secret> \
      /root/Aria-Swarm/.venv/bin/uvicorn tools.integrations.instantly_webhook:app \
      --host 127.0.0.1 --port 8099

Then in Instantly → Settings → Webhooks → Add Webhook:
    URL:   https://<your-host>/instantly/reply?token=<INSTANTLY_WEBHOOK_SECRET>
    Event: reply_received

Security posture:
  * The secret is REQUIRED (fail-closed). A request without the matching token
    (query ``?token=`` or ``Authorization: Bearer``) is refused (401).
  * Only ``reply_received`` is acted on; other events are acked (200) and ignored
    so Instantly doesn't retry.
  * Firing is fire-and-forget, so Instantly gets a fast 200 (it retries on
    non-2xx). The swarm qualifier triages in the background.
"""

from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

from fastapi import FastAPI, Header, HTTPException, Request

from tools.muffin_bridge import looks_automated, notify_seller_reply

app = FastAPI(title="ARIA inbound receiver", version="1.1")

# Deal-approval flow (Telegram HITL + PandaDoc) shares this service + tunnel:
#   POST /deal-agreed  /telegram/callback  /pandadoc/webhook
try:
    from tools.dealflow.router import router as _dealflow_router

    app.include_router(_dealflow_router)
except Exception:  # never let a dealflow import break the reply webhook
    pass

REPLY_EVENT = "reply_received"


def _authorized(token: str | None, authorization: str) -> bool:
    """Shared-secret gate. Fail-closed when no secret is configured."""

    secret = os.environ.get("INSTANTLY_WEBHOOK_SECRET")
    if not secret:
        return False  # never run an unauthenticated public receiver
    if token and token == secret:
        return True
    return authorization == f"Bearer {secret}"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "configured": bool(os.environ.get("INSTANTLY_WEBHOOK_SECRET"))}


@app.get("/instantly/reply")
def instantly_reply_probe() -> dict:
    """Reachability probe — some webhook UIs validate the URL with a GET."""

    return {"ok": True, "probe": True}


@app.post("/instantly/reply")
async def instantly_reply(request: Request, token: str | None = None,
                          authorization: str = Header(default="")) -> dict:
    """Instantly reply_received webhook -> swarm seller_reply (detached).

    Validation pings (empty/non-JSON body, or any non-reply event) are acked with
    200 so Instantly can save the webhook — they don't fire anything. Auth (the
    shared secret) is required only to actually *fire* a reply, which is the only
    path that touches the swarm; a probe without the secret is inert.
    """

    try:
        body = await request.json()
    except Exception:
        body = {}  # validation ping / empty body -> ack below, never 400

    if not isinstance(body, dict):
        body = {}

    event = str(body.get("event_type") or "")
    if event != REPLY_EVENT:
        # Probe or non-reply event: ack so Instantly saves the webhook; no auth
        # needed because nothing is fired.
        return {"ok": True, "ignored": event or "no_event"}

    # Only a real reply fires the swarm -> the secret is required here.
    if not _authorized(token, authorization):
        raise HTTPException(status_code=401, detail="unauthorized")

    lead = str(body.get("lead_email") or "").strip()
    reply_text = (
        body.get("reply_text")
        or body.get("reply_text_snippet")
        or body.get("reply_subject")
        or ""
    ).strip()
    # Trim the quoted thread the same way the poller does — same text means the
    # same reply hash, so a reply seen by BOTH sensors dedups instead of
    # pushing twice.
    try:
        from .instantly_replies import trim_quoted

        reply_text = trim_quoted(reply_text) or reply_text
    except Exception:  # trimming must never break the receiver
        pass

    if not lead:
        return {"ok": True, "ignored": "no_lead_email"}
    if looks_automated(lead):
        return {"ok": True, "ignored": "automated_sender"}

    # Fire-and-forget: qualifier runs in the background, Instantly gets a fast 200.
    notify_seller_reply(
        lead, reply_text or "(empty reply)", lead,
        detach=True, campaign_id=body.get("campaign_id"),
    )
    return {"ok": True, "queued": lead}
