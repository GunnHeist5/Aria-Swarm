# DEALDESK.md — the live pricing API for the AI receptionist

Turns Aria (the Trillet voice agent) from a message-taker into a closer: when a
seller names their property, Aria calls this endpoint, which looks the lot up in
your PropStream data, runs **your** offer formula, and returns a negotiation
band — an opening offer and a hard ceiling Aria must never exceed. Anything it
can't price cleanly comes back flagged to escalate to you.

```
seller: "I'm Jane, selling my lot at 4210 Bering"
   → Trillet calls  POST /offer-range {address:"4210 Bering"}
      → look up the property → run max_offer = ARV×mult − repair×mult − fee
         → { opening_offer, max_offer, escalate }   ← Aria negotiates in this band
```

## Run it on the VPS

The data source is a PropStream export file (the same data Muffin syncs to the
Sheet). Drop your latest land export somewhere and point the service at it.

```bash
cd /root/Aria-Swarm
# in .env (never in chat):
#   DEALDESK_API_KEY=<a long random secret>            # Trillet authenticates with this
#   DEALDESK_EXPORT_PATH=/root/land_export.xlsx        # your PropStream land export
#   DEALDESK_ARV_BASIS=lower_of                        # lower_of (default) | assessed | est_value
#   DEALDESK_ASSIGNMENT_FEE_PCT=0.10                   # fee = 10% of resale (default; scales with lot)
#   DEALDESK_MAX_AUTONOMOUS_OFFER_USD=250000           # ceilings above this escalate to a human
#   DEALDESK_ARV_MULTIPLIER=0.70                       # optional override
#   DEALDESK_OPENING_FRACTION=0.85                     # anchor = 85% of ceiling
#   DEALDESK_MIN_VIABLE_OFFER_USD=1000
#   # DEALDESK_ASSIGNMENT_FEE_USD=4000                 # only used when FEE_PCT=0 (flat fee)

.venv/bin/uvicorn tools.dealdesk.api:app --host 127.0.0.1 --port 8088
# health check:
curl -s localhost:8088/health
```

Put it behind your existing Cloudflare tunnel / reverse proxy so Trillet can
reach it over HTTPS, and keep it bound to localhost otherwise. Run it as a
systemd service so it survives reboots (same pattern as the swarm heartbeat).

## How the ceiling is priced for LAND

Three knobs make the ceiling safe on vacant land — all defaulted so it works out
of the box, all env-overridable:

- **`DEALDESK_ARV_BASIS=lower_of`** — PropStream's "Est. Value" on land often
  floats on nearby *improved* comps and runs hot. `lower_of` builds the ceiling
  on the **smaller** of Est. Value and county-assessed value, so an inflated
  estimate can never set the offer. (Real example from the Harris export: a lot
  with Est. Value $206k but assessed $4,725 — `lower_of` prices it off $4,725,
  not $206k. Use `assessed` to always trust the county, `est_value` to trust
  PropStream.)
- **`DEALDESK_ASSIGNMENT_FEE_PCT=0.10`** — the fee is a **percentage of the
  buyer-side resale**, not a flat house-sized number. A cheap lot yields a
  proportionally smaller fee instead of escalating `below_min_viable`. Set it to
  `0` to fall back to a flat `DEALDESK_ASSIGNMENT_FEE_USD`.
- **`DEALDESK_MAX_AUTONOMOUS_OFFER_USD=250000`** — any lot whose ceiling exceeds
  this comes back `escalate_reason: "high_value"` so a **human** negotiates the
  whale, never Aria unattended. Set `0` to disable.

The ceiling remains a hard cap: any price at or below `max_offer` is profitable
by construction, which is what makes unattended negotiation safe. These are the
same genome parameters the swarm evolves — start with your known-good numbers.

## Trillet setup (APIs / Integrations tab)

Add one custom API tool the agent can call:

- **Name:** `get_offer_range`
- **Method / URL:** `POST https://<your-host>/offer-range`
- **Headers:** `Authorization: Bearer <DEALDESK_API_KEY>`, `Content-Type: application/json`
- **Body (from call variables):**
  ```json
  { "address": "{{address}}", "apn": "{{apn}}", "owner_name": "{{owner_name}}" }
  ```
- **Response fields the prompt uses:** `found`, `opening_offer`, `max_offer`,
  `escalate`, `escalate_reason`.

The receptionist prompt (see **`RECEPTIONIST.md`** — the paste-ready System
Prompt + the value-argument talk track) references `get_offer_range` and only
offers within `[opening_offer, max_offer]`, never above `max_offer`, and routes
any `escalate:true` to a human. Until this tool is connected, the prompt degrades
safely — Aria captures details and escalates instead of inventing numbers.

## Response shape

```json
{
  "found": true,
  "opening_offer": 46800,
  "max_offer": 55000,
  "escalate": false,
  "escalate_reason": null,
  "property": {"address": "...", "city": "...", "county": "Harris", "state": "TX", "apn": "..."},
  "notes": "clean"
}
```
Escalations return `escalate:true` with a reason: `not_found`, `no_valuation`,
`listed_with_agent`, `encumbered`, `high_value`, `not_land`, `below_min_viable`,
`data_unavailable`. **The response never includes ARV, comps, or margin** — so
the agent can't leak how offers are computed.

## Safety notes

- Bearer auth is **required**; with `DEALDESK_API_KEY` unset the endpoint refuses
  every request (fail-closed).
- The ceiling (`max_offer`) is your formula's hard cap — any accepted price at or
  below it is profitable by construction, which is what makes it safe for Aria to
  negotiate unattended.
- Aria's authority ends at a **verbal** agreement. The written contract, e-sign,
  and any money movement stay human (CRITICAL_GATE) — the desk prices, the human
  signs.
- Freshness: the file source is as current as your last export. For live data,
  a `SheetLookup` (reading the Sheet Muffin syncs) drops in behind the same
  `find()` interface — a small later add.
