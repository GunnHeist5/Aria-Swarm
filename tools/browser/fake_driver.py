"""tools/browser/fake_driver.py — in-memory PageDriver double for tests.

Zero browser, zero network. Records an ordered call log so tests can assert
the flow's structure (e.g. export never happens before a verify gate).
"""

from __future__ import annotations

from .driver import ElementMissing


class FakePageDriver:
    """A scripted PageDriver.

    present_keys: keys that ``is_present``/``wait_for`` treat as visible.
    texts:        key -> string returned by ``get_text``.
    counts:       key -> int returned by ``result_count``.
    download_path: path ``expect_download`` returns.
    fail_on:      keys that raise ElementMissing when addressed (missing DOM).
    """

    def __init__(self, *, present_keys=None, texts=None, counts=None,
                 download_path="/tmp/fake_export.csv", fail_on=None):
        self.present = set(present_keys or ())
        self.texts = dict(texts or {})
        self.counts = dict(counts or {})
        self.download_path = download_path
        self.fail_on = set(fail_on or ())
        self.calls: list[tuple] = []
        self.url = "about:blank"

    def _record(self, *call) -> None:
        self.calls.append(call)

    def _guard(self, key: str) -> None:
        if key in self.fail_on:
            raise ElementMissing(key)

    def goto(self, url: str) -> None:
        self._record("goto", url)
        self.url = url

    def fill(self, key: str, value: str, *, delay_ms: int = 40) -> None:
        self._guard(key)
        # NEVER record the value — a password must not land in the call log.
        self._record("fill", key)

    def click(self, key: str) -> None:
        self._guard(key)
        self._record("click", key)

    def get_text(self, key: str) -> str:
        self._guard(key)
        self._record("get_text", key)
        return self.texts.get(key, "")

    def wait_for(self, key: str, *, state: str = "visible",
                 timeout_ms: int | None = None) -> None:
        self._record("wait_for", key)
        if key not in self.present:
            raise ElementMissing(key)

    def is_present(self, key: str, *, timeout_ms: int = 2000) -> bool:
        self._record("is_present", key)
        return key in self.present

    def result_count(self, key: str) -> int | None:
        self._record("result_count", key)
        return self.counts.get(key)

    def expect_download(self, trigger_key: str, dest_dir: str) -> str:
        self._guard(trigger_key)
        self._record("expect_download", trigger_key)
        return self.download_path

    def current_url(self) -> str:
        return self.url

    def screenshot(self, label: str) -> str:
        self._record("screenshot", label)
        return f"/tmp/{label}.png"

    # -- test helpers --------------------------------------------------------

    def clicked(self, key: str) -> bool:
        return ("click", key) in self.calls

    def index_of(self, verb: str, key: str | None = None) -> int:
        for i, call in enumerate(self.calls):
            if call[0] == verb and (key is None or (len(call) > 1 and call[1] == key)):
                return i
        return -1

    def last_index(self, verb: str) -> int:
        idx = -1
        for i, call in enumerate(self.calls):
            if call[0] == verb:
                idx = i
        return idx
