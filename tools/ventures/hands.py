"""tools/ventures/hands.py — the execution "hands" seam.

Provider-agnostic interface for how a venture actually *does* things in the
world. The pipeline decides WHAT to do and ``autonomy.resolve_autonomy``
decides WHETHER it may run unattended; ``Hands`` is HOW it happens.

Three capability surfaces cover the venture space:
  * ``run_code``  — write/deploy software (dev hands): a later adapter shells
    out to the Claude Code CLI on the VPS.
  * ``web_task``  — drive a browser for anything with no API (the "mechanical
    hands"): later adapters use Browserbase/Browserless cloud browsers with
    Playwright / Browser-Use.
  * ``api_action`` — authenticated API calls across services: a later adapter
    uses Composio (managed OAuth + hundreds of tool integrations).

v1 ships ``StubHands`` only — it records intent and returns a simulated result,
so the whole engine is offline-testable with zero spend or deploys. Real
adapters (needing creds + egress the sandbox blocks) drop in behind this
interface without touching the pipeline.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Hands(Protocol):
    """What a venture can do in the world. Implementations must not exceed the
    autonomy mode the caller already resolved."""

    def run_code(self, task: str, *, workdir: str | None = None) -> dict: ...
    def web_task(self, goal: str, *, url: str | None = None) -> dict: ...
    def api_action(self, service: str, action: str, args: dict) -> dict: ...


class StubHands:
    """Offline stand-in: logs the intended action, performs nothing, returns a
    simulated success. The default until live adapters are wired."""

    def __init__(self, log: list | None = None):
        self.log = log if log is not None else []

    def _record(self, capability: str, detail: dict) -> dict:
        entry = {"hands": "stub", "capability": capability, **detail}
        self.log.append(entry)
        return {"status": "simulated", **entry}

    def run_code(self, task: str, *, workdir: str | None = None) -> dict:
        return self._record("run_code", {"task": task, "workdir": workdir})

    def web_task(self, goal: str, *, url: str | None = None) -> dict:
        return self._record("web_task", {"goal": goal, "url": url})

    def api_action(self, service: str, action: str, args: dict) -> dict:
        return self._record("api_action", {"service": service, "action": action, "args": args})


# The engine's default hands until real adapters land. Swap by passing a live
# Hands implementation into the pipeline once the adapter increment ships.
DEFAULT_HANDS: Hands = StubHands()
