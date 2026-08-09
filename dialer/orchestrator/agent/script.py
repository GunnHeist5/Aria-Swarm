"""Per-call system-prompt assembly from the prompt Markdown files.

Fail-closed like everything else: a missing prompt file or an opener variant
that isn't defined in ``prompts/openers.md`` raises ConfigError instead of
silently dialing with a broken script — a live call with no opener (and
therefore no AI disclosure) would be a compliance violation, not a cosmetic
bug.

The opener choice is a stable hash of the contact id so a contact always gets
the same variant across retries — otherwise the A/B test data would be
polluted by contacts who heard both openers.
"""

from __future__ import annotations

import hashlib
import re

from orchestrator.config import ConfigError, PROJECT_ROOT, Settings
from orchestrator.models import TouchType

PROMPTS_DIR = PROJECT_ROOT / "prompts"

COMPANY_FALLBACK = "your business"
_COMPANY_PLACEHOLDER = "{{CompanyName}}"
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_VARIANT_HEADING_RE = re.compile(r"^##\s+(\S+)\s*$", re.MULTILINE)

# Spoken when the call is recorded in a two-party/all-party consent state
# (two_party_policy == "disclose"). Appended to the welcome greeting so the
# disclosure happens before any conversation, and restated in the system
# prompt so the agent can confirm it if asked.
RECORDING_DISCLOSURE_LINE = "Just so you know, this call is recorded for quality purposes."


def _load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"prompt file missing: {path}") from exc
    # HTML comments are maintainer notes (TODO markers, editing rules) —
    # they must never reach the model or, worse, be spoken aloud.
    return _HTML_COMMENT_RE.sub("", text).strip()


def _parse_variants(markdown: str) -> dict[str, str]:
    """Split ``## <name>`` sections into {name: body} (comment-free input)."""
    variants: dict[str, str] = {}
    matches = list(_VARIANT_HEADING_RE.finditer(markdown))
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        if body:
            variants[match.group(1)] = body
    return variants


def _opener_text(variant: str) -> str:
    variants = _parse_variants(_load_prompt("openers.md"))
    if variant not in variants:
        raise ConfigError(
            f"opener variant {variant!r} not defined in prompts/openers.md "
            f"(have: {', '.join(sorted(variants)) or 'none'}) — keep "
            "OPENER_VARIANTS and the markdown headings in sync"
        )
    return variants[variant]


def _fill_company(text: str, company_name: str | None) -> str:
    name = (company_name or "").strip() or COMPANY_FALLBACK
    return text.replace(_COMPANY_PLACEHOLDER, name)


def pick_opener_variant(cfg: Settings, contact_id: int) -> str:
    """Stable contact→variant assignment for the A/B test (SPEC §11 TODO a)."""
    if not cfg.opener_variants:
        raise ConfigError("OPENER_VARIANTS is empty — need at least one opener")
    # hashlib, not hash(): Python salts str/bytes hashing per process, and
    # the assignment must survive restarts to keep the A/B split stable.
    digest = hashlib.sha256(str(contact_id).encode("utf-8")).hexdigest()
    return cfg.opener_variants[int(digest, 16) % len(cfg.opener_variants)]


def opener_greeting(
    cfg: Settings,
    *,
    opener_variant: str,
    company_name: str | None,
    two_party_disclose: bool = False,
) -> str:
    """One-line spoken greeting for ConversationRelay's welcomeGreeting.

    This is the first thing the prospect hears (spoken by Twilio TTS before
    any LLM turn), so it carries the AI disclosure — and the recording
    disclosure when the prospect is in a two-party consent state.
    """
    text = _fill_company(_opener_text(opener_variant), company_name)
    greeting = " ".join(text.split())  # markdown line wraps → one spoken line
    if two_party_disclose:
        greeting = f"{greeting} {RECORDING_DISCLOSURE_LINE}"
    return greeting


def build_system_prompt(
    cfg: Settings,
    *,
    touch: TouchType,
    company_name: str | None,
    opener_variant: str,
    two_party_disclose: bool,
) -> str:
    """Assemble the full system prompt for one call."""
    company = (company_name or "").strip() or COMPANY_FALLBACK
    sections = [
        _fill_company(_load_prompt("agent_core.md"), company),
        (
            f"# ASSIGNED OPENER (variant: {opener_variant})\n\n"
            "The call was opened with this exact greeting, already spoken to "
            "the prospect before your first turn — do not repeat it, and do "
            "not switch to another opener version:\n\n"
            + _fill_company(_opener_text(opener_variant), company)
        ),
    ]
    if touch is TouchType.SECOND:
        sections.append(_load_prompt("second_touch.md"))
    if two_party_disclose:
        sections.append(
            "# RECORDING DISCLOSURE\n\n"
            "This prospect is in a two-party consent state and the call is "
            f'recorded. The greeting already stated: "{RECORDING_DISCLOSURE_LINE}" '
            "If the prospect asks, confirm plainly that the call is recorded."
        )
    return "\n\n".join(sections)
