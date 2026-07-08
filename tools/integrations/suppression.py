"""tools/integrations/suppression.py — the do-not-contact ledger.

One shared, durable set of opted-out email addresses. Written when a seller
replies STOP (dealflow), read by everything that could ever contact them again:
the reply drafter (no reply, no offer) and the lead loader (an opted-out
address must never be re-pushed into a campaign, even if it reappears in a
fresh PropStream export — re-adding an opt-out is a CAN-SPAM violation).

Deliberately stdlib-only and dependency-free so any module can import it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _store() -> Path:
    return Path(os.environ.get(
        "SUPPRESSION_STORE", os.path.expanduser("~/.automaton/suppressed.json")))


def load() -> set[str]:
    try:
        return {str(e).strip().lower() for e in json.loads(_store().read_text())}
    except Exception:
        return set()


def add(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    entries = load()
    entries.add(email)
    store = _store()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(sorted(entries)))


def contains(email: str) -> bool:
    return (email or "").strip().lower() in load()
