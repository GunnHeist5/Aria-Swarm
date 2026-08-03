"""Layer 3 assembly: engagement context = approved global methods (Layer 1)
+ this tenant's recent learnings (Layer 2). Data flows down only."""

from __future__ import annotations

from app.db import knowledge, tenant as tenant_db_mod


def engagement_context(tenant_id: str, question: str) -> str:
    parts: list[str] = []

    methods = knowledge.search_methods(question, limit=3)
    if methods:
        parts.append("## Approved methods (firm library)")
        for m in methods:
            parts.append(f"### {m['title']} ({m['kind']}, domain: {m['domain'] or 'general'})\n{m['body_md']}")

    learnings = tenant_db_mod.recent_learnings(tenant_id, limit=5)
    if learnings:
        parts.append("## Client-specific guidance (from prior reviews of this client's work)")
        for l in learnings:
            parts.append(f"- {l['text']}")

    return "\n\n".join(parts)
