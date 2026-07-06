"""tools/muffin_bridge.py — the Muffin→swarm seam.

Muffin (the operator) and the swarm (the manager/brain) run on the same VPS but
under **different Python venvs** — Muffin under the hermes venv, the swarm under
its own ``.venv``. So this bridge is deliberately **stdlib-only**: Muffin can
``import`` it directly, and it shells out to the *swarm's* interpreter to fire an
event. The swarm's durable snapshot is the ledger; every firing is one discrete,
persisted cycle.

Safety property (non-negotiable): **a swarm hiccup must never break Muffin's
operator loop.** Every call is fire-and-forget and catches everything — a failed
notify returns ``False`` and is logged, never raised.

Muffin-side usage (add to e.g. check_seller_responses.py):

    import sys; sys.path.append("/root/Aria-Swarm")
    from tools.muffin_bridge import notify_seller_reply, looks_automated
    if not looks_automated(from_address):
        notify_seller_reply(lead_id, reply_body, from_address, detach=True)

Or shell-only (cron):

    python -m tools.muffin_bridge seller-reply --lead-id L1 --reply "..." --contact "a@b.c"
    python -m tools.muffin_bridge deal-closed  --deal-id D1 --assignment-fee 12000
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

# --- Where the swarm lives (override via env; sane VPS defaults) ---
SWARM_DIR = os.environ.get("SWARM_DIR", "/root/Aria-Swarm")
SWARM_PYTHON = os.environ.get("SWARM_PYTHON", os.path.join(SWARM_DIR, ".venv/bin/python"))
SWARM_MAIN = os.environ.get("SWARM_MAIN", os.path.join(SWARM_DIR, "main.py"))
DETACH_LOG = os.environ.get(
    "MUFFIN_BRIDGE_LOG", os.path.expanduser("~/.automaton/muffin_bridge.log")
)

DEFAULT_TIMEOUT_S = 120

# Sender-address markers that mean "automated / no-reply" — mail from these
# should never wake the qualifier (newsletters, bounces, notifications).
_AUTOMATED_MARKERS = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "notification", "notifications", "bounce", "mailer@",
    "automated@", "@google.com", "@googlemail.com", "workspace-noreply",
)


def looks_automated(address: str) -> bool:
    """True if an email address looks like an automated/no-reply sender.

    Muffin's inbox filter only excludes self-mail, so it still admits newsletters
    and bounces; gate the swarm firing on this so the qualifier only sees mail
    that plausibly came from a human seller.
    """

    a = (address or "").lower()
    return any(marker in a for marker in _AUTOMATED_MARKERS)


def _log(msg: str) -> None:
    """One-line diagnostic to stderr — Muffin's job logs capture it."""

    print(f"[muffin_bridge] {msg}", file=sys.stderr)


