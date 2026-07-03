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

### `check_seller_responses.py` → seller_reply (the live one)

At the top of Muffin's script:
```python
import sys; sys.path.insert(0, "/root/Aria-Swarm")
from tools.muffin_bridge import notify_seller_reply
```
Then where a new inbound reply is detected:
```python
notify_seller_reply(lead_id, reply_body, from_address)   # -> swarm qualifier
```

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
