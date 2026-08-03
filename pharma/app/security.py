"""Auth layer. Fail-closed everywhere (the dealdesk discipline):
missing PHARMA_SECRET_KEY -> 503 for cookie sessions; unknown token -> 401
with no detail; wrong role -> 403. Identity resolves to a (key_id, role,
tenant_id) triple; tenant scoping is enforced by construction downstream —
routes never accept a tenant id from the request.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app import config
from app.db import control


@dataclass(frozen=True)
class Identity:
    key_id: str
    role: str  # client | trainer | admin
    tenant_id: str | None  # set for client keys only


def _serializer() -> URLSafeTimedSerializer:
    if not config.SECRET_KEY:
        raise HTTPException(status_code=503, detail="server not configured")
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="pharma-session")


def make_session_cookie(raw_key: str) -> str:
    """The cookie stores the raw key, signed. We deliberately do not mint a
    separate session id: resolving through api_keys on every request means a
    revoked key or suspended tenant locks out immediately."""
    return _serializer().dumps({"k": raw_key})


def _key_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    cookie = request.cookies.get(config.SESSION_COOKIE)
    if cookie:
        try:
            data = _serializer().loads(cookie, max_age=config.SESSION_MAX_AGE)
            return data.get("k")
        except BadSignature:
            return None
    return None


def resolve_identity(request: Request) -> Identity:
    raw_key = _key_from_request(request)
    if raw_key is None:
        raise HTTPException(status_code=401)
    row = control.resolve_key(raw_key)
    if row is None:
        raise HTTPException(status_code=401)
    return Identity(key_id=row["key_id"], role=row["role"], tenant_id=row["tenant_id"])


def require_client(identity: Identity = Depends(resolve_identity)) -> Identity:
    if identity.role != "client" or not identity.tenant_id:
        raise HTTPException(status_code=403)
    return identity


def require_trainer(identity: Identity = Depends(resolve_identity)) -> Identity:
    if identity.role not in ("trainer", "admin"):
        raise HTTPException(status_code=403)
    return identity


def require_admin(identity: Identity = Depends(resolve_identity)) -> Identity:
    if identity.role != "admin":
        raise HTTPException(status_code=403)
    return identity
