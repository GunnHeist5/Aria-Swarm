# Vacant-Land Wholesaling Integration — roadmap

Goal: the swarm drives the wholesaling pipeline **end-to-end**, funded by **10%
of every closed deal**. End-to-end means the swarm *operates every stage*, while
**humans gate two things forever: signing contracts and moving real money.**
This is a phased program, not a single build — each stage is its own integration
with its own guardrail.

---

## The pipeline (stages, integration, guardrail)

| # | Stage | Integration | Guardrail |
|---|---|---|---|
| 1 | Pull leads | List provider API / your lists | dedupe, suppression list |
| 2 | Skip trace | Skip-trace provider | cost cap per batch |
| 3 | Outreach | **Instantly** (30 mailboxes) | CAN-SPAM: footer + opt-out + volume ramp |
| 4 | Reply triage / qualify | Instantly inbox + LLM | auto-reply only to clear non-offers |
| 5 | Comps / offer | your comps source | **human approves every offer number** |
| 6 | Contract / e-sign | your e-sign tool | **HITL — a human signs every contract** |
| 7 | Disposition (find buyer) | buyer list + outreach | same CAN-SPAM rules |
| 8 | Title / close | a fast-turnaround title co. (you still need one) | standard closing controls |
| 9 | Payout + book 10% | `tools/revenue.py` + wallet | **HITL — real money move** |

## Phase plan (build order — lowest risk / highest leverage first)

- **Phase 1 — Lead gen + outreach + reply triage (stages 1–4).**
  The swarm pulls/skips lists, runs Instantly campaigns across your 30 mailboxes,
  and triages replies into "interested / not / wrong number / DNC." No money, no
  contracts — pure top-of-funnel leverage. This is where to start.
- **Phase 2 — Comps + offer drafting (stage 5).** Swarm drafts the offer; **you
  approve the number** before it's sent.
- **Phase 3 — Disposition (stage 7).** Swarm markets the contract to your buyer
  list once you have a signed deal.
- **Phase 4 — Close loop (stages 8–9).** Title coordination + the 10% booking.
  Contract signing (6) and the payout (9) stay **permanently HITL**.

"End-to-end" = the swarm runs 1–5 and 7 autonomously; 6, 8, 9 are human-gated.

## Compliance — stop-gates, not optional

- **Wholesaling legality varies by state.** Some require a real-estate license or
  restrict marketing the *equitable interest* / the contract itself. Confirm your
  state's rules (and your attorney's read) before the swarm sends a single offer.
- **CAN-SPAM** on all cold email: valid physical address, working opt-out honored
  promptly, no deceptive subject lines, ramp volume so domains stay healthy.
- **Never** let the swarm autonomously **sign a contract** or **move real money** —
  both are hard HITL gates in the architecture (`CRITICAL_GATE`), and we keep them
  that way regardless of how good the swarm gets.

## Fiat → USDC (the 10% routing)

The cut closes in **USD** (title wire to your bank); the swarm treasury is
**USDC**. To bridge:
- **Now (manual):** on close, convert the 10% to USDC (Coinbase) and send it to
  the swarm's Base address, then record it: `python -m tools.revenue --deal-id
  <id> --assignment-fee <gross_fee>`. The CLI books what *arrived* on-chain.
- **Later (automated):** a CRM "deal closed" webhook → a small service that
  converts + sends via the Coinbase/CDP API, then calls the same booking. Keep a
  human approval on the transfer (it's a real-money move).

## What I need from you to build Phase 1

1. **Instantly** — API key + how you run it today (campaigns/sequences, sending
   volume per mailbox, current domains).
2. **List provider** — name + API/export method.
3. **Skip-trace** — provider + API.
4. **CRM** — which tool (Podio / Airtable / GoHighLevel / custom) + API access.
5. **Comps** — your data source / how you price land now.
6. **E-sign** — which tool (for Phase 4 wiring, not Phase 1).
7. **Title** — pick a fast-turnaround company (the one missing piece).
8. **VPS** — OS/version + how you access it (for the deploy).
9. **State(s)** you operate in (for the compliance read).

With #1–4 I can start Phase 1: a `tools/wholesaling/` package that pulls a list,
skip-traces, launches an Instantly campaign, and triages replies into your CRM —
all under the swarm's metabolic/HITL guardrails.

## Out of scope until explicitly wired

Autonomous contract signing, automated real-money transfers (still simulated +
HITL), and the on-ramp automation. These stay gated by design.
