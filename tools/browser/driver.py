"""tools/browser/driver.py — the PageDriver seam. NO Playwright import here.

The whole harness turns on one move: flow logic never touches Playwright and
never touches a raw selector. It speaks only in LOGICAL KEYS ("login.submit")
against this narrow interface. Production wires it to a real Page (see
``playwright_driver.py``); tests wire it to ``FakePageDriver`` and run with
zero browser and zero network.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class BrowserError(Exception):
    """Base for every browser-runner failure."""


class ElementMissing(BrowserError):
    """A configured logical key never resolved to a visible element."""


class VerificationError(BrowserError):
    """A fail-closed gate did not confirm — never retried, never bypassed."""


class AuthChallenge(BrowserError):
    """A login / 2FA / CAPTCHA / consent wall — route to HITL freeze.

    Raised, never solved: the runner fires the notification webhook and exits
    non-zero (CLAUDE.md: freeze on CAPTCHA/2FA/verification, block until a
    signed resume arrives).
    """


@runtime_checkable
class PageDriver(Protocol):
    """Everything the PropStream flow (or a future deed-pull) needs from a page.

    All element-addressing methods take a LOGICAL KEY, resolved to a real
    selector via the SELECTORS config map in the production driver.
    """

    def goto(self, url: str) -> None: ...

    def fill(self, key: str, value: str, *, delay_ms: int = 40) -> None: ...

    def click(self, key: str) -> None: ...

    def get_text(self, key: str) -> str: ...

    def wait_for(self, key: str, *, state: str = "visible",
                 timeout_ms: int | None = None) -> None: ...

    def is_present(self, key: str, *, timeout_ms: int = 2000) -> bool: ...

    def result_count(self, key: str) -> int | None:
        """Parse the live filter result-count chip to an int (None if absent)."""
        ...

    def expect_download(self, trigger_key: str, dest_dir: str) -> str:
        """Arm the download listener, click ``trigger_key``, return saved path."""
        ...

    def current_url(self) -> str: ...

    def screenshot(self, label: str) -> str:
        """Save a screenshot; return its path. Labels must never hold secrets."""
        ...
