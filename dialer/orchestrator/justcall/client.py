"""JustCall Sales Dialer API v2.1 client (sync, rate-limited, retrying).

JustCall stays the contact database and rep tool while Twilio does the AI
calling (SPEC §1), so every read (campaign contacts) and write (DNCA moves,
disposition notes) funnels through this one client. It is synchronous by
design — async edges wrap calls in ``asyncio.to_thread`` — and fail-closed:
credentials are required at construction, never logged, and never echoed into
error messages. The token bucket and backoff exist because a paced dialer must
ride out JustCall's 429s without ever dropping a writeback.
"""

from __future__ import annotations

import time
from typing import Callable, Iterator

import httpx

from orchestrator.config import Settings
from orchestrator.logging_utils import get_logger

log = get_logger(__name__)

BASE_URL = "https://api.justcall.io/v2.1"
CONTACTS_PATH = "/sales_dialer/campaigns/contacts"

_MAX_RETRIES = 4                # retries after the first attempt → ≤5 requests total
_BASE_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 60.0
_TIMEOUT_SECONDS = 30.0
_RATE_PER_SECOND = 2.0          # ~2 req/s per the module contract


class JustCallError(RuntimeError):
    """A JustCall API call failed for good (4xx, or retries exhausted)."""


class _TokenBucket:
    """Client-side throttle. ``reserve()`` returns the seconds the caller must
    sleep before proceeding; returning the delay instead of sleeping internally
    keeps the bucket pure and clock-injectable for tests."""

    def __init__(
        self,
        rate_per_sec: float,
        capacity: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate_per_sec
        self._capacity = capacity
        self._clock = clock
        self._tokens = capacity
        self._last = clock()

    def reserve(self) -> float:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0
        wait = (1.0 - self._tokens) / self._rate
        # The caller sleeps `wait`; account for the token that accrues meanwhile.
        self._tokens = 0.0
        self._last = now + wait
        return wait


def _backoff_delay(attempt: int, retry_after: str | None) -> float:
    """Exponential backoff, overridden by a parseable Retry-After header."""
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_BACKOFF_SECONDS)
        except ValueError:
            pass  # HTTP-date form — just fall back to exponential
    return min(_BASE_BACKOFF_SECONDS * (2 ** attempt), _MAX_BACKOFF_SECONDS)


class JustCallClient:
    def __init__(self, cfg: Settings):
        cfg.require("justcall_api_key", "justcall_api_secret")
        self._cfg = cfg
        # Exact JustCall scheme: "key:secret" — no Bearer prefix.
        self._auth_header = f"{cfg.justcall_api_key}:{cfg.justcall_api_secret}"
        self._http = httpx.Client(base_url=BASE_URL, timeout=_TIMEOUT_SECONDS)
        self._bucket = _TokenBucket(_RATE_PER_SECOND, capacity=2.0)
        self._sleep: Callable[[float], None] = time.sleep

    # ------------------------------------------------------------------ http

    def _headers(self) -> dict[str, str]:
        # Built per-request (not on the httpx.Client) so tests can swap the
        # transport without losing auth, and so headers never sit in reprs.
        return {"Authorization": self._auth_header, "Accept": "application/json"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        """One API call with throttle + retry. Raises JustCallError terminally.

        Retryable: 429, 5xx, and transport errors. Other 4xx fail immediately.
        Error text carries only the status and a body snippet — never headers.
        """
        failure = "unknown error"
        for attempt in range(_MAX_RETRIES + 1):
            wait = self._bucket.reserve()
            if wait > 0:
                self._sleep(wait)
            retry_after: str | None = None
            try:
                response = self._http.request(
                    method, path, params=params, json=json_body, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                failure = f"network error: {type(exc).__name__}"
            else:
                if response.status_code < 400:
                    return response
                failure = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code != 429 and response.status_code < 500:
                    raise JustCallError(f"JustCall {method} {path} failed: {failure}")
                retry_after = response.headers.get("Retry-After")
            if attempt == _MAX_RETRIES:
                break
            self._sleep(_backoff_delay(attempt, retry_after))
        raise JustCallError(
            f"JustCall {method} {path} failed after {_MAX_RETRIES + 1} attempts: {failure}"
        )

    # ------------------------------------------------------------------- api

    def fetch_campaign_contacts(
        self,
        *,
        progress_status: str | None = None,
        contact_status: str | None = None,
        per_page: int = 100,
    ) -> Iterator[dict]:
        """Yield every matching Sales Dialer contact, newest first.

        Pages lazily: `page` is 0-based and pagination stops at the first page
        shorter than `per_page` (including empty). ``order=desc`` is fixed so
        sync can early-stop at its incremental cursor simply by abandoning the
        iterator — the binding signature exposes no order knob.
        """
        if per_page < 1:
            raise ValueError("per_page must be >= 1")
        page = 0
        while True:
            params: dict[str, object] = {
                "campaign_id": self._cfg.justcall_campaign_id,
                "per_page": per_page,
                "page": page,
                "order": "desc",
            }
            if progress_status is not None:
                params["progress_status"] = progress_status
            if contact_status is not None:
                params["contact_status"] = contact_status
            response = self._request("GET", CONTACTS_PATH, params=params)
            body = response.json()
            rows = body.get("data", []) if isinstance(body, dict) else body
            if not isinstance(rows, list):
                raise JustCallError(
                    "unexpected contacts response shape (expected list or {'data': [...]})"
                )
            yield from rows
            if len(rows) < per_page:
                return
            page += 1

    def set_contact_dnca(self, justcall_contact_id: int) -> None:
        """Flip a campaign contact to DNCA so reps never redial an opt-out.

        SPEC §1 pins the Sales Dialer contacts endpoint but not the exact
        status-update body, so the shape lives in this one place — if JustCall
        rejects the field names, only this method changes.
        """
        self._request(
            "POST",
            CONTACTS_PATH,
            json_body={
                "campaign_id": self._cfg.justcall_campaign_id,
                "id": justcall_contact_id,
                "status": "DNCA",
            },
        )
        log.info("JustCall contact %s set to DNCA", justcall_contact_id)

    def add_contact_note(self, justcall_contact_id: int, note: str) -> None:
        """Attach an AI-call outcome note so the campaign reflects reality
        (SPEC §7). Same endpoint caveat as set_contact_dnca; note text is not
        logged because it can carry transcript fragments."""
        self._request(
            "POST",
            CONTACTS_PATH,
            json_body={
                "campaign_id": self._cfg.justcall_campaign_id,
                "id": justcall_contact_id,
                "notes": note,
            },
        )
        log.info("JustCall note added to contact %s", justcall_contact_id)
