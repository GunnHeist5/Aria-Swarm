"""tools/wholesaling/closing.py — nationwide title/attorney closing router.

Closing is state-regulated: most states close through title/escrow companies,
a bloc closes through real-estate attorneys, and wholesaling legality itself
varies (license/disclosure rules). At scale this must be a routing table the
swarm enforces, not tribal knowledge:

  * A REVIEWED state routes a signed deal to its vetted closer (national
    primary + local backup — the two-provider rule per market).
  * An UNREVIEWED or unknown state FAILS CLOSED: the router says
    "needs_review" and the graph escalates to a human. The system never
    invents a vendor or assumes a state's law.

The compliance notes here are operating flags, not legal advice — each state's
row is filled in only after a human verifies current law for that market.
Runtime overrides live in ``operational_flags["closing_rules_overrides"]`` so
approving a new market is a one-row update, no code deploy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StateRule:
    """One state's closing + wholesaling posture."""

    closer_type: str                    # "title_company" | "attorney"
    wholesaling_notes: str = ""         # compliance flags a human verified
    reviewed: bool = False              # True only after legality + vendor vet
    primary_vendor: str | None = None
    backup_vendor: str | None = None


# Baseline table. Only TX is fully reviewed (the live market). The
# attorney-close bloc and known restrictive-wholesaling states are seeded
# unreviewed so entering them surfaces the right warning instead of silence.
STATE_RULES: dict[str, StateRule] = {
    # --- Active, reviewed markets ---
    "TX": StateRule(
        closer_type="title_company",
        wholesaling_notes=(
            "SB 2212: must disclose sale of equitable interest, not the "
            "property itself. Title premiums are state-promulgated (identical "
            "everywhere) — vet on escrow fee + speed, not price."
        ),
        reviewed=True,
        primary_vendor="CLOSED Title (Texas entities) — closedtitle.com",
        backup_vendor="American Title of Houston — 2000 Bering Dr, 713-965-9777",
    ),

    # --- Attorney-close states (closer is an attorney, not a title co.) ---
    **{
        code: StateRule(
            closer_type="attorney",
            wholesaling_notes="Attorney-close state: engage a RE attorney, not a title company.",
        )
        for code in ("GA", "SC", "NC", "MA", "CT", "DE", "WV", "VT")
    },

    # --- Known restrictive-wholesaling states (verify before ANY outreach) ---
    "OK": StateRule(
        closer_type="title_company",
        wholesaling_notes="Predatory Real Estate Wholesaler Act: license/registration rules — verify before operating.",
    ),
    "IL": StateRule(
        closer_type="title_company",
        wholesaling_notes="License required beyond minimal deal count — verify current threshold before operating.",
    ),
}
# SC is both attorney-close AND restrictive — strengthen its note.
STATE_RULES["SC"] = StateRule(
    closer_type="attorney",
    wholesaling_notes=(
        "Attorney-close state AND restrictive wholesaling rules — full legal "
        "review before any outreach."
    ),
)


def route_closing(state_code: str, overrides: dict | None = None) -> dict:
    """Route a deal's closing by property state. Fails closed on the unknown.

    ``overrides`` (from ``operational_flags['closing_rules_overrides']``) maps
    state code -> StateRule-shaped dict and takes precedence over the baseline,
    so a human can approve a new market at runtime without a deploy.
    """

    code = (state_code or "").strip().upper()
    rule = None
    if overrides and code in overrides:
        rule = StateRule(**overrides[code])
    elif code in STATE_RULES:
        rule = STATE_RULES[code]

    if rule is None:
        return {
            "status": "needs_review",
            "state": code or "??",
            "closer_type": None,
            "notes": "no rule for this state — legality + closer must be reviewed before operating",
        }
    if not rule.reviewed:
        return {
            "status": "needs_review",
            "state": code,
            "closer_type": rule.closer_type,
            "notes": rule.wholesaling_notes,
        }
    return {
        "status": "routed",
        "state": code,
        "closer_type": rule.closer_type,
        "vendor": rule.primary_vendor,
        "backup": rule.backup_vendor,
        "notes": rule.wholesaling_notes,
    }


def review_state(state_code: str, **rule_fields) -> dict:
    """Build an override row for a newly-approved market.

    Returns ``{code: rule_dict}`` ready to merge into
    ``operational_flags["closing_rules_overrides"]``. ``reviewed`` defaults
    True — calling this IS the human approval act.
    """

    rule_fields.setdefault("reviewed", True)
    rule = StateRule(**rule_fields)
    return {(state_code or "").strip().upper(): asdict(rule)}
