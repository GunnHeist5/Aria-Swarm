"""FastAPI surface: JustCall + Twilio webhooks and the ConversationRelay socket.

Every inbound webhook is verified before anything else runs, and verification
fails closed: no configured secret means no accepted events. Handlers are
idempotent by construction (webhook_events dedupe) because both providers
re-deliver and neither guarantees ordering.

Sibling packages (justcall/voice/queueing) are imported lazily inside
handlers per the module contract — this module must import (and its routes be
testable with monkeypatched seams) without the full tree resolvable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, WebSocket

from . import db
from .config import Settings, load_settings
from .logging_utils import get_logger, setup_logging
from .models import CallCosts, CallResult

log = get_logger(__name__)

_ATTEMPT_BY_SID_SQL = "SELECT * FROM call_attempts WHERE provider_call_id = %(sid)s"

_STATUS_DEDUPE_SQL = """
INSERT INTO webhook_events (source, external_id, event_type, payload, processed_at)
VALUES ('twilio', %(sid)s, %(event)s, %(payload)s, now())
ON CONFLICT (source, external_id, event_type) DO NOTHING
"""


def _twilio_callback_url(cfg: Settings, request: Request) -> str:
    """The URL Twilio signed: the public one, not what the proxy saw."""
    if cfg.public_base_url:
        return cfg.public_base_url.rstrip("/") + request.url.path
    return str(request.url)


def _finalize_from_status(cfg: Settings, form: dict) -> str:
    """Sync body of the Twilio status route (runs in a thread)."""
    from .queueing import completion  # lazy: sibling package
    from .voice.twilio import provider as twilio_provider

    update = twilio_provider.parse_status_callback(form)
    if update.outcome is None:
        return "ignored"  # non-terminal status (initiated/ringing/answered)

    deduped = db.execute(
        cfg,
        _STATUS_DEDUPE_SQL,
        {
            "sid": update.provider_call_id,
            "event": f"status:{update.call_status}",
            "payload": db.Jsonb({k: str(v) for k, v in form.items()}),
        },
    )
    if deduped == 0:
        return "duplicate"

    row = db.query_one(cfg, _ATTEMPT_BY_SID_SQL, {"sid": update.provider_call_id})
    if row is None:
        log.warning("status callback for unknown call %s", update.provider_call_id)
        return "unknown"

    # Voice cost straight from the provider when already priced; the estimator
    # falls back to duration × rate when it isn't.
    voice_usd: float | None = None
    try:
        from .voice import get_voice_provider

        voice_usd = get_voice_provider(cfg).fetch_call_cost_usd(update.provider_call_id)
    except Exception:
        log.exception("voice cost lookup failed for %s — estimating", update.provider_call_id)

    completion.finalize_attempt(
        cfg,
        row,
        CallResult(
            provider_call_id=update.provider_call_id,
            outcome=update.outcome,
            duration_sec=update.duration_sec,
            recording_url=update.recording_url,
            # The relay socket already persisted transcript/disposition/LLM
            # cost onto the row; carry the stored LLM figure into the total.
            costs=CallCosts(
                voice_usd=voice_usd or 0.0,
                llm_usd=float(row.get("cost_llm_usd") or 0.0),
            ),
        ),
    )
    return "finalized"


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or load_settings()
    setup_logging(cfg)
    app = FastAPI(title="Reachwell Orchestrator", docs_url=None, redoc_url=None)

    @app.on_event("startup")
    async def _startup() -> None:
        await asyncio.to_thread(db.migrate, cfg)

    @app.get("/healthz")
    async def healthz() -> Response:
        def _check() -> dict:
            state = {"db": False, "redis": False}
            try:
                db.query_one(cfg, "SELECT 1 AS ok")
                state["db"] = True
            except Exception:
                log.exception("healthz: db check failed")
            try:
                import redis as redis_lib

                redis_lib.Redis.from_url(
                    cfg.redis_url, socket_connect_timeout=2
                ).ping()
                state["redis"] = True
            except Exception:
                log.exception("healthz: redis check failed")
            return state

        state = await asyncio.to_thread(_check)
        ok = all(state.values())
        return Response(
            content=json.dumps({"ok": ok, **state, "phase": cfg.phase}),
            media_type="application/json",
            status_code=200 if ok else 503,
        )

    @app.post("/webhooks/justcall/call-completed")
    async def justcall_call_completed(request: Request) -> Response:
        from .justcall import webhook as justcall_webhook  # lazy: sibling

        raw = await request.body()
        headers = dict(request.headers)
        if not justcall_webhook.verify_signature(
            raw, headers, cfg.justcall_webhook_secret or ""
        ):
            log.warning("rejected unsigned/missigned JustCall webhook")
            return Response(status_code=401)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Response(status_code=400)
        result = await asyncio.to_thread(
            justcall_webhook.handle_call_completed, cfg, payload
        )
        return Response(
            content=json.dumps({"result": result}), media_type="application/json"
        )

    @app.post("/webhooks/twilio/status")
    async def twilio_status(request: Request) -> Response:
        from .voice.twilio import provider as twilio_provider  # lazy: sibling

        form = dict(await request.form())
        signature = request.headers.get("x-twilio-signature", "")
        url = _twilio_callback_url(cfg, request)
        if not twilio_provider.validate_twilio_signature(cfg, url, form, signature):
            log.warning("rejected unsigned/missigned Twilio status callback")
            return Response(status_code=403)
        try:
            result = await asyncio.to_thread(_finalize_from_status, cfg, form)
        except ValueError:
            return Response(status_code=400)
        return Response(
            content=json.dumps({"result": result}), media_type="application/json"
        )

    @app.websocket("/ws/relay")
    async def relay(websocket: WebSocket) -> None:
        from .voice.twilio import relay as twilio_relay  # lazy: sibling

        await twilio_relay.handle_relay_socket(websocket, cfg)

    app.state.cfg = cfg
    app.state.started_at = datetime.now(timezone.utc)
    return app
