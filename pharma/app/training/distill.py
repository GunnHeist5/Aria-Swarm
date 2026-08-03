"""Ingest channel 2: trainer chat -> structured playbook draft.

The distill function is real and LLM-backed (injectable for tests); the chat UI
around it is the trainer console's session view. The output is a DRAFT — it
still walks through the anonymization scan and human approval like everything
else."""

from __future__ import annotations

from typing import Any, Callable

from app import config
from app.agent import prompts
from app.db import knowledge


def _default_llm(system: str, user_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=config.PHARMA_MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user_text}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("distillation request was declined")
    return next((b.text for b in response.content if b.type == "text"), "")


def distill_session(session_id: str, title: str, domain: str | None = None,
                    llm: Callable[[str, str], str] | None = None) -> str:
    """Turn a trainer chat session's transcript into a draft playbook."""
    if llm is None:
        llm = _default_llm
    transcript = knowledge.list_trainer_messages(session_id)
    if not transcript:
        raise ValueError("empty trainer session")
    text = "\n".join(f"{m['role']}: {m['content']}" for m in transcript)
    body_md = llm(prompts.DISTILLER_SYSTEM, f"Consultant transcript:\n\n{text}")
    return knowledge.create_draft(
        kind="playbook",
        title=title,
        body_md=body_md,
        domain=domain,
        source_kind="trainer_chat",
        source_ref=session_id,
    )
