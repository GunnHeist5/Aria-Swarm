"""The analyst agentic loop (dealflow pattern: plain dict messages, duck-typed
LLM so tests drive the loop with a scripted stub, manual tool loop so there is
no beta-SDK dependency).

The LLM callable receives (system, messages, tools) and returns an object with
`.content` (blocks with .type/.text/.name/.input/.id) and `.stop_reason` — the
anthropic SDK response shape. `AnthropicLLM` is the production implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app import config
from app.agent import prompts, retrieval, tools as toolbox_mod
from app.db import control, tenant as tdb

MAX_TURNS = 25


class LLM(Protocol):
    def __call__(self, system: str, messages: list[dict], tools: list[dict]) -> Any: ...


class AnthropicLLM:
    def __init__(self) -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = config.PHARMA_MODEL

    def __call__(self, system: str, messages: list[dict], tools: list[dict]) -> Any:
        return self._client.messages.create(
            model=self._model,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=tools,
        )


@dataclass
class AnalysisResult:
    analysis_id: str
    status: str  # done | failed | refused
    answer: str
    tokens_in: int = 0
    tokens_out: int = 0


def _usage_of(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0


def run_analysis(tenant_id: str, question: str, conversation_id: str | None = None,
                 llm: LLM | None = None,
                 on_tool_event: Callable[[dict], None] | None = None,
                 analysis_id: str | None = None) -> AnalysisResult:
    tdb.validate_tenant_id(tenant_id)
    if llm is None:
        llm = AnthropicLLM()

    if analysis_id is None:
        analysis_id = tdb.create_analysis(tenant_id, question, conversation_id)
    specs, impls = toolbox_mod.build_toolbox(tenant_id, analysis_id)

    context = retrieval.engagement_context(tenant_id, question)
    system = prompts.ANALYST_SYSTEM
    if context:
        system += f"\n\n# Engagement context\n{context}"

    messages: list[dict] = []
    if conversation_id:
        for m in tdb.list_messages(tenant_id, conversation_id):
            if m["role"] in ("user", "assistant") and m["content"]:
                messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})

    tokens_in = tokens_out = 0
    answer = ""
    status = "failed"
    try:
        for _ in range(MAX_TURNS):
            response = llm(system, messages, specs)
            ti, to = _usage_of(response)
            tokens_in += ti
            tokens_out += to

            if response.stop_reason == "refusal":
                status, answer = "refused", "The analysis request was declined by the model's safety system."
                break

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_blocks:
                answer = next((b.text for b in response.content if b.type == "text"), "")
                status = "done"
                break

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in tool_blocks:
                impl = impls.get(block.name)
                if impl is None:
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Unknown tool: {block.name}", "is_error": True})
                    continue
                try:
                    out = impl(**(block.input or {}))
                except Exception as exc:  # tool errors go back to the model, never crash the loop
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": f"Tool error: {exc}", "is_error": True})
                else:
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
                if on_tool_event:
                    on_tool_event({"type": "tool_use", "name": block.name, "input": block.input or {}})
                    on_tool_event({"type": "tool_result", "name": block.name,
                                   "content": str(results[-1]["content"])[:4000],
                                   "is_error": bool(results[-1].get("is_error"))})
            messages.append({"role": "user", "content": results})
        else:
            answer = "Analysis stopped: exceeded the maximum number of steps."
    finally:
        tdb.finish_analysis(tenant_id, analysis_id, status, answer[:2000] if answer else None)
        control.record_usage(tenant_id, "analysis", tokens_in, tokens_out, config.PHARMA_MODEL)
        if conversation_id:
            tdb.add_message(tenant_id, conversation_id, "user", question)
            if answer:
                tdb.add_message(tenant_id, conversation_id, "assistant", answer)

    return AnalysisResult(analysis_id=analysis_id, status=status, answer=answer,
                          tokens_in=tokens_in, tokens_out=tokens_out)
