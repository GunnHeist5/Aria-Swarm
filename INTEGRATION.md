# INTEGRATION.md — wiring sensors into the event-driven swarm

The swarm is **event-dispatched**: `main.py --event TYPE --payload '<json>'`
fires one trigger, the graph wakes only the subgraph that trigger needs,
the state snapshot is persisted, and the process exits. Operations are
event-driven; the clock is reserved for the metabolic heartbeat.

```
sensor (Muffin / cron / webhook)
      │  python main.py --event <type> --payload '<json>'
      ▼
dispatch router ─┬ heartbeat / wallet_low → metabolic check → exit
                 ├ ideation              → full dialectic (Visionary→Realist→Synthesizer)
                 ├ seller_reply          → qualifier gene triages the reply
                 ├ deal_closed           → book the swarm's 10% → metabolic check
                 ├ new_leads_synced      → pipeline bookkeeping
                 └ offer_accepted        → HITL FREEZE (contract = CRITICAL_GATE)
```

Every firing is durable: state hydrates from `~/.automaton/state_snapshot.json`
before the invoke and persists after it, so sensors on the same box can fire
independently without coordinating. Unknown event types are accepted and
**fail closed** (HITL freeze) — a sensor typo is surfaced, never dropped.

## The triggers

| Event | Fired by | Payload | Effect |
|---|---|---|---|
| `heartbeat` | systemd/cron timer (`--cron` is sugar for it) | none | metabolic ratio, lifecycle (saving/replication/extinction), capital phases |
| `ideation` | operator / schedule, when there's a decision to make | none | full Visionary → Realist → Synthesizer dialectic |
| `seller_reply` | Muffin's Seller Response Monitor | `lead_id`, `reply_text`, `contact` (+ any lead fields) | qualifier gene triages → verdict recorded in `operational_flags["qualified_replies"]` |
| `deal_closed` | close-out step / CRM webhook | `deal_id`, `assignment_fee_usd`, optional `swarm_cut_pct` | books the swarm's cut (idempotent per `deal_id`), re-runs metabolism in the same invoke |
| `new_leads_synced` | PropStream→Sheets sync job | free-form counts (e.g. `{"count": 412}`) | pipeline bookkeeping in `operational_flags["lead_pipeline"]` |
| `offer_accepted` | reply-handling flow | offer/contract details | **always freezes for HITL** — a human signs every contract |
| `contract_signed` | human/operator, after signing | `deal_id`, optional `state` (2-letter, default TX), `signed_date` (default: today), `address`, `arv`, `offer_price`, `assignment_fee_target` | opens the **10-day dispo clock**, routes the closing by state (unreviewed state → HITL freeze), emits the Day-0 "blast" action |
| `buyer_confirmed` | dispo flow, when earnest posts | `deal_id`, `buyer`, `earnest_posted` (bool — required true to lock) | stops the dispo clock → wire pending; no earnest = clock keeps running |
| `venture_proposed` | swarm dialectic / operator | `venture_id`, `kind`, optional `hypothesis`, `seed_cap_usd`, `stage_budgets`, `kill_criteria` | opens a venture under graduated autonomy; cheap/proven → funds stage 1, big/critical → HITL gate. See VENTURES.md |
| `venture_validated` | hands / metrics feed | `venture_id` + any of `signal`, `revenue_usd`, `spent_usd` | records metrics, then ticks — a gate/kill line acts immediately |
| `venture_killed` | operator | `venture_id` | manual apoptosis — reclaims unspent capital to treasury |
| `learning_ingested` | operator (send a link) | `url` | fetch → distill → **gated** route: knowledge stored, venture idea → gated venture path, genome tweak → Red Queen + autonomy gate. See LEARNING.md |
| `wallet_low` | balance monitor | none | forces a saving-mode re-evaluation |

`hitl_resume` is **not** an event — resuming a frozen thread goes through
`python resume.py <thread_id> approve|reject` (LangGraph Command resume).

## The 10-day dispo clock (heartbeat-enforced)

Once `contract_signed` fires, **every heartbeat** checks the deal against the
disposition timeline and surfaces due actions in the snapshot under
`operational_flags.dispo_actions_due` (plus loud `error_log` lines and an alert
webhook on the critical gates). The swarm tracks and alerts; humans/Muffin
execute the blasts, contracts, cancellations, and wires.

