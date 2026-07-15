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
from .driver import AuthChallenge


def resolve_credentials(config: BrowserConfig = DEFAULT_CONFIG) -> tuple[str, str]:
    """(username, password) from the environment via get_secret. Never logged."""

    from tools.integrations.secrets import get_secret

    user = get_secret(config.username_env, required=True)
    pw = get_secret(config.password_env, required=True)
    return user, pw


def _ensure_dirs(config: BrowserConfig) -> None:
    for raw, mode in ((config.user_data_dir, 0o700),
                      (config.download_dir, 0o700),
                      (config.artifact_dir, 0o700)):
        p = Path(raw).expanduser()
        p.mkdir(parents=True, exist_ok=True, mode=mode)
    state = Path(config.storage_state).expanduser()
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def real_driver(config: BrowserConfig = DEFAULT_CONFIG, *, headless: bool | None = None,
                selectors_path: str | None = None):
    """Yield a live PlaywrightPageDriver in a persistent, authenticated context."""

    from playwright.sync_api import sync_playwright

    from .playwright_driver import PlaywrightPageDriver

    _ensure_dirs(config)
    selectors = load_selectors(selectors_path)
    state_path = Path(config.storage_state).expanduser()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(config.user_data_dir).expanduser()),
            headless=config.headless if headless is None else headless,
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
            storage_state=str(state_path) if state_path.exists() else None,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(config.default_timeout_ms)
        try:
            yield PlaywrightPageDriver(page, selectors, config)
        finally:
            try:
                context.storage_state(path=str(state_path))
                os.chmod(state_path, 0o600)
            except Exception:  # noqa: BLE001
                pass
            context.close()


def seed_login(config: BrowserConfig = DEFAULT_CONFIG) -> None:
    """One-time HEADFUL human login that captures storage_state for reuse.

    Run once on the VPS (under xvfb) or a laptop; every scheduled run afterward
    reuses the captured session and never types credentials.
    """

    from playwright.sync_api import sync_playwright

    _ensure_dirs(config)
    state_path = Path(config.storage_state).expanduser()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(Path(config.user_data_dir).expanduser()),
            headless=False,
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(config.login_url)
        print("\nA browser window is open. Log in to PropStream fully — accept "
              "the cookie banner, complete any 2FA — until you see your "
              "dashboard.")
        input("Then press Enter here to capture the session… ")
        context.storage_state(path=str(state_path))
        os.chmod(state_path, 0o600)
        context.close()
    print(f"Session captured -> {state_path} (0600). Scheduled pulls can now "
          "run unattended.")


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
