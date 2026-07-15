#!/usr/bin/env python3
"""Standalone PropStream session seeder — run on YOUR LAPTOP, not the VPS.

This is the one step that needs a browser you can see. It has NO dependency on
the rest of the swarm — download just this file, and on a machine with a screen:

    pip install playwright
    playwright install chromium
    python seed_propstream.py

A Chrome window opens on login.propstream.com. Log in fully — accept the cookie
banner, complete any 2FA — until you see your dashboard, then press Enter in the
terminal. It writes ``propstream_storage_state.json`` next to this script.

Then copy that ONE file to the VPS (from your laptop terminal):

    scp propstream_storage_state.json \
        root@YOUR_VPS:/root/.automaton/propstream/storage_state.json

On the VPS: ``chmod 600 /root/.automaton/propstream/storage_state.json``.
That's the whole session hand-off — scheduled pulls then run unattended.
"""

from pathlib import Path

OUT = str(Path(__file__).resolve().parent / "propstream_storage_state.json")
LOGIN_URL = "https://login.propstream.com/"


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.goto(LOGIN_URL)
        print("\nA Chrome window is open. Log in to PropStream completely — "
              "accept cookies, finish any 2FA — until you see your dashboard.")
        input("Then press Enter here to capture the session… ")
        context.storage_state(path=OUT)
        context.close()
        browser.close()
    print(f"\nWrote {OUT}")
    print("Now copy it to the VPS at "
          "/root/.automaton/propstream/storage_state.json (then chmod 600).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
