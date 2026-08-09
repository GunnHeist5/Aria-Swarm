"""Federal DNC scrub sources.

The DNC check is a hard block with a fail-closed twist: the ABSENCE of a DNC
source is itself a block (DNC_SOURCE_MISSING), because "we couldn't check" and
"we checked and they're listed" must both stop the dial. `MissingDncChecker`
exists so the gate can distinguish "no source configured" from "source says
clear" without special-casing None.

The file format matches what DNC list exports actually look like: one number
per line (10-digit or E.164), '#' comments, blank lines. Matching is on the
last 10 digits so formatting differences between the list and our E.164
storage can never cause a miss.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..logging_utils import get_logger
from ..models import DncChecker
from .phones import last_ten_digits

log = get_logger(__name__)

# Federal DNC files run to millions of lines; cache the parsed set on
# (path, mtime, size) so long-running workers don't re-read it per dial while
# still picking up a refreshed download.
_cache: dict[str, tuple[tuple[float, int], frozenset[str]]] = {}


class MissingDncChecker:
    """No DNC source configured. available() is False; the gate blocks."""

    def available(self) -> bool:
        return False

    def is_listed(self, phone_e164: str) -> bool:
        # Never consulted by a correct caller (available() gates it), but if
        # someone does ask, the fail-closed answer is "listed".
        return True


class FileDncChecker:
    """DNC list loaded from a local file (one number per line)."""

    def __init__(self, path: Path):
        self._path = path
        self._numbers: frozenset[str] | None = self._load()

    def _load(self) -> frozenset[str] | None:
        try:
            stat = self._path.stat()
        except OSError:
            log.error("DNC list configured but missing/unreadable: %s", self._path)
            return None
        key = str(self._path)
        cache_tag = (stat.st_mtime, stat.st_size)
        cached = _cache.get(key)
        if cached and cached[0] == cache_tag:
            return cached[1]

        numbers: set[str] = set()
        skipped = 0
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # '#' starts a comment, full-line or trailing.
                    entry = line.split("#", 1)[0].strip()
                    if not entry:
                        continue
                    national = last_ten_digits(entry)
                    if national is None:
                        skipped += 1
                        continue
                    numbers.add(national)
        except OSError:
            log.error("DNC list unreadable mid-parse: %s", self._path)
            return None
        if skipped:
            log.warning("DNC list %s: skipped %d unparseable lines", self._path, skipped)
        parsed = frozenset(numbers)
        _cache[key] = (cache_tag, parsed)
        log.info("DNC list loaded: %d numbers from %s", len(parsed), self._path)
        return parsed

    def available(self) -> bool:
        return self._numbers is not None

    def is_listed(self, phone_e164: str) -> bool:
        if self._numbers is None:
            return True  # unreadable source: fail closed
        national = last_ten_digits(phone_e164)
        if national is None:
            return True  # unmatchable number: fail closed
        return national in self._numbers


def get_dnc_checker(cfg: Settings) -> DncChecker:
    """The configured DNC source; MissingDncChecker when none is set."""
    if cfg.dnc_list_path is None:
        return MissingDncChecker()
    return FileDncChecker(cfg.resolve_path(cfg.dnc_list_path))
