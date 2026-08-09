# Trust Hub checklist — do this BEFORE volume dialing

Number reputation is our biggest operational risk (SPEC §3). The existing
JustCall numbers are already spam-flagged; these steps are how we avoid
repeating that with the Twilio pool. `TRUST_HUB_CONFIRMED=true` (which gates
`PHASE=3`) means every box below is checked — set it by hand, deliberately.

These are manual console/portal steps; Twilio's business-verification reviews
take days, so start before Phase 2.

## 1. Trust Hub business profile

- Twilio Console → Trust Hub → create a **Primary Customer Profile** for the
  legal entity behind Reachwell (legal name, EIN, address, website).
- Wait for approval (typically 1–3 business days). Everything below attaches
  to this profile.

## 2. SHAKEN/STIR attestation

- Attach a **CNAM & SHAKEN/STIR Trust Product** to the profile and assign
  every number in the pool. Goal: **A-level attestation** on outbound calls
  so carriers see us as the legitimate owner of the caller ID.
- Re-run the assignment step every time `orchestrator numbers purchase` adds
  numbers — new numbers are NOT covered automatically.

## 3. CNAM registration

- Register the outbound CNAM as **"Reachwell"** for every pool number so that
  name displays instead of a bare number where carriers support it.
- CNAM propagation is carrier-dependent and slow (days-to-weeks); verify with
  test calls to phones on the major carriers.

## 4. Voice Integrity / branded calling (recommended)

- Enroll the pool in Twilio **Voice Integrity** (spam-label remediation
  across the analytics providers) and, if budget allows, **Branded Calling**.
- Register the numbers + campaign description with **Free Caller Registry**
  (freecallerregistry.com) covering the three major spam-analytics vendors.

## 5. Ongoing hygiene (why the code does what it does)

- Ramp: new numbers start at `RAMP_SCHEDULE` dials/day, not full volume —
  sudden volume on a fresh number is the classic spam-flag trigger.
- Rotation: per-number daily cap + cooldown spreads volume across the pool.
- Health: connect-rate collapse vs pool average auto-benches a number and
  alerts — treat a bench as "this number is burned", replace it, and check
  the registries above for its status.

## Sign-off

When 1–4 are done for the whole pool:

```
TRUST_HUB_CONFIRMED=true
```

`orchestrator readiness` fails at PHASE=3 until this is set. Flipping it
without doing the work just moves the failure to the carriers.