| Day | Action emitted | Rule |
|---|---|---|
| 0 | `blast` | hit all 4 buyer platforms in parallel, never sequential |
| 1 | `followup` | re-touch every platform |
| 3 | `escalate` | "interested buyers only — 7 days to close" |
| 5 | `maxdispo_gate` | HARD gate: full deal package to MaxDispo (baseline bid in writing). First seen after Day 5 → `maxdispo_gate_MISSED` |
| 9 | `decision_point` | EOD: buyer confirmed w/ earnest, or prepare cancellation |
| 10 | `hard_deadline` | resolve or cancel via option period — phase becomes `cancel_pending` |

`buyer_confirmed` with `earnest_posted: true` stops the clock (phase
`wire_pending`); interest without earnest does NOT. The timeline days and
platform list are genome (`tools/wholesaling/dispo.DispoConfig`) — evolvable
like every other strategy parameter.

## Closing router (nationwide scale)

`contract_signed` routes each deal's closing by property state through
`tools/wholesaling/closing.py`:

- **Reviewed state** (today: TX) → the deal record gets its vetted closer:
  primary **CLOSED Title** + local backup, with the state's compliance notes
  (TX: SB 2212 equitable-interest disclosure; promulgated premiums).
- **Unreviewed/unknown state** → **fail-closed**: the record is kept but the
  swarm freezes with `unreviewed_state:<XX>` — attorney-close states (GA, SC,
  NC, ...) and restrictive-wholesaling states (OK, IL, SC) surface their
  specific warning. No outreach/closing proceeds until a human reviews the
  market and adds the row (`closing.review_state(...)` →
  `operational_flags["closing_rules_overrides"]` — one row per new market, no
  deploy).

**Wire watchdog**: once a buyer is confirmed (`wire_pending`), every heartbeat
counts business days since confirmation; past 2 business days without the wire
(`resolve_dispo(deal_id, "closed")` not yet fired) it emits `wire_overdue_dN`
alarms — "follow up with the title company NOW" — one per day until resolved.

## Muffin as the sensor — `tools/muffin_bridge.py` (the seam)

Muffin stays the operator (the hands); the swarm is the manager/brain. They run
on the same VPS but under **different venvs** (Muffin: hermes venv; swarm: its
own `.venv`), so the bridge is **stdlib-only** — Muffin imports it directly and
it shells out to the swarm's interpreter. Every call is **fire-and-forget and
never raises into Muffin** — a swarm hiccup can't break the operator loop.

Paths default to `/root/Aria-Swarm` (override via `SWARM_DIR` / `SWARM_PYTHON` /
`SWARM_MAIN` env vars).

### Instantly `reply_received` webhook → seller_reply (the LIVE campaign sensor)

**This is the real reply source for the outreach campaign.** Cold-email replies
land in Instantly (routed to the sending mailbox / Unibox), not in any single
inbox — so Instantly's `reply_received` webhook, whose payload carries the full
`reply_text` + `lead_email`, is the correct sensor. `tools/integrations/instantly_webhook.py`
is a tiny FastAPI receiver that validates a shared secret and fires `seller_reply`
(detached, idempotent, noise-filtered) — the same qualifier brain as the CLI seam.

Run it on the VPS behind your Cloudflare tunnel (same pattern as the deal desk):
```bash
INSTANTLY_WEBHOOK_SECRET=<long random secret> \
  /root/Aria-Swarm/.venv/bin/uvicorn tools.integrations.instantly_webhook:app \
  --host 127.0.0.1 --port 8099
```
Then Instantly → Settings → Webhooks → **Add Webhook**:
- **URL:** `https://<your-host>/instantly/reply?token=<INSTANTLY_WEBHOOK_SECRET>`
- **Event:** `reply_received`

The secret is required (fail-closed 401 without it); non-reply events and
automated senders are acked-and-ignored; firing is fire-and-forget so Instantly
gets a fast 200.

### `check_seller_responses.py` → seller_reply (legacy inbox monitor — optional)

> Note: this watches a single himalaya inbox, which is **not** where the Instantly
> campaign's replies arrive. Keep it only if that inbox still receives real
> inbound; the Instantly webhook above is the campaign sensor.

At the top of Muffin's script (append to `sys.path` so nothing shadows Muffin's
own imports; guard the import so a missing swarm repo can never break Muffin):
```python
import sys; sys.path.append("/root/Aria-Swarm")
try:
    from tools.muffin_bridge import notify_seller_reply
except Exception:
    notify_seller_reply = None
```
Muffin fetches envelopes (`himalaya envelope list`) which carry no body, so pull
the message text on demand and pass it as `reply_text`; the sender address is the
lead key. In the loop over detected responses:
```python
if notify_seller_reply:
    from_addr = resp.get("from", {}).get("addr", "")
    if from_addr:
        body = get_message_body(resp.get("id")) or resp.get("subject", "")
        notify_seller_reply(from_addr, body, from_addr)   # -> swarm qualifier
```
where `get_message_body(id)` shells `himalaya message read <id>` (best-effort,
falls back to the subject). The swarm qualifier is idempotent per lead+text, so
re-listing the same inbox reply every cron run costs nothing.