def fire_event(event_type: str, payload: dict, *, timeout: int = DEFAULT_TIMEOUT_S,
               detach: bool = False) -> bool:
    """Fire one swarm event. NEVER raises into Muffin.

    ``detach=True`` is true fire-and-forget: spawn the swarm cycle in its own
    session and return immediately (True = queued), so Muffin's loop never blocks
    on a full LLM cycle — right for firing inside a per-message loop. The swarm
    serializes on its own state lock, so concurrent fires can't corrupt state.

    ``detach=False`` (default) waits for the cycle and returns True on a clean
    (exit 0) run, False otherwise — right for one-shot cron steps that want a
    real exit code. Either way the swarm persists its own state; a dropped notify
    simply isn't recorded.
    """

    try:
        payload_json = json.dumps(payload, default=str)
    except (TypeError, ValueError) as exc:
        _log(f"payload not serializable for {event_type}: {exc}")
        return False

    cmd = [SWARM_PYTHON, SWARM_MAIN, "--event", event_type, "--payload", payload_json]

    if detach:
        try:
            out = open(DETACH_LOG, "a")  # noqa: SIM115 — child inherits this fd
        except Exception:
            out = subprocess.DEVNULL
        try:
            subprocess.Popen(
                cmd, cwd=SWARM_DIR, stdout=out, stderr=out,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
            return True
        except FileNotFoundError:
            _log(f"swarm python not found at {SWARM_PYTHON!r} — is the venv built?")
            return False
        except Exception as exc:  # never let Muffin's loop die on our account
            _log(f"swarm event {event_type} spawn failed: {exc}")
            return False
        finally:
            if out is not subprocess.DEVNULL:
                try:
                    out.close()
                except Exception:
                    pass

    try:
        result = subprocess.run(
            cmd, timeout=timeout, cwd=SWARM_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except FileNotFoundError:
        _log(f"swarm python not found at {SWARM_PYTHON!r} — is the venv built?")
        return False
    except subprocess.TimeoutExpired:
        _log(f"swarm event {event_type} timed out after {timeout}s")
        return False
    except Exception as exc:  # never let Muffin's loop die on our account
        _log(f"swarm event {event_type} failed: {exc}")
        return False

    if result.returncode == 0:
        return True
    # Exit 2 = swarm froze for HITL (expected, e.g. offer_accepted). Note it,
    # don't treat as a bridge failure.
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    _log(f"swarm event {event_type} exit {result.returncode}: {tail[-1] if tail else ''}")
    return False


# ---------------------------------------------------------------------------
# Typed helpers — the surface Muffin calls
# ---------------------------------------------------------------------------


def notify_seller_reply(lead_id: str, reply_text: str, contact: str, *,
                        detach: bool = False, **extra) -> bool:
    """A seller replied — the swarm's qualifier triages it.

    Pass ``detach=True`` when calling inside a per-message loop so Muffin doesn't
    block on each cycle.
    """

    return fire_event("seller_reply", {
        "lead_id": lead_id, "reply_text": reply_text, "contact": contact, **extra,
    }, detach=detach)


def notify_deal_closed(deal_id: str, assignment_fee_usd: float, *,
                       swarm_cut_pct: float = 0.10) -> bool:
    """A deal closed — book the swarm's cut (idempotent per deal_id)."""

    return fire_event("deal_closed", {
        "deal_id": deal_id, "assignment_fee_usd": assignment_fee_usd,
        "swarm_cut_pct": swarm_cut_pct,
    })


def notify_leads_synced(count: int, **extra) -> bool:
    """A lead sync ran — pipeline bookkeeping."""

    return fire_event("new_leads_synced", {"count": count, **extra})


def notify_contract_signed(deal_id: str, **fields) -> bool:
    """A contract was signed — open the 10-day disposition clock."""

    return fire_event("contract_signed", {"deal_id": deal_id, **fields})


def notify_buyer_confirmed(deal_id: str, buyer: str, earnest_posted: bool) -> bool:
    """A dispo buyer is locked — stop the clock (earnest required)."""

    return fire_event("buyer_confirmed", {
        "deal_id": deal_id, "buyer": buyer, "earnest_posted": earnest_posted,
    })


# ---------------------------------------------------------------------------
# CLI mirror (shell-only cron steps)
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.muffin_bridge",
        description="Fire a swarm event from Muffin (fire-and-forget).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seller-reply")
    p.add_argument("--lead-id", required=True)
    p.add_argument("--reply", required=True)
    p.add_argument("--contact", required=True)

    p = sub.add_parser("deal-closed")
    p.add_argument("--deal-id", required=True)
    p.add_argument("--assignment-fee", type=float, required=True)
    p.add_argument("--cut-pct", type=float, default=0.10)

    p = sub.add_parser("leads-synced")
    p.add_argument("--count", type=int, required=True)

    p = sub.add_parser("contract-signed")
    p.add_argument("--deal-id", required=True)
    p.add_argument("--state", default=None)

    p = sub.add_parser("buyer-confirmed")
    p.add_argument("--deal-id", required=True)
    p.add_argument("--buyer", required=True)
    p.add_argument("--earnest-posted", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "seller-reply":
        ok = notify_seller_reply(args.lead_id, args.reply, args.contact)
    elif args.cmd == "deal-closed":
        ok = notify_deal_closed(args.deal_id, args.assignment_fee, swarm_cut_pct=args.cut_pct)
    elif args.cmd == "leads-synced":
        ok = notify_leads_synced(args.count)
    elif args.cmd == "contract-signed":
        fields = {"state": args.state} if args.state else {}
        ok = notify_contract_signed(args.deal_id, **fields)
    elif args.cmd == "buyer-confirmed":
        ok = notify_buyer_confirmed(args.deal_id, args.buyer, args.earnest_posted)
    else:  # pragma: no cover - argparse requires a subcommand
        parser.error("unknown command")

    print(f"[muffin_bridge] {args.cmd}: {'ok' if ok else 'not recorded (see log)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
