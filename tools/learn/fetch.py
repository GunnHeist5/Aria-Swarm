"""tools/learn/fetch.py — pull the text behind a link (seam + stub).

``fetch_content`` is the ingestion boundary. The default fetcher is an offline
**stub** (no network) so the whole learning pipeline is testable here. The live
fetchers — a YouTube transcript extractor and an article/readability extractor —
are VPS adapters injected via ``fetcher``; they land behind this seam because
they need network and because the text they return is **untrusted input** that
must never bypass the Red Queen / autonomy gates downstream.
"""

from __future__ import annotations

from urllib.parse import urlparse


def _source_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "youtu" in host:
        return "youtube"
    if host:
        return "web"
    return "unknown"


def _stub_fetcher(url: str) -> dict:
    """Offline stand-in: returns a canned transcript, makes no network call."""

    return {
        "title": f"[stub] content for {url}",
        "text": (
            "This is stubbed transcript text. The live fetcher returns the real "
            "YouTube transcript or article body here."
        ),
        "source": _source_of(url),
    }


def fetch_content(url: str, *, fetcher=None) -> dict:
    """Return ``{title, text, source, url}`` for a link.

    ``fetcher`` (injected on the VPS) does the real extraction; default is the
    offline stub. Raises ``ValueError`` on an empty/invalid url (fail-closed —
    a bad link never silently yields empty text the distiller would misread).
    """

    if not url or not urlparse(url).scheme:
        raise ValueError(f"invalid learning url: {url!r}")
    fetch = fetcher or _stub_fetcher
    result = fetch(url)
    result.setdefault("source", _source_of(url))
    result["url"] = url
    return result
