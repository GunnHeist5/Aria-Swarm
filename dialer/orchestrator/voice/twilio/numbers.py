"""Number-pool provisioning: buy local Twilio numbers, register them in
Postgres (SPEC §4's config-driven purchase script).

Idempotent per number: registration is an ``ON CONFLICT DO NOTHING`` upsert
keyed on phone_e164, so re-running a purchase plan (or importing a number
twice) never duplicates pool rows or resets ramp state — ``purchased_at``
(which drives the ramp schedule) is set once, on first insert.
"""

from __future__ import annotations

from twilio.rest import Client

from orchestrator import db
from orchestrator.config import ConfigError, Settings
from orchestrator.logging_utils import get_logger

from .provider import TwilioVoiceError

log = get_logger(__name__)


def _rest_client(cfg: Settings) -> Client:
    cfg.require("twilio_account_sid", "twilio_auth_token")
    return Client(cfg.twilio_account_sid, cfg.twilio_auth_token)


def _area_code_of(phone_e164: str) -> str:
    digits = "".join(ch for ch in phone_e164 if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[:3]
    raise ConfigError(f"cannot derive area code from {phone_e164!r} (not US E.164)")


def _register(cfg: Settings, phone_e164: str, area_code: str, provider_sid: str | None) -> bool:
    """Insert into the pool; False when the number was already registered."""
    inserted = db.execute(
        cfg,
        """
        INSERT INTO numbers (phone_e164, area_code, provider, provider_sid)
        VALUES (%s, %s, 'twilio', %s)
        ON CONFLICT (phone_e164) DO NOTHING
        """,
        (phone_e164, area_code, provider_sid),
    )
    if inserted:
        log.info("registered pool number %s (area %s)", phone_e164, area_code)
    else:
        log.info("pool number %s already registered", phone_e164)
    return inserted > 0


def purchase_numbers(cfg: Settings, plan: list[dict]) -> list[str]:
    """Buy numbers per plan (``[{"area_code": "614", "count": 2}]``).

    Returns the E.164 numbers purchased in THIS run. A shortfall (area code
    sold out, purchase rejected) is logged loudly but does not raise — the
    operator re-runs with an adjusted plan; partial pools are usable.
    """
    client = _rest_client(cfg)
    purchased: list[str] = []
    for entry in plan:
        area_code = str(entry["area_code"]).strip()
        count = int(entry["count"])
        if not area_code.isdigit() or len(area_code) != 3:
            raise ConfigError(f"bad area_code in purchase plan: {entry!r}")
        if count < 1:
            continue
        # Over-fetch candidates: some purchases fail (number grabbed by
        # someone else between search and create).
        candidates = client.available_phone_numbers("US").local.list(
            area_code=int(area_code), limit=count * 2
        )
        acquired = 0
        for candidate in candidates:
            if acquired >= count:
                break
            phone = candidate.phone_number
            try:
                incoming = client.incoming_phone_numbers.create(
                    phone_number=phone,
                    friendly_name=f"reachwell-pool-{area_code}",
                )
            except Exception as exc:  # noqa: BLE001 — try the next candidate
                log.warning(
                    "purchase of %s failed (%s); trying next candidate",
                    phone, type(exc).__name__,
                )
                continue
            _register(cfg, phone, area_code, incoming.sid)
            purchased.append(phone)
            acquired += 1
        if acquired < count:
            log.error(
                "area code %s: wanted %d numbers, purchased %d — adjust the "
                "plan or try again later",
                area_code, count, acquired,
            )
    return purchased


def import_number(cfg: Settings, phone_e164: str) -> None:
    """Register an ALREADY-OWNED Twilio number into the pool.

    Fails closed when the account doesn't own the number — registering a
    caller ID we can't actually place calls from would poison the pool.
    """
    client = _rest_client(cfg)
    owned = client.incoming_phone_numbers.list(phone_number=phone_e164, limit=1)
    if not owned:
        raise TwilioVoiceError(
            f"{phone_e164} is not owned by this Twilio account — purchase it "
            "first or fix the number"
        )
    _register(cfg, phone_e164, _area_code_of(phone_e164), owned[0].sid)
