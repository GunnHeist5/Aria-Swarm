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
#   DEALDESK_EXPORT_PATH=/root/.hermes/exports/land.xlsx
#   DEALDESK_ASSIGNMENT_FEE_USD=4000                    # ** TUNE FOR LAND ** (see below)
#   DEALDESK_ARV_MULTIPLIER=0.70                        # optional override
#   DEALDESK_OPENING_FRACTION=0.85                      # anchor = 85% of ceiling
#   DEALDESK_MIN_VIABLE_OFFER_USD=1000

.venv/bin/uvicorn tools.dealdesk.api:app --host 127.0.0.1 --port 8088
# health check:
curl -s localhost:8088/health
```

Put it behind your existing Cloudflare tunnel / reverse proxy so Trillet can
reach it over HTTPS, and keep it bound to localhost otherwise. Run it as a
systemd service so it survives reboots (same pattern as the swarm heartbeat).

## ⚠️ Tune the pricing for LAND before going live

The swarm's default `assignment_fee_usd` is **$15,000 — a house number**. On a
cheap infill lot (many Harris County lots are $5k–30k), a flat $15k fee makes
the formula return nothing, so **every cheap-lot call escalates**. Set
`DEALDESK_ASSIGNMENT_FEE_USD` to a land-appropriate figure (e.g., $3k–5k, or your
real target spread) so the desk can actually make offers. `escalate_reason:
"below_min_viable"` is the signal your fee/multiplier needs adjusting for a price
band. These are the same genome parameters the swarm evolves — start with your
known-good land numbers.

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

The receptionist prompt already references `get_offer_range` and only offers
within `[opening_offer, max_offer]`, never above `max_offer`, and routes any
`escalate:true` to a human. Until this tool is connected, the prompt degrades
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
`listed_with_agent`, `encumbered`, `not_land`, `below_min_viable`,
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
