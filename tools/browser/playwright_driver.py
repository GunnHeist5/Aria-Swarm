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
        loc = self._loc(key)
        loc.fill("", timeout=self.config.default_timeout_ms)   # focus + clear
        loc.press_sequentially(value, delay=delay_ms)          # human-cadence typing

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

    def type_keys(self, text: str, delay_ms: int = 40) -> None:
        """Raw keyboard typing into whatever is focused — the escape hatch for
        SPA inputs that re-render mid-interaction (their ids regenerate, which
        detaches locators between the click and the fill)."""

        self.page.keyboard.type(text, delay=delay_ms)

    def fill_labeled_range(self, label: str, min_value=None, max_value=None) -> None:
        """Fill the Min/Max inputs belonging to a labeled filter section.

        The panel's input ids are random UUIDs, so the anchor is the section's
        visible label text; the section's own Min/Max are the FIRST such
        inputs following it in document order. Values are typed (keyboard) so
        the SPA's change handlers fire."""

        anchor = self.page.get_by_text(label, exact=True).last
        for placeholder, value in (("Min", min_value), ("Max", max_value)):
            if value is None:
                continue
            target = anchor.locator(
                f"xpath=following::input[@placeholder='{placeholder}'][1]")
            target.click(timeout=4000)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            self.page.keyboard.type(str(value), delay=30)

    def click_text(self, text: str) -> None:
        """Click the last visible element with this exact text (section
        headings that aren't buttons; input VALUES are not text nodes, so a
        find-box containing the same string never matches)."""

        self.page.get_by_text(text, exact=True).last.click(timeout=4000)

    def page_text(self, limit: int = 1500) -> str:
        try:
            return " ".join(
                (self.page.inner_text("body", timeout=3000) or "").split())[:limit]
        except Exception:  # noqa: BLE001
            return ""

    def force_fill(self, key: str, text: str) -> None:
        """fill() with a keyboard fallback: SPA dropdowns/overlays can make
        the strict actionability wait time out even though the element is
        right there — focus() + keyboard typing has far weaker preconditions."""

        loc = self._loc(key).first
        try:
            loc.fill(text, timeout=4000)
        except Exception:  # noqa: BLE001
            loc.focus(timeout=4000)
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")
            if text:
                self.page.keyboard.type(text, delay=40)

    def dom_inventory(self, limit: int = 40) -> list[dict]:
        """Visible inputs/buttons with their identifying attributes — the raw
        material for writing browser.yaml selectors. Never raises."""

        try:
            return self.page.evaluate(
                """(limit) => [...document.querySelectorAll(
                       'input, button, select, textarea, [role=button], a[href]')]
                   .filter(el => el.offsetParent !== null)
                   .slice(0, limit)
                   .map(el => ({
                       tag: el.tagName.toLowerCase(),
                       type: el.type || null,
                       name: el.name || null,
                       id: el.id || null,
                       placeholder: el.placeholder || null,
                       aria: el.getAttribute('aria-label'),
                       testid: el.getAttribute('data-testid'),
                       cls: (el.className || '').toString().slice(0, 60),
                       text: (el.innerText || el.value || '').trim().slice(0, 40),
                   }))""", limit)
        except Exception:  # noqa: BLE001
            return []

    def page_summary(self) -> dict:
        """Where are we? url + title + a visible-text excerpt, for calibration
        diagnostics (an all-MISSING sweep usually means wrong page, not 37
        wrong selectors). Never raises."""

        out = {"url": "", "title": "", "text": ""}
        try:
            out["url"] = self.page.url
            out["title"] = self.page.title()
            out["text"] = " ".join(
                (self.page.inner_text("body", timeout=3000) or "").split())[:500]
        except Exception:  # noqa: BLE001
            pass
        return out

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
