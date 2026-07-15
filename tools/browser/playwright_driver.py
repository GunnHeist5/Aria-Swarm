"""tools/browser/playwright_driver.py — the real PageDriver (wraps a Page).

The ONLY file that imports Playwright, and it does so lazily (inside methods
that need types) so importing tools.browser never drags Playwright into the
rest of the swarm. Resolves a logical key to a Locator via the SELECTORS spec,
priority: role -> label -> text -> testid -> placeholder -> css.
"""

from __future__ import annotations

from .config import BrowserConfig, DEFAULT_CONFIG
from .driver import ElementMissing


def _resolve(page, spec: dict, subs: dict):
    """spec dict -> Playwright Locator. `subs` fills {placeholders} in names."""

    by = spec.get("by")
    def fmt(v):
        return v.format(**subs) if isinstance(v, str) else v

    if by == "role":
        return page.get_by_role(spec["role"], name=fmt(spec.get("name")))
    if by == "label":
        return page.get_by_label(fmt(spec["text"]))
    if by == "text":
        return page.get_by_text(fmt(spec["text"]))
    if by == "placeholder":
        return page.get_by_placeholder(fmt(spec["text"]))
    if by == "testid":
        return page.get_by_test_id(fmt(spec["id"]))
    if by == "css":
        return page.locator(spec["css"])
    raise ValueError(f"unknown selector strategy: {by!r}")


class PlaywrightPageDriver:
    """PageDriver backed by a live Playwright Page + a resolved selector map."""

    def __init__(self, page, selectors: dict, config: BrowserConfig = DEFAULT_CONFIG):
        self.page = page
        self.selectors = selectors
        self.config = config
        self._subs = {"property_class": config.property_class}

    def _loc(self, key: str):
        spec = self.selectors.get(key)
        if spec is None:
            raise ElementMissing(f"no selector configured for '{key}'")
        return _resolve(self.page, spec, self._subs)

    def goto(self, url: str) -> None:
        self.page.goto(url, timeout=self.config.nav_timeout_ms,
                       wait_until="domcontentloaded")

    def fill(self, key: str, value: str, *, delay_ms: int = 40) -> None:
        self._loc(key).fill("", timeout=self.config.default_timeout_ms)
        self._loc(key).type(value, delay=delay_ms)

    def click(self, key: str) -> None:
        self._loc(key).click(timeout=self.config.default_timeout_ms)

    def get_text(self, key: str) -> str:
        return self._loc(key).inner_text(timeout=self.config.default_timeout_ms)

    def wait_for(self, key: str, *, state: str = "visible",
                 timeout_ms: int | None = None) -> None:
        self._loc(key).wait_for(
            state=state, timeout=timeout_ms or self.config.default_timeout_ms)

    def is_present(self, key: str, *, timeout_ms: int = 2000) -> bool:
        try:
            self._loc(key).first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001 — absence is a normal answer, not an error
            return False

    def result_count(self, key: str) -> int | None:
        import re

        try:
            text = self.get_text(key)
        except Exception:  # noqa: BLE001
            return None
        digits = re.sub(r"[^\d]", "", text or "")
        return int(digits) if digits else None

    def expect_download(self, trigger_key: str, dest_dir: str) -> str:
        import os

        with self.page.expect_download(
                timeout=self.config.download_timeout_ms) as dl:
            self._loc(trigger_key).click(timeout=self.config.default_timeout_ms)
        download = dl.value
        dest = os.path.join(dest_dir, download.suggested_filename)
        download.save_as(dest)
        return dest

    def current_url(self) -> str:
        return self.page.url

    def screenshot(self, label: str) -> str:
        import os

        os.makedirs(os.path.expanduser(self.config.artifact_dir), exist_ok=True)
        path = os.path.join(os.path.expanduser(self.config.artifact_dir),
                            f"{label}.png")
        try:
            self.page.screenshot(path=path, full_page=True)
        except Exception:  # noqa: BLE001
            return ""
        return path
