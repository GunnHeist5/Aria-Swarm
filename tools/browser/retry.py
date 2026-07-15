"""tools/browser/retry.py — per-step retry + screenshot-on-failure.

Wraps a single browser step so a transient miss (SPA still rendering) gets a
bounded retry, and a hard failure leaves a screenshot behind for the operator.
Verification and auth-challenge failures are NEVER retried — a wrong page
state must not be papered over by trying again.
"""

from __future__ import annotations

import time

from .driver import AuthChallenge, PageDriver, VerificationError


def step(driver: PageDriver, label: str, action, *, attempts: int = 3,
         delays=(0.5, 2.0, 5.0), sleep=time.sleep):
    """Run ``action()`` with bounded retries; screenshot + re-raise on failure."""

    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return action()
        except (VerificationError, AuthChallenge):
            raise  # never retry a bad-state signal
        except Exception as exc:  # noqa: BLE001 — transient DOM/timeouts
            last = exc
            if i + 1 < attempts:
                sleep(delays[min(i, len(delays) - 1)])
    try:
        driver.screenshot(f"fail-{label}")
    except Exception:  # noqa: BLE001 — screenshotting must not mask the real error
        pass
    raise last if last else RuntimeError(f"step {label} failed")
