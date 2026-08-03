"""The gate: anonymization scan -> human review -> approve/publish.

The scan only FLAGS (regex pass + optional LLM pass writing scan_flags); the
human decides. approve() in knowledge.py hard-requires anonymized=1, and only
approve() writes the FTS index — ingestion is open, adoption is gated."""

from __future__ import annotations

import json
import re
from typing import Callable

from app import config
from app.agent import prompts
from app.db import knowledge, tenant as tdb

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_MONEY_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|b|mm|bn|million|billion)?", re.I)
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")


def _regex_flags(text: str) -> list[str]:
    flags: list[str] = []
    flags += [f"email: {m}" for m in _EMAIL_RE.findall(text)[:10]]
    flags += [f"figure: {m.strip()}" for m in _MONEY_RE.findall(text)[:10]]
    flags += [f"phone: {m.strip()}" for m in _PHONE_RE.findall(text)[:10]]
    return flags


def _llm_flags(text: str) -> list[str]:
    if not config.ANTHROPIC_API_KEY:
        return []  # offline: regex-only scan; human review still stands between draft and publish
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.PHARMA_MODEL,
        max_tokens=2048,
        system=prompts.ANONYMIZER_SCAN_SYSTEM,
        messages=[{"role": "user", "content": text[:30000]}],
    )
    if response.stop_reason == "refusal":
        return ["scan-refused: manual review required"]
    raw = next((b.text for b in response.content if b.type == "text"), "[]")
    try:
        start, end = raw.index("["), raw.rindex("]") + 1
        parsed = json.loads(raw[start:end])
        return [str(f) for f in parsed][:50]
    except (ValueError, json.JSONDecodeError):
        return [f"scan-unparseable: {raw[:200]}"]


def scan(method_id: str, use_llm: bool = True) -> list[str]:
    method = knowledge.get_method(method_id)
    if not method:
        raise ValueError("unknown method")
    text = f"{method['title']}\n{method['body_md']}"
    flags = _regex_flags(text)
    if use_llm:
        flags += _llm_flags(text)
    knowledge.update_draft(method_id, scan_flags=json.dumps(flags))
    with knowledge.connect() as conn:
        conn.execute(
            "UPDATE methods SET status = 'pending_review' WHERE method_id = ? AND status = 'draft'",
            (method_id,),
        )
    return flags


def approve_method(method_id: str, approved_by: str, anonymization_confirmed: bool) -> None:
    """Human decision point. The reviewer asserts the text is anonymized; only
    then does approve() publish it to the search index."""
    knowledge.mark_anonymized(method_id, anonymization_confirmed)
    knowledge.approve(method_id, approved_by)  # raises unless anonymized=1


def reject_method(method_id: str, rejected_by: str) -> None:
    knowledge.reject(method_id, rejected_by)


def promote_learning(tenant_id: str, learning_text: str, generalized_title: str,
                     generalized_body: str, domain: str | None = None) -> str:
    """Promotion from Layer 2 to Layer 1 is a separate, explicit act: the
    trainer writes a GENERALIZED version; the original client-specific text
    stays in the client workspace. The draft still walks the scan+approve
    gate before it is ever retrievable."""
    tdb.validate_tenant_id(tenant_id)
    return knowledge.create_draft(
        kind="playbook",
        title=generalized_title,
        body_md=generalized_body,
        domain=domain,
        source_kind="correction_promotion",
        source_ref=None,  # never a tenant id — Layer 1 carries no client references
    )
