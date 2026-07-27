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
        # PropStream pops 'Information' announcement modals at unpredictable
        # moments and they intercept pointer events (observed live, twice at
        # different flow stages). Auto-dismiss: Playwright re-runs this
        # handler whenever the caption is visible during any locator action.
        try:
            self.page.add_locator_handler(
                self.page.locator('[class*="defaultCaption"]').first,
                lambda *_: self.dismiss_modals(),
                no_wait_after=True)
        except Exception:  # noqa: BLE001
            pass

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

    def press_key(self, key: str) -> None:
        self.page.keyboard.press(key)

    def wait_ms(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    def click_any_containing(self, words: list) -> str:
        """JS-click the first visible clickable-ish element (button, menu
        item, link, list item) whose own text contains ALL words. Menus in
        this app render items as non-button elements. Returns matched text."""

        try:
            return self.page.evaluate(
                """(words) => {
                    for (const el of document.querySelectorAll(
                            'button, [role=button], [role=menuitem], a, li, span')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const t = (el.innerText || '').replace(/\\s+/g,' ').trim();
                        if (t && t.length < 60 && words.every(w =>
                                t.toLowerCase().includes(w.toLowerCase()))) {
                            el.click();
                            return t.slice(0, 60);
                        }
                    }
                    return '';
                }""", words) or ""
        except Exception:  # noqa: BLE001
            return ""

    def dismiss_modals(self) -> list:
        """Close any blocking overlay dialog (announcement, promo, the
        single-session Proceed) that would intercept pointer clicks —
        observed live: a Modal-style overlay swallowed the Filters click.
        Clicks the dialog's own affirmative/close control via JS (immune to
        interception); Escape as last resort. Returns what it clicked."""

        done = []
        for _ in range(3):
            try:
                acted = self.page.evaluate(
                    """() => {
                        const all = [...document.querySelectorAll(
                                '[class*="odal"], [role=dialog]')]
                            .filter(el => {
                                const r = el.getBoundingClientRect();
                                return r.width > 0 && r.height > 0;
                            });
                        // prefer the dialog carrying a caption (the
                        // 'Information' popup) over other modal-ish
                        // containers (e.g. the filters panel — whose
                        // chip-remove X buttons must never be clicked);
                        // last match = the most recently mounted dialog
                        const withCap = all.filter(el =>
                            el.querySelector('[class*="defaultCaption"]'));
                        const pool = withCap.length ? withCap : all;
                        const modal = pool[pool.length - 1];
                        if (!modal) return '';
                        const byText = (words) =>
                            [...modal.querySelectorAll(
                                'button, [role=button], a, span, div')]
                            .find(el => {
                                const t = (el.innerText || '')
                                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                                return t && t.length < 30 && words.some(w =>
                                    w.length <= 2 ? t === w : t.includes(w));
                            });
                        const el =
                            byText(['proceed']) ||
                            modal.querySelector(
                                '[aria-label*="lose"], [class*="close" i]') ||
                            byText(['got it', 'no thanks', 'maybe later',
                                    'dismiss', 'skip', 'close', 'ok',
                                    '\\u00d7', 'x']);
                        if (!el) return 'modal-no-button';
                        el.click();
                        return ((el.innerText ||
                                 el.getAttribute('aria-label') ||
                                 el.className || 'clicked')
                                .toString().replace(/\\s+/g, ' ')
                                .trim().slice(0, 40));
                    }""") or ""
            except Exception:  # noqa: BLE001
                acted = ""
            if not acted:
                break
            done.append(acted)
            if acted == "modal-no-button":
                try:
                    self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
                done.append("Escape")
            self.page.wait_for_timeout(800)
        return done

    def click_deep_text(self, words: list) -> str:
        """JS-click the DEEPEST visible element (any tag) whose text contains
        ALL words — catches controls rendered as bare divs, which the
        tag-scoped matchers miss (observed live: the results-view 'Actions'
        dropdown is not a button/span). Among sibling matches, the LAST in
        document order wins (later panels overlay earlier headers)."""

        try:
            return self.page.evaluate(
                """(words) => {
                    const hits = [];
                    for (const el of document.querySelectorAll('body *')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const t = (el.innerText || '').replace(/\\s+/g,' ').trim();
                        if (t && t.length < 40 && words.every(w =>
                                t.toLowerCase().includes(w.toLowerCase())))
                            hits.push(el);
                    }
                    const deep = hits.filter(el =>
                        !hits.some(m => m !== el && el.contains(m)));
                    if (!deep.length) return '';
                    const el = deep[deep.length - 1];
                    el.click();
                    return el.tagName.toLowerCase() + ':' +
                        (el.innerText || '').replace(/\\s+/g,' ').trim().slice(0, 40);
                }""", words) or ""
        except Exception:  # noqa: BLE001
            return ""

    def real_click_css(self, css: str) -> bool:
        """TRUSTED-events click (full pointer sequence) on the first element
        matching ``css``. React dropdown toggles that listen on mousedown
        ignore synthetic el.click() — this is the cure. False on any miss."""

        try:
            self.page.locator(css).first.click(timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def any_text_visible(self, text: str) -> bool:
        """Is any element containing ``text`` visible — via Playwright's
        engine, which pierces shadow DOM that the JS scanners can't see."""

        try:
            loc = self.page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 10)):
                if loc.nth(i).is_visible():
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def real_click_text(self, text: str) -> bool:
        """TRUSTED-events click on the last element containing ``text`` —
        for menu items living in the same synthetic-click-deaf dropdown."""

        try:
            self.page.get_by_text(text, exact=False).last.click(timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def real_click_regex(self, pattern: str) -> bool:
        """TRUSTED-events click matched by regex — for controls whose label
        carries live data (e.g. 'View 4,933 Properties')."""

        import re

        try:
            self.page.get_by_text(re.compile(pattern)).last.click(timeout=6000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def click_nth_deep_text(self, words: list, nth_from_end: int = 0) -> str:
        """Like click_deep_text but clicks the nth match counting from the
        END of document order (0 = last, the click_deep_text default), and
        fires hover events first — dropdown toggles can be hover-driven.
        Returns tag:text ('' when fewer matches exist)."""

        try:
            return self.page.evaluate(
                """([words, nth]) => {
                    const hits = [];
                    for (const el of document.querySelectorAll('body *')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const t = (el.innerText || '').replace(/\\s+/g,' ').trim();
                        if (t && t.length < 40 && words.every(w =>
                                t.toLowerCase().includes(w.toLowerCase())))
                            hits.push(el);
                    }
                    const deep = hits.filter(el =>
                        !hits.some(m => m !== el && el.contains(m)));
                    const el = deep[deep.length - 1 - nth];
                    if (!el) return '';
                    for (const type of ['pointerover', 'mouseover',
                                        'mouseenter'])
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true}));
                    el.click();
                    return el.tagName.toLowerCase() + ':' +
                        (el.innerText || '').replace(/\\s+/g,' ')
                            .trim().slice(0, 40);
                }""", [words, nth_from_end]) or ""
        except Exception:  # noqa: BLE001
            return ""

    def css_probe(self, css: str, limit: int = 15) -> list:
        """Diagnostic: tag/class/text/visibility of every element matching
        ``css`` — for dumping a menu's real items. Never raises."""

        try:
            return self.page.evaluate(
                """([css, limit]) => {
                    const out = [];
                    for (const el of document.querySelectorAll(css)) {
                        const r = el.getBoundingClientRect();
                        out.push({tag: el.tagName.toLowerCase(),
                                  cls: String(el.className || '').slice(0, 60),
                                  text: (el.innerText || '')
                                      .replace(/\\s+/g, ' ').trim().slice(0, 80),
                                  visible: r.width > 0 && r.height > 0});
                        if (out.length >= limit) break;
                    }
                    return out;
                }""", [css, limit])
        except Exception:  # noqa: BLE001
            return []

    def visible_own_texts(self, limit: int = 800) -> list:
        """Every visible element's OWN short text (deduped) — snapshot it
        before and after a click and the difference names whatever UI just
        appeared (menu items, dialogs), wherever they portal to."""

        try:
            return self.page.evaluate(
                """(limit) => {
                    const out = new Set();
                    for (const el of document.querySelectorAll('body *')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const own = [...el.childNodes]
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join(' ')
                            .replace(/\\s+/g, ' ').trim();
                        if (own && own.length < 50) out.add(own);
                        if (out.size >= limit) break;
                    }
                    return [...out];
                }""", limit)
        except Exception:  # noqa: BLE001
            return []

    def watch_dom_start(self) -> None:
        """Record every element mounted (or class/style-toggled) from now
        until watch_dom_stop — catches menus that mount and unmount inside
        a single click, invisible to after-the-fact probes."""

        try:
            self.page.evaluate(
                """() => {
                    window.__added = [];
                    window.__obs = new MutationObserver(ms => {
                        if (window.__added.length > 300) return;
                        for (const m of ms) {
                            for (const n of (m.addedNodes || [])) {
                                if (n.nodeType !== 1) continue;
                                const t = (n.innerText || '')
                                    .replace(/\\s+/g, ' ').trim();
                                if (t) window.__added.push(t.slice(0, 200));
                            }
                            if (m.type === 'attributes') {
                                const t = (m.target.innerText || '')
                                    .replace(/\\s+/g, ' ').trim();
                                if (t && t.length < 120)
                                    window.__added.push('attr:' + t);
                            }
                        }
                    });
                    window.__obs.observe(document.body,
                        {childList: true, subtree: true, attributes: true,
                         attributeFilter: ['class', 'style']});
                }""")
        except Exception:  # noqa: BLE001
            pass

    def watch_dom_stop(self, limit: int = 30) -> list:
        try:
            return self.page.evaluate(
                """(limit) => {
                    if (window.__obs) window.__obs.disconnect();
                    const out = (window.__added || []).slice(0, limit);
                    window.__added = [];
                    return out;
                }""", limit)
        except Exception:  # noqa: BLE001
            return []

    def tag_element_by_text(self, css: str, text: str,
                            attr: str = "data-aria-target") -> bool:
        """Stamp a marker attribute on the element matching ``css`` whose
        own text equals ``text`` — a class selector alone can hit the WRONG
        sibling (observed live: several dropdownToggleBtn elements exist and
        .first was a column-filter toggle, not the Actions menu)."""

        try:
            return bool(self.page.evaluate(
                """([css, text, attr]) => {
                    for (const el of document.querySelectorAll(css)) {
                        const t = (el.innerText || '')
                            .replace(/\\s+/g, ' ').trim();
                        if (t === text) {
                            el.setAttribute(attr, '1');
                            return true;
                        }
                    }
                    return false;
                }""", [css, text, attr]))
        except Exception:  # noqa: BLE001
            return False

    def react_probe(self, css: str) -> list:
        """Read the React props attached to an element, its ancestors and
        children — lists the 'on*' handler names each one really has, ending
        the guessing about which event the component listens to."""

        try:
            return self.page.evaluate(
                """(css) => {
                    const out = [];
                    const el = document.querySelector(css);
                    if (!el) return out;
                    const inspect = (node, label) => {
                        if (!node || node.nodeType !== 1) return;
                        const pk = Object.keys(node).find(k =>
                            k.startsWith('__reactProps$'));
                        const props = pk ? node[pk] : null;
                        out.push({label, tag: node.tagName.toLowerCase(),
                                  cls: String(node.className || '')
                                      .slice(0, 50),
                                  handlers: props ? Object.keys(props)
                                      .filter(k => k.startsWith('on')) : []});
                    };
                    inspect(el, 'self');
                    let p = el.parentElement;
                    for (let i = 0; i < 3 && p; i++, p = p.parentElement)
                        inspect(p, 'parent' + (i + 1));
                    [...el.children].forEach((c, i) =>
                        inspect(c, 'child' + i));
                    return out;
                }""", css)
        except Exception:  # noqa: BLE001
            return []

    def react_invoke(self, css: str, handler: str) -> str:
        """Call a React prop handler DIRECTLY (fake synthetic event) on the
        element or its nearest ancestor that has it — sidesteps all event
        plumbing. Returns what was invoked ('no-handler' / 'no-el')."""

        try:
            return self.page.evaluate(
                """([css, name]) => {
                    let node = document.querySelector(css);
                    if (!node) return 'no-el';
                    for (let i = 0; i < 4 && node; i++,
                         node = node.parentElement) {
                        const pk = Object.keys(node).find(k =>
                            k.startsWith('__reactProps$'));
                        const props = pk ? node[pk] : null;
                        if (props && typeof props[name] === 'function') {
                            const el = document.querySelector(css);
                            const ev = {preventDefault() {},
                                        stopPropagation() {}, persist() {},
                                        nativeEvent: {}, target: el,
                                        currentTarget: node, button: 0};
                            props[name](ev);
                            return 'invoked:' + name + '@' +
                                node.tagName.toLowerCase();
                        }
                    }
                    return 'no-handler';
                }""", [css, handler]) or "no-handler"
        except Exception:  # noqa: BLE001
            return "error"

    def dispatch_pointer_sequence(self, css: str) -> bool:
        """Full SYNTHETIC pointer gesture (pointerdown -> mousedown ->
        pointerup -> mouseup -> click) on an element — components that open
        on mousedown never react to a bare el.click()."""

        try:
            return bool(self.page.evaluate(
                """(css) => {
                    const el = document.querySelector(css);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const opts = {bubbles: true, cancelable: true, button: 0,
                                  clientX: r.x + r.width / 2,
                                  clientY: r.y + r.height / 2};
                    for (const t of ['pointerdown', 'mousedown', 'pointerup',
                                     'mouseup', 'click'])
                        el.dispatchEvent(t.startsWith('pointer')
                            ? new PointerEvent(t, opts)
                            : new MouseEvent(t, opts));
                    return true;
                }""", css))
        except Exception:  # noqa: BLE001
            return False

    def focus_and_key(self, css: str, key: str) -> bool:
        """Focus an element and press a key — dropdowns commonly open on
        Enter/Space/ArrowDown even when pointer handlers misbehave."""

        try:
            self.page.locator(css).first.focus(timeout=3000)
            self.page.keyboard.press(key)
            return True
        except Exception:  # noqa: BLE001
            return False

    def mouse_down_on(self, css: str) -> bool:
        """Press (and HOLD) the mouse on an element's center — for menus
        that only stay open while the button is held."""

        try:
            box = self.page.locator(css).first.bounding_box()
            if not box:
                return False
            self.page.mouse.move(box["x"] + box["width"] / 2,
                                 box["y"] + box["height"] / 2)
            self.page.mouse.down()
            return True
        except Exception:  # noqa: BLE001
            return False

    def mouse_up_neutral(self) -> None:
        """Release the held button over an inert corner (no item clicked)."""

        try:
            self.page.mouse.move(2, 2, steps=4)
            self.page.mouse.up()
        except Exception:  # noqa: BLE001
            pass

    def hold_click_menu_item(self, toggle_css: str, item_text: str) -> bool:
        """press-drag-release: mouse.down on the toggle, drag to the menu
        item while the menu is held open, release ON the item."""

        try:
            if not self.mouse_down_on(toggle_css):
                return False
            self.page.wait_for_timeout(700)
            target = self.page.get_by_text(item_text, exact=False).last
            tb = target.bounding_box()
            if not tb:
                self.mouse_up_neutral()
                return False
            self.page.mouse.move(tb["x"] + tb["width"] / 2,
                                 tb["y"] + tb["height"] / 2, steps=8)
            self.page.wait_for_timeout(250)
            self.page.mouse.up()
            return True
        except Exception:  # noqa: BLE001
            try:
                self.page.mouse.up()
            except Exception:  # noqa: BLE001
                pass
            return False

    def parent_text_of(self, css: str) -> str:
        """innerText of the matched element's PARENT — dumps a dropdown
        container's real items given its toggle's selector."""

        try:
            return self.page.evaluate(
                """(css) => {
                    const el = document.querySelector(css);
                    if (!el || !el.parentElement) return '';
                    return (el.parentElement.innerText || '')
                        .replace(/\\s+/g, ' ').trim().slice(0, 300);
                }""", css) or ""
        except Exception:  # noqa: BLE001
            return ""

    def text_probe(self, needle: str, limit: int = 10) -> list:
        """Diagnostic: every element whose OWN text nodes contain ``needle``,
        with tag/class/visibility — pinpoints how a control really renders
        when all the clickers miss. Never raises."""

        try:
            return self.page.evaluate(
                """([needle, limit]) => {
                    const out = [];
                    for (const el of document.querySelectorAll('body *')) {
                        const own = [...el.childNodes]
                            .filter(n => n.nodeType === 3)
                            .map(n => n.textContent).join(' ')
                            .replace(/\\s+/g, ' ').trim();
                        if (!own.toLowerCase().includes(needle.toLowerCase()))
                            continue;
                        const r = el.getBoundingClientRect();
                        out.push({tag: el.tagName.toLowerCase(),
                                  cls: String(el.className || '').slice(0, 60),
                                  text: own.slice(0, 40),
                                  visible: r.width > 0 && r.height > 0});
                        if (out.length >= limit) break;
                    }
                    return out;
                }""", [needle, limit])
        except Exception:  # noqa: BLE001
            return []

    def download_by_words(self, words: list, dest_dir: str) -> str:
        """Arm the download listener and JS-click the element matching
        ``words`` (the CSV/confirm control in an export menu)."""

        import os

        dest_dir = os.path.expanduser(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        with self.page.expect_download(
                timeout=self.config.download_timeout_ms) as dl:
            matched = self.click_any_containing(words)
            if not matched and self.real_click_text(" ".join(words)):
                matched = "real:" + " ".join(words)
            if not matched:
                raise RuntimeError(f"no element matching {words} to download from")
        download = dl.value
        dest = os.path.join(dest_dir, download.suggested_filename)
        download.save_as(dest)
        return dest

    def click_button_containing(self, words: list) -> str:
        """JS-click the first visible button whose text contains ALL words —
        tolerant of dynamic labels like 'View 28,405 Properties'. Returns the
        matched button's text ('' if none found)."""

        try:
            return self.page.evaluate(
                """(words) => {
                    for (const el of document.querySelectorAll(
                            'button, [role=button]')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const t = (el.innerText || '').trim();
                        if (words.every(w =>
                                t.toLowerCase().includes(w.toLowerCase()))) {
                            el.click();
                            return t.slice(0, 60);
                        }
                    }
                    return '';
                }""", words) or ""
        except Exception:  # noqa: BLE001
            return ""

    def find_and_click_suggestion(self, needle: str) -> list[dict]:
        """Find visible elements (any tag) whose own text contains ``needle``
        — the autocomplete suggestions, wherever and however they render —
        and JS-click the first plausible one. Returns what was seen, so the
        calibration can report the real suggestion DOM."""

        # NOTE: offsetParent is null for position:fixed elements — exactly how
        # dropdown portals render — so visibility is judged by bounding box.
        js_scan = """(args) => {
            const [needle, doClick] = args;
            const hits = [];
            for (const el of document.querySelectorAll('body *')) {
                if (['INPUT','SCRIPT','STYLE'].includes(el.tagName)) continue;
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                if (getComputedStyle(el).visibility === 'hidden') continue;
                const own = [...el.childNodes]
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent).join(' ').trim();
                if (own.includes(needle) && own.length < 80) {
                    hits.push({tag: el.tagName.toLowerCase(),
                               cls: (el.className||'').toString().slice(0,60),
                               text: own.slice(0, 60)});
                    if (doClick) { el.click(); return hits; }
                }
            }
            return hits.slice(0, 10);
        }"""
        seen = self.page.evaluate(js_scan, [needle, False])
        if seen:
            self.page.evaluate(js_scan, [needle, True])
        return seen

    def header_counters(self) -> list[str]:
        """The dashboard counter chips ('123 MLS', '4,567 Vacant', ...) — all
        zeros means no geography is applied; non-zero proves the county
        search actually executed."""

        try:
            return self.page.evaluate(
                """() => [...document.querySelectorAll(
                       '[class*="HeaderSearchItem"]')]
                   .filter(e => e.offsetParent !== null)
                   .map(e => (e.innerText || '').replace(/\\s+/g, ' ').trim())
                   .filter(Boolean).slice(0, 12)""")
        except Exception:  # noqa: BLE001
            return []

    def click_first_checkbox(self) -> str:
        """Click the grid's select-all checkbox (header first). Styled
        checkboxes hide the real input (zero-size, styled overlay on top),
        so NO visibility filter — JS click works on hidden inputs. Returns
        the selector that matched ('' if none)."""

        try:
            return self.page.evaluate(
                """() => {
                    for (const sel of ['th input[type=checkbox]',
                                       'thead input[type=checkbox]',
                                       'input[type=checkbox]',
                                       '[role=checkbox]']) {
                        const el = document.querySelector(sel);
                        if (el) { el.click(); return sel; }
                    }
                    return '';
                }""") or ""
        except Exception:  # noqa: BLE001
            return ""

    def visible_button_texts(self, limit: int = 40) -> list:
        try:
            return self.page.evaluate(
                """(limit) => {
                    const out = [];
                    for (const el of document.querySelectorAll(
                            'button, [role=button]')) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (t && !out.includes(t)) out.push(t.slice(0, 40));
                        if (out.length >= limit) break;
                    }
                    return out;
                }""", limit)
        except Exception:  # noqa: BLE001
            return []

    def click_text(self, text: str) -> None:
        """Click the last visible element with this exact text (section
        headings that aren't buttons; input VALUES are not text nodes, so a
        find-box containing the same string never matches)."""

        self.page.get_by_text(text, exact=True).last.click(timeout=4000)

    def click_after_heading(self, heading: str, target: str) -> None:
        """Click the FIRST ``target`` element that follows ``heading`` in
        document order — disambiguates repeated labels (e.g. the 'Vacant
        Land' classification chip vs the 'Vacant Land' Lead-List row) by
        anchoring on the section heading."""

        anchor = self.page.get_by_text(heading, exact=True).last
        anchor.locator(
            f"xpath=following::*[normalize-space(text())='{target}']"
            "[1]").click(timeout=4000)

    def results_recon(self) -> dict:
        """Targeted results-view snapshot: the count text + only the
        action-relevant controls (buttons, checkboxes), not the whole DOM —
        keeps the calibration output small."""

        out = {"count_text": "", "buttons": [], "checkbox_like": []}
        try:
            out["count_text"] = " ".join(
                (self.page.inner_text("body", timeout=3000) or "").split())
            # keep only the segment likely to hold the result count
            for marker in ("Results", "results", "Properties", "selected"):
                i = out["count_text"].find(marker)
                if i != -1:
                    out["count_text"] = out["count_text"][max(0, i - 40):i + 40]
                    break
        except Exception:  # noqa: BLE001
            pass
        try:
            for b in self.page.get_by_role("button").all()[:60]:
                t = (b.inner_text(timeout=800) or "").strip()
                if t and t not in out["buttons"]:
                    out["buttons"].append(t[:40])
        except Exception:  # noqa: BLE001
            pass
        try:
            out["checkbox_like"] = self.page.evaluate(
                """() => [...document.querySelectorAll(
                     'input[type=checkbox], [role=checkbox], th input, td input')]
                   .filter(e => e.offsetParent !== null)
                   .slice(0, 8)
                   .map(e => ({tag: e.tagName.toLowerCase(),
                               cls: (e.className||'').toString().slice(0,50)}))""")
        except Exception:  # noqa: BLE001
            pass
        return out

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
