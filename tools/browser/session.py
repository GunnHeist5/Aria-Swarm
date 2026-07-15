"""tools/browser/session.py — Playwright context lifecycle + credentials + HITL.

Launches a persistent Chromium context (warmed profile + reused storage_state)
so scheduled runs never re-type credentials, seeds that session with a ONE-TIME
human headful login, and turns any auth challenge into a HITL freeze + webhook
(CLAUDE.md: freeze on CAPTCHA/2FA/verification, never auto-solve).

Playwright is imported lazily so importing this module (e.g. for its env-name
constants) doesn't require Playwright installed.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from .config import BrowserConfig, DEFAULT_CONFIG, load_selectors
from .driver import AuthChallenge, BrowserError


def resolve_credentials(config: BrowserConfig = DEFAULT_CONFIG) -> tuple[str, str]:
    """(username, password) from the environment via get_secret. Never logged."""

    from tools.integrations.secrets import get_secret

    user = get_secret(config.username_env, required=True)
    pw = get_secret(config.password_env, required=True)
    return user, pw


def _ensure_dirs(config: BrowserConfig) -> None:
    for raw in (config.download_dir, config.artifact_dir):
        Path(raw).expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
    state = Path(config.storage_state).expanduser()
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def real_driver(config: BrowserConfig = DEFAULT_CONFIG, *, headless: bool | None = None,
                selectors_path: str | None = None):
    """Yield a live PlaywrightPageDriver, session loaded from storage_state.json.

    The session is a single PORTABLE file (seed on a laptop, ship it here). The
    existence check runs BEFORE importing Playwright, so a missing session fails
    with a clear message even where Playwright isn't installed.
    """

    _ensure_dirs(config)
    state_path = Path(config.storage_state).expanduser()
    if not state_path.exists():
        raise BrowserError(
            f"no PropStream session at {state_path} — seed it on a machine with "
            "a screen (see DEPLOY.md) and copy storage_state.json here")
    selectors = load_selectors(selectors_path)

    from playwright.sync_api import sync_playwright

    from .playwright_driver import PlaywrightPageDriver

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.headless if headless is None else headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(state_path), accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(config.default_timeout_ms)
        try:
            yield PlaywrightPageDriver(page, selectors, config)
        finally:
            try:  # refresh the rolling session so it doesn't age out
                context.storage_state(path=str(state_path))
                os.chmod(state_path, 0o600)
            except Exception:  # noqa: BLE001
                pass
            context.close()
            browser.close()


def seed_login(config: BrowserConfig = DEFAULT_CONFIG) -> None:
    """One-time HEADFUL human login that captures storage_state for reuse.

    Run once on the VPS (under xvfb) or a laptop; every scheduled run afterward
    reuses the captured session and never types credentials.
    """

    from playwright.sync_api import sync_playwright

    _ensure_dirs(config)
    state_path = Path(config.storage_state).expanduser()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(config.login_url)
        print("\nA browser window is open. Log in to PropStream fully — accept "
              "the cookie banner, complete any 2FA — until you see your "
              "dashboard.")
        input("Then press Enter here to capture the session… ")
        context.storage_state(path=str(state_path))
        os.chmod(state_path, 0o600)
        context.close()
        browser.close()
    print(f"Session captured -> {state_path} (0600). Ship this file to the VPS; "
          "scheduled pulls then run unattended.")


def freeze_hitl(reason: str, config: BrowserConfig = DEFAULT_CONFIG, *,
                artifact: str | None = None, notifier=None) -> None:
    """Fire the operator webhook for a browser challenge — never auto-solve.

    Best-effort Telegram alert with a remote-view/action line, matching the
    swarm's HITL freeze convention. Secrets are read locally, never logged.
    """

    try:
        from tools.integrations.secrets import get_secret

        token = get_secret("MUFFIN_TELEGRAM_TOKEN")
        chat_id = get_secret("JUSTIN_TELEGRAM_CHAT_ID")
    except Exception:  # noqa: BLE001
        token = chat_id = None
    msg = ("⛔ PropStream pull frozen — human action needed.\n"
           f"reason: {reason}\n"
           + (f"screenshot: {artifact}\n" if artifact else "")
           + "Fix: re-run `python -m tools.browser.cli seed-login` to refresh "
           "the session, then the timer resumes.")
    if token and chat_id:
        if notifier is None:
            from tools.dealflow import notify as notifier
        try:
            notifier.send_text(msg, token=token, chat_id=chat_id, parse_mode=None)
        except Exception:  # noqa: BLE001
            pass
    print(msg)
