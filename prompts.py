"""prompts.py — fail-closed loader for the agent genome (`agents/*.md`).

Resolves a persona template by name, injects live swarm state via ``str.format``,
and returns the ready-to-invoke prompt string. There is **no** silent fallback:
a missing/renamed template raises ``FileNotFoundError`` and an out-of-directory
name raises ``ValueError`` — callers convert these into a HITL freeze rather than
ever running an agent on an unformatted or generic prompt.
"""

from __future__ import annotations

from pathlib import Path

# Genome directory, anchored to this file (not the cwd) so resolution is stable
# under cron/headless runs from any working directory.
AGENTS_DIR = (Path(__file__).resolve().parent / "agents").resolve()


def render_agent_prompt(name: str, ctx: dict) -> str:
    """Load ``agents/<name>.md`` and inject ``ctx`` via ``str.format``.

    Args:
        name: Template stem (e.g. ``"visionary"``) — no extension, no path parts.
        ctx:  Injection variables; extra keys are ignored by ``str.format``.

    Returns:
        The fully-resolved prompt string.

    Raises:
        ValueError: ``name`` resolves outside ``AGENTS_DIR`` or isn't a ``.md``
            file (blocks path traversal like ``"../state"``).
        FileNotFoundError: the template does not exist (fail-closed; no fallback).
        KeyError: the template references an injection var absent from ``ctx``.
    """

    path = (AGENTS_DIR / f"{name}.md").resolve()

    # Safe path resolution: the resolved file must sit directly in AGENTS_DIR.
    if path.parent != AGENTS_DIR or path.suffix != ".md":
        raise ValueError(f"unsafe prompt name: {name!r}")

    if not path.is_file():
        raise FileNotFoundError(f"agent prompt not found: {path}")

    return path.read_text(encoding="utf-8").format(**ctx)