### Other events (import the matching helper)
```python
from tools.muffin_bridge import (
    notify_deal_closed, notify_leads_synced,
    notify_contract_signed, notify_buyer_confirmed,
)
notify_deal_closed("D123", 12000)          # books the swarm's 10% (idempotent)
notify_leads_synced(412)                    # pipeline bookkeeping
notify_contract_signed("D123", state="TX")  # opens the 10-day dispo clock
notify_buyer_confirmed("D123", "Cash LLC", earnest_posted=True)
```

### Shell-only (cron steps, no import)
```bash
SWARM=/root/Aria-Swarm/.venv/bin/python
$SWARM -m tools.muffin_bridge seller-reply --lead-id L1 --reply "..." --contact "a@b.c"
$SWARM -m tools.muffin_bridge deal-closed  --deal-id D123 --assignment-fee 12000
$SWARM -m tools.muffin_bridge leads-synced --count 412
```

Booking is idempotent per `deal_id`; the manual `python -m tools.revenue ...`
still works as a fallback (same ledger).

## Reply puller — Instantly backstop (`tools/integrations/instantly_replies.py`)

The webhook is the real-time sensor; the puller is the guarantee. It lists every
received reply in the campaign via the Instantly v2 `/emails` API and fires each
*new* one through the same `seller_reply` pipeline (triage → deal card → draft →
Telegram). A seen-ledger (`~/.automaton/instantly_replies_seen.json`, keyed by
Instantly's email id) makes re-polling free — one push per reply, ever.

```bash
python -m tools.integrations.instantly_replies          # dry-run: list replies
python -m tools.integrations.instantly_replies --push   # fire NEW replies
python -m tools.integrations.instantly_replies --raw    # first raw item (schema debug)
```

Poll-every-5-minutes backstop (systemd):

```ini
# /etc/systemd/system/aria-replypoll.service
[Unit]
Description=ARIA Instantly reply poller
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/root/Aria-Swarm
ExecStart=/root/Aria-Swarm/.venv/bin/python -m tools.integrations.instantly_replies --push

# /etc/systemd/system/aria-replypoll.timer
[Unit]
Description=Poll Instantly replies every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

`systemctl daemon-reload && systemctl enable --now aria-replypoll.timer`

## Deal chat — talk back to the deal desk (`tools/dealflow/chat.py`)

Pushes are one-way; the chat is the way back. Send the bot a plain message —
"why did this escalate?", "counter her at 60k", "rewrite it more direct" — and
the specialist model answers with the deal's context loaded (card, band,
seller's words, recent turns). Target a deal by replying to its push, naming
the seller's email, or saying nothing (most recent push wins).

Every reply push writes its context to `~/.automaton/deal_chat.json`; only the
operator's `JUSTIN_TELEGRAM_CHAT_ID` is answered. Chat can explain, re-price,
and re-draft, but has no send path — emails go out from Instantly (operator)
and contracts only via the Accept button (CRITICAL_GATE unchanged).

The chat is **agentic** (`tools/dealflow/agent.py`): the model carries tools —
`lookup_property` (any lead, by email or address), `list_pending_deals`,
`agree_deal(email, price)` (stores the deal at the *negotiated* price and sends
the Accept/Decline prompt — refuses a price above the ceiling), and
`suppress_lead`. So "agreed at 85k with jane@…" closes at the real number; the
push button remains the shortcut for closing at the opening. No tool sends
email; `agree_deal` only *asks* — Accept is still the only path to a contract.

Draft registers: when a seller's ask exceeds **2× the ceiling**, the auto-draft
switches from the warm template to a candid, number-forward register (state the
opening, ground it in assessed value, leave the door open, don't chase).

## Exit codes

`0` done · `1` cycle error (state preserved) · `2` frozen awaiting HITL
(resume with `resume.py`) · `3` extinction (final snapshot persisted).

## Reading the swarm's output

The qualifier's verdicts land in the snapshot under
`operational_flags.qualified_replies.<lead_id>.assessment` (XML:
`verdict / motivation / price_signal / timeline / red_flags / next_action`).
The operator layer (Muffin) consumes `next_action`; the swarm never sends
outreach itself — responding, offers, and contracts stay behind Muffin's
approval gates, and anything touching money or contracts is CRITICAL_GATE.
