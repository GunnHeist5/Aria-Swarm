"""tools/browser — the isolated browser runner (Playwright).

The swarm's Web2 bridge: sites with no API get driven through a real browser,
in a runner deliberately decoupled from the pure graph (CLAUDE.md: browser
automation runs in an isolated runner, never inline in a node's invoke).

Its ONLY job today is the PropStream pull — log in, apply vacant-land
filters, skip-trace, export the CSV, drop the file into the leads inbox.
Everything downstream (dedupe, suppression, market routing, Instantly push)
is the already-tested `tools/integrations/lead_intake` pipeline; the runner
ends at "file in inbox". Instantly needs NO browser automation — its API
path already does it.

Design invariants:
  * Flow logic (`propstream.py`) speaks only LOGICAL KEYS against the narrow
    `PageDriver` seam — never a raw selector, never Playwright — so the whole
    flow is offline-testable with a `FakePageDriver`, and DOM drift is a
    config edit (SELECTORS in `config.py` / `browser.yaml`), not a redeploy.
  * First-party, ToS-respecting: PropStream has no public API and its ToU
    forbid scraping, so this is conservative session reuse (one human
    seed-login, reused storage_state, human-cadence pacing, hard row caps)
    and it FAILS CLOSED on any CAPTCHA/2FA/verification wall — freezes and
    fires the HITL webhook, never auto-solves (CLAUDE.md HITL rule).
"""

from .config import DEFAULT_CONFIG, BrowserConfig, config_hash, load_config
from .driver import (
    AuthChallenge,
    BrowserError,
    ElementMissing,
    PageDriver,
    VerificationError,
)

__all__ = [
    "DEFAULT_CONFIG",
    "BrowserConfig",
    "config_hash",
    "load_config",
    "PageDriver",
    "BrowserError",
    "ElementMissing",
    "VerificationError",
    "AuthChallenge",
]
