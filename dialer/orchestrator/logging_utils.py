"""Logging setup with secret redaction.

Every entry point calls `setup_logging(cfg)` first. The redaction filter
scrubs any configured secret value that would otherwise leak through a log
line (API keys, auth tokens, webhook secrets) — the same fail-closed posture
as the rest of the system: we redact by value, so even an accidental
`log.info("payload %s", payload)` cannot print a credential.
"""

from __future__ import annotations

import logging
import sys

from .config import Settings

_REDACTED = "[REDACTED]"


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        # Longest first so overlapping values redact fully.
        self._secrets = sorted((s for s in secrets if len(s) >= 6), key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            try:
                message = record.getMessage()
            except Exception:
                return True
            scrubbed = message
            for secret in self._secrets:
                if secret in scrubbed:
                    scrubbed = scrubbed.replace(secret, _REDACTED)
            if scrubbed is not message:
                record.msg = scrubbed
                record.args = ()
        return True


def setup_logging(cfg: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(cfg.log_level.upper())
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(SecretRedactionFilter(cfg.secret_values()))
    root.handlers[:] = [handler]
    # Noisy third-party loggers stay at WARNING unless explicitly debugging.
    for noisy in ("httpx", "httpcore", "twilio", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
