# Compliance map — SPEC §6 requirement → enforcing code

Every requirement is a **hard block, not a warning**: the gate
(`orchestrator/compliance/__init__.py:check_dial_allowed`) runs at planning
time AND again in the dial worker moments before the provider call, and a
missing input (DNC list, consent basis, unknown area code) blocks exactly
like a violation. Dry-run CSVs surface every reason so nothing is silent.

| Requirement (SPEC §6) | Enforced by |
|---|---|
| Calling window in the PROSPECT's local time, from area code; 9am–5pm default; per-state override table | `compliance/windows.py` (`is_within_window`, `next_window_open`) over `compliance/area_codes.py` (full NANP → state + IANA tz map); overrides in `config/state_windows.json` |
| Federal DNC scrub before EVERY dial | `compliance/dnc.py` — `FileDncChecker` over `DNC_LIST_PATH`; **no configured list ⇒ every dial blocked** (`DNC_SOURCE_MISSING`) |
| Internal suppression list before EVERY dial | `compliance/suppression.py` — insert-only Postgres ledger; checked in the gate |
| Suppression permanent + immediate on any opt-out / "stop calling" / rep disqualification | `queueing/completion.py` (opt_out/disqualified dispositions), `justcall/webhook.py` (rep dispositions + DNCA), `justcall/sync.py` (DNCA statuses). No delete path exists in code. |
| Second-touch: someone who told a rep no is never re-dialed by a bot | same suppression path — rep-marked DNC/disqualification suppresses the phone across BOTH touch types before any AI follow-up is scheduled |
| Max total attempts per business across both touch types | `compliance/attempts.py` counted per phone in the gate; `queueing/retries.py` re-checks before scheduling any retry (`MAX_ATTEMPTS_TOTAL`) |
| Agent MUST disclose it is an AI at call start | `prompts/openers.md` — every opener variant carries the disclosure ("an AI assistant calling on behalf of Justin at Reachwell"); `agent/script.py` refuses variants without it; the TwiML welcome greeting is built from the opener |
| Recording/transcript retention configurable; two-party consent states honored | `RECORDING_ENABLED`, `*_RETENTION_DAYS`, `TWO_PARTY_POLICY` in config; `config/two_party_states.json` + `compliance/consent.py:is_two_party_state`; the dial worker passes `two_party_disclose` into the call variables and the script appends the recording disclosure (or recording is disabled under `no_record`) |
| Consent basis per contact, required, NO default | `contacts.consent_basis` column stamped at sync from `CONSENT_BASIS_*` (never invented, never overwritten); NULL ⇒ `NO_CONSENT_BASIS` block; snapshotted onto every attempt row for the audit trail |

## Defense in depth beyond §6

- **Phase gate in code**: phase 1 places zero calls (`worker.dial_attempt`
  refuses independently of the gate); phase 2 refuses the first-touch
  audience and clamps live second-touch attempts to ~50.
- **Kill switch**: `orchestrator kill` — Redis fast path + Postgres
  authority, fail-closed reads (`queueing/killswitch.py`).
- **Idempotency**: one dial per `(contact, touch, attempt)` enforced by DB
  uniques + reserve-before-dial commit ordering; duplicate webhooks dedupe in
  `webhook_events`.
- **Non-US NANP blocked** by default (`ALLOW_NON_US_NANP=false`); unknown
  area codes always blocked.

## TCPA notes (context for the design, not legal advice)

- An AI voice agent is an **artificial or prerecorded voice** under the TCPA
  as interpreted by the FCC's February 2024 declaratory ruling on AI-generated
  voices. Calls to wireless numbers with such a voice generally require
  **prior express consent** — and telemarketing calls generally require
  **prior express written consent**. That is why `consent_basis` is a
  required field with no default: if you cannot name the basis, the system
  will not dial.
- The federal DNC scrub fails closed for the same reason: "we didn't have
  the list" is not a defense.
- Several states add their own telemarketing statutes (registration,
  narrower hours, state DNC lists). `config/state_windows.json` narrows hours
  per state; state DNC lists can be merged into the `DNC_LIST_PATH` file.
- Two-party/all-party recording-consent states are listed in
  `config/two_party_states.json`; the shipped list is the commonly cited set
  — **verify with counsel**, and pick `TWO_PARTY_POLICY` deliberately.

This document is engineering documentation, not legal advice. Have counsel
review the calling program (consent bases, hours, disclosures, recording
policy) before Phase 2.
