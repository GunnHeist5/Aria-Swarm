"""Agent script assembly — the conversation persona behind ConversationRelay.

The prompt text itself lives in ``prompts/*.md`` (SPEC §11 ported verbatim);
this package only assembles it per call: pick the A/B opener variant, fill in
the company name, bolt on the second-touch addendum and the recording
disclosure. Keeping the copy in Markdown means Justin can edit the script
without touching code, and tests can pin the assembly rules instead of prose.
"""

from .script import build_system_prompt, opener_greeting, pick_opener_variant

__all__ = ["build_system_prompt", "opener_greeting", "pick_opener_variant"]
