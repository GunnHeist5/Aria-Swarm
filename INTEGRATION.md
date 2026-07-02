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
| `wallet_low` | balance monitor | none | forces a saving-mode re-evaluation |

`hitl_resume` is **not** an event — resuming a frozen thread goes through
`python resume.py <thread_id> approve|reject` (LangGraph Command resume).

## Muffin as the sensor (same VPS, no network transport)

Muffin stays the operator (the hands); the swarm is the manager/brain. The
seam is one subprocess call — add it where each Muffin script detects the
condition. Adjust `SWARM_DIR`/python path to the real install location.

### `check_seller_responses.py` → seller_reply

```python
import json, subprocess

SWARM = ["/root/Aria-Swarm/.venv/bin/python", "/root/Aria-Swarm/main.py"]

def notify_swarm_of_reply(lead_id: str, reply_text: str, contact: str) -> None:
    """Fire-and-forget: the swarm qualifies; Muffin keeps operating either way."""
    payload = {"lead_id": lead_id, "reply_text": reply_text, "contact": contact}
    try:
        subprocess.run(
            [*SWARM, "--event", "seller_reply", "--payload", json.dumps(payload)],
            timeout=120, check=False,
        )
    except Exception as exc:
        print(f"swarm notify failed (non-fatal): {exc}")
```

### Deal close-out → deal_closed (replaces the manual `python -m tools.revenue`)

```bash
python /root/Aria-Swarm/main.py --event deal_closed \
  --payload '{"deal_id": "D123", "assignment_fee_usd": 12000}'
```

Booking is idempotent per `deal_id` — a retried webhook never double-counts.
The manual CLI (`python -m tools.revenue --deal-id D123 --assignment-fee 12000`)
still works as a fallback; both paths share the same ledger.

### Lead sync → new_leads_synced

```bash
python /root/Aria-Swarm/main.py --event new_leads_synced --payload '{"count": 412}'
```

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
