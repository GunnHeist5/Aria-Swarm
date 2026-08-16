"""Auto-grader: compares an attempt's answer and steps against the answer key.

Fail-closed like review._llm_flags: anything unparseable or refused becomes
verdict='error' with score 0 — into the miss queue for human eyes, never a
silent pass.
"""

from __future__ import annotations

import json
from typing import Callable

from app import config
from app.agent import prompts
from app.db import gym

GraderLLM = Callable[[str, str], str]

_TRANSCRIPT_EVENT_CHARS = 2000
_PAYLOAD_CHARS = 30000


class AnthropicGrader:
    def __init__(self) -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        self._model = config.PHARMA_MODEL

    def __call__(self, system: str, user_text: str) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_text}],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("grader request was declined")
        return next((b.text for b in response.content if b.type == "text"), "")


def _render_transcript(transcript_json: str | None) -> str:
    if not transcript_json:
        return "(no tool activity)"
    try:
        events = json.loads(transcript_json)
    except json.JSONDecodeError:
        return "(transcript unreadable)"
    lines = []
    for e in events:
        if e.get("type") == "tool_use":
            lines.append(f"[tool_use] {e.get('name')} input={json.dumps(e.get('input', {}))[:_TRANSCRIPT_EVENT_CHARS]}")
        else:
            flag = " (error)" if e.get("is_error") else ""
            lines.append(f"[tool_result]{flag} {str(e.get('content', ''))[:_TRANSCRIPT_EVENT_CHARS]}")
    return "\n".join(lines) or "(no tool activity)"


def _payload(problem: dict, attempt: dict) -> str:
    return (
        f"# Problem\n{problem['prompt_md']}\n\n"
        f"# Expected steps (trainer)\n{problem.get('expected_steps_md') or '(none provided)'}\n\n"
        f"# Answer key (trainer — ground truth)\n{problem['answer_key_md']}\n\n"
        f"# Trainee final answer\n{attempt.get('answer_md') or '(no answer produced)'}\n\n"
        f"# Trainee tool transcript\n{_render_transcript(attempt.get('transcript_json'))}"
    )[:_PAYLOAD_CHARS]


def _parse(raw: str) -> tuple[str, float, list[str]]:
    start, end = raw.index("{"), raw.rindex("}") + 1
    parsed = json.loads(raw[start:end])
    verdict = parsed["verdict"]
    if verdict not in ("pass", "partial", "fail"):
        raise ValueError(f"bad verdict: {verdict}")
    score = min(1.0, max(0.0, float(parsed.get("score", 0.0))))
    gaps = [str(g) for g in parsed.get("gaps", [])][:20]
    return verdict, score, gaps


def grade_attempt(attempt: dict, problem: dict, llm: GraderLLM | None = None) -> str:
    """Grade one finished attempt; returns grade_id. Never raises on grader
    misbehavior — that becomes verdict='error'."""
    if attempt["status"] in ("failed", "refused"):
        return gym.add_grade(
            attempt["attempt_id"], "fail", 0.0,
            json.dumps([f"attempt did not complete: {attempt['status']}"]),
        )

    if llm is None:
        llm = AnthropicGrader()
    raw = ""
    try:
        raw = llm(prompts.GYM_GRADER_SYSTEM, _payload(problem, attempt))
        verdict, score, gaps = _parse(raw)
    except Exception as exc:
        return gym.add_grade(
            attempt["attempt_id"], "error", 0.0,
            json.dumps([f"grader-unparseable: {str(exc)[:100]}: {raw[:200]}"]),
            grader_raw=raw or None,
        )
    return gym.add_grade(attempt["attempt_id"], verdict, score, json.dumps(gaps), grader_raw=raw)
